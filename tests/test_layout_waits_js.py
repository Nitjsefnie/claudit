from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tests.node_run import run_node

ROOT = Path(__file__).resolve().parents[1]

# A Chromium page double driven by hand-emitted events, under a clock the test
# advances, so a readiness deadline expires exactly when told to. Each request
# is reported twice, as the two real sources report it: CDP's Network stream in
# the order the document sent it, tagged with that document's loader, and
# Playwright's `request` event, emitted only once Fetch.requestPaused pairs with
# it, which can be after the next document commits.
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
  const main = {name: 'main'}, handlers = {}, cdpHandlers = {};
  let panelVisible = true, loader = 'document-1', ids = 0;
  const cdp = {
    on: (name, fn) => { (cdpHandlers[name] ||= []).push(fn); },
    send: async method => {
      if (method === 'Network.enable' || method === 'Page.enable') return {};
      if (method === 'Page.getFrameTree') return {frameTree: {frame: {id: 'main', loaderId: loader}}};
      throw new Error(`unmodelled CDP method ${method}`);
    },
  };
  const makePage = () => {
    const self = {
      on: (name, fn) => { (handlers[name] ||= []).push(fn); },
      mainFrame: () => main,
      viewportSize: () => ({width: 375, height: 900}),
      waitForFunction: () => panelVisible ? Promise.resolve() : new Promise(() => {}),
      evaluate: fn => fn(),
      context: () => ({newCDPSession: async target => {
        if (target !== self) throw new Error('CDP session for another page');
        return cdp;
      }}),
    };
    return self;
  };
  const page = makePage();
  const emit = (name, arg) => { for (const fn of handlers[name] || []) fn(arg); };
  const cdpEmit = (name, arg) => { for (const fn of cdpHandlers[name] || []) fn(arg); };
  const api = path => {
    const id = `request-${++ids}`, url = `http://test${path}`;
    return {id, url, loaderId: loader,
      request: {url: () => url, isNavigationRequest: () => false, frame: () => main}};
  };
  const send = r => cdpEmit('Network.requestWillBeSent',
    {requestId: r.id, loaderId: r.loaderId, frameId: 'main', request: {url: r.url}});
  const pair = r => emit('request', r.request);
  const start = r => { send(r); pair(r); };
  const finish = r => {
    emit('requestfinished', r.request); cdpEmit('Network.loadingFinished', {requestId: r.id});
  };
  // The reload: its document request, then the commit under a new loader.
  const commit = ({cdpEvent = true} = {}) => {
    emit('request', {url: () => 'http://test/', isNavigationRequest: () => true, frame: () => main});
    loader = `document-${Number(loader.split('-')[1]) + 1}`;
    if (cdpEvent) cdpEmit('Page.frameNavigated', {frame: {id: 'main', loaderId: loader}});
    emit('framenavigated', main);
  };
  await waits.trackLayoutRequests(page);
"""


def _node(body: str):
    proc = run_node(
        ["node", "--input-type=module"], input=HARNESS + body, capture_output=True,
        text=True, timeout=60, check=False, encoding="utf-8", errors="strict", cwd=ROOT,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return json.loads(proc.stdout)


def _require_node():
    if shutil.which("node") is None:
        pytest.skip("node not available")


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
    _require_node()
    out = _node(f"const stalled = {json.dumps(stalled)}, options = {json.dumps(options)};" + """
      panelVisible = stalled !== 'panel';
      const done = api('/api/tool-usage');
      start(done); finish(done);
      if (stalled === 'requests') start(api('/api/projects?range=all'));
      const result = observe(waits.settleDashboard(page, options));
      await flush();
      // Issued after the wait began: the message reads the live set at expiry.
      if (stalled === 'requests') start(api('/api/dashboard'));
      await advance(10);
      console.log(JSON.stringify(result));
    """)
    assert out == {"status": "rejected", "error": message}


@pytest.mark.parametrize("when", [
    "before-settle", "during-settle", "paired-after-commit", "commit-event-late"])
def test_a_reload_releases_the_requests_its_old_document_left_in_flight(when):
    _require_node()
    out = _node(f"const when = {json.dumps(when)};" + """
      // Still held in its route when the reload commits: neither source ever
      // reports it finished or failed.
      const old = api('/api/dashboard?range=all');
      send(old);
      if (when !== 'paired-after-commit') pair(old);
      const settle = () => observe(waits.settleDashboard(page, {timeoutMs: 10}));
      let result = when === 'during-settle' ? settle() : null;
      await flush();
      commit({cdpEvent: when !== 'commit-event-late'});
      if (when === 'paired-after-commit') pair(old);
      const fresh = api('/api/dashboard');
      start(fresh);
      result ||= settle();
      await flush();
      const waiting = result.status;
      finish(fresh);
      await flush(); await advance(10);
      console.log(JSON.stringify({waiting, result}));
    """)
    assert out == {"waiting": "pending", "result": {"status": "fulfilled"}}


@pytest.mark.parametrize("event", ["child-frame-commit", "redirect"])
def test_a_live_request_holds_the_settle_through(event):
    _require_node()
    out = _node(f"const event = {json.dumps(event)};" + """
      const live = api('/api/dashboard');
      start(live);
      const result = observe(waits.settleDashboard(page, {timeoutMs: 10}));
      await flush();
      if (event === 'redirect') send(live);
      else cdpEmit('Page.frameNavigated', {frame: {id: 'child', parentId: 'main', loaderId: 'child-1'}});
      await flush();
      const waiting = result.status;
      finish(live);
      await flush(); await advance(10);
      console.log(JSON.stringify({waiting, result}));
    """)
    assert out == {"waiting": "pending", "result": {"status": "fulfilled"}}


def test_a_positional_timeout_is_refused():
    _require_node()
    out = _node("""
      panelVisible = false;
      const result = observe(waits.settleDashboard(page, 10));
      await flush(); await advance(10);
      console.log(JSON.stringify(result));
    """)
    assert out == {"status": "rejected",
                   "error": "settleDashboard takes {timeoutMs, call}, not 10"}


@pytest.mark.parametrize("case", ["untracked-page", "failing-label", "stalled-tracking"])
def test_a_deadline_rejects_readably_instead_of_crashing(case):
    _require_node()
    out = _node(f"const scenario = {json.dumps(case)};" + """
      panelVisible = false;
      const stalled = {...makePage(), context: () => ({newCDPSession: () => new Promise(() => {})})};
      const result = observe(scenario === 'untracked-page'
        ? waits.settleDashboard(makePage(), {timeoutMs: 10})
        : scenario === 'stalled-tracking' ? waits.trackLayoutRequests(stalled)
          : waits.withDeadline(() => new Promise(() => {}),
            () => { throw new Error('label broke'); }, 10));
      await flush(); await advance(30_000);
      console.log(JSON.stringify(result));
    """)
    assert out == {"status": "rejected", "error": {
        "untracked-page": "settleDashboard needs trackLayoutRequests(page) first",
        "failing-label": "timed out waiting; its label failed: label broke",
        "stalled-tracking": "timed out waiting for layout request tracking",
    }[case]}
