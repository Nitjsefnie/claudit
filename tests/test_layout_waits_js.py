from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tests.node_run import run_node

ROOT = Path(__file__).resolve().parents[1]

# A Playwright page double driven by hand-emitted events, under a clock the
# test advances, so a readiness deadline expires exactly when told to.
HARNESS = r"""
  const waits = await import('./scripts/ci/layout_waits.mjs');
  let now = 0, serial = 0;
  const timers = new Map();
  globalThis.setTimeout = (fn, ms) => {
    const id = ++serial; timers.set(id, {fn, at: now + ms}); return id;
  };
  globalThis.clearTimeout = id => timers.delete(id);
  Object.defineProperty(globalThis, 'performance', {value: {now: () => now}});
  globalThis.requestAnimationFrame = fn => queueMicrotask(fn);
  globalThis.requestIdleCallback = fn => queueMicrotask(fn);
  globalThis.document = {fonts: {ready: Promise.resolve()}};
  const flush = async () => { for (let i = 0; i < 100; i++) await Promise.resolve(); };
  const advance = async ms => {
    now += ms;
    for (const [id, timer] of [...timers]) {
      if (timer.at <= now && timers.delete(id)) timer.fn();
    }
    await flush();
  };
  const observe = promise => {
    const result = {status: 'pending'};
    promise.then(() => { result.status = 'fulfilled'; },
      error => Object.assign(result, {status: 'rejected', error: error.message}));
    return result;
  };
  const main = {name: 'main'}, child = {name: 'child'}, handlers = {};
  let panelVisible = true;
  const page = {
    on: (name, fn) => { (handlers[name] ||= []).push(fn); },
    mainFrame: () => main,
    viewportSize: () => ({width: 375, height: 900}),
    waitForFunction: () => panelVisible ? Promise.resolve() : new Promise(() => {}),
    evaluate: fn => fn(),
  };
  const emit = (name, arg) => { for (const fn of handlers[name] || []) fn(arg); };
  const api = (path, frame = main) => ({url: () => `http://test${path}`,
    isNavigationRequest: () => false, frame: () => frame});
  const documentRequest = frame => ({url: () => 'http://test/',
    isNavigationRequest: () => true, frame: () => frame});
  waits.trackLayoutRequests(page);
"""


def _node(body: str):
    proc = run_node(
        ["node", "--input-type=module"], input=HARNESS + body, capture_output=True,
        text=True, timeout=60, check=False, encoding="utf-8", errors="strict", cwd=ROOT,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return json.loads(proc.stdout)


@pytest.mark.parametrize("stalled,options,message", [
    ("requests", {"timeoutMs": 10, "call": "reload for rule panel-label-fits"},
     "timed out waiting for dashboard readiness at width 375, reload for rule"
     " panel-label-fits: API requests; pending API requests:"
     " http://test/api/projects?range=all, http://test/api/dashboard"),
    ("panel", {"timeoutMs": 10},
     "timed out waiting for dashboard readiness at width 375: visible panel;"
     " pending API requests: none"),
], ids=["requests", "panel"])
def test_a_readiness_timeout_names_width_call_and_every_pending_request(stalled, options, message):
    if shutil.which("node") is None:
        pytest.skip("node not available")
    out = _node(f"const stalled = {json.dumps(stalled)}, options = {json.dumps(options)};" + """
      panelVisible = stalled !== 'panel';
      const done = api('/api/tool-usage');
      emit('request', done); emit('requestfinished', done);
      if (stalled === 'requests') emit('request', api('/api/projects?range=all'));
      const result = observe(waits.settleDashboard(page, options));
      await flush();
      // Issued after the wait began: the message reads the live set at expiry.
      if (stalled === 'requests') emit('request', api('/api/dashboard'));
      await advance(10);
      console.log(JSON.stringify(result));
    """)
    assert out == {"status": "rejected", "error": message}
