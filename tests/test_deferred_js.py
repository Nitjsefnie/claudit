"""The deferred-transform helper, driven through node.

`src/deferred.js` is plain JS on purpose -- no React, no JSX -- so node
can execute it, exactly as tests/test_dashboard_fetch_js.py drives the
real src/dashboard-fetch.js. Nothing else in the suite can see it: it
schedules a callback with requestIdleCallback (or setTimeout), and the
panel that routes its trace transform through it is JSX node cannot parse.

Every branch is exercised: run with and without requestIdleCallback, and
cancel over an idle handle (with and without cancelIdleCallback), a
timeout handle, a missing handle and an already-cancelled one. The JS
coverage floor is a real risk for a new half-covered module.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from tests.node_run import run_node

ROOT = Path(__file__).resolve().parents[1]
DEFERRED_JS = ROOT / "src" / "deferred.js"


def _node(body: str):
    script = f"""
      global.window = {{}};
      require({str(DEFERRED_JS)!r});
      {body}
    """
    # Over STDIN, not -e: the payload embeds JSON, and Windows refuses a
    # CreateProcess command line over 32k (WinError 206).
    proc = run_node(
        ["node"], input=script, capture_output=True, text=True, timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# The setTimeout/clearTimeout fakes, so a test can fire the deferred
# callback by hand and read which ids were cleared.
_TIMERS = """
  const timers = [], cleared = [];
  global.setTimeout = (fn, ms) => { timers.push(fn); return timers.length; };
  global.clearTimeout = (id) => { cleared.push(id); if (id) timers[id - 1] = null; };
  const tick = () => { const live = timers.filter(Boolean); timers.length = 0; live.forEach(f => f()); };
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_run_defers_and_then_invokes_with_the_value():
    """Without requestIdleCallback, run schedules via setTimeout -- the
    callback does NOT fire synchronously, and gets the value when it does."""
    out = _node(_TIMERS + """
      const got = [];
      window.deferred.run(42, v => got.push(v));
      const beforeTick = got.slice();
      tick();
      console.log(JSON.stringify({beforeTick, afterTick: got.slice()}));
    """)
    assert out["beforeTick"] == [], "run must not call the callback synchronously"
    assert out["afterTick"] == [42]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_run_uses_request_idle_callback_when_present():
    """The scheduling path is decided per call: a page WITH
    requestIdleCallback uses it (no setTimeout), and cancel calls
    cancelIdleCallback."""
    out = _node("""
      const idle = [], cancelled = [];
      global.requestIdleCallback = (cb) => { idle.push(cb); return 7; };
      global.cancelIdleCallback = (id) => { cancelled.push(id); };
      global.setTimeout = () => { throw new Error('setTimeout used'); };
      const got = [];
      const h = window.deferred.run('x', v => got.push(v));
      const scheduled = idle.length;
      idle[0]();
      const afterFire = got.slice();
      const h2 = window.deferred.run('y', v => got.push(v));
      window.deferred.cancel(h2);
      idle[1]();   // the cancelled callback is now a no-op
      console.log(JSON.stringify({
        scheduled, afterFire, cancelled, afterCancelFire: got.slice(),
        handleType: typeof h,
      }));
    """)
    assert out["scheduled"] == 1, "requestIdleCallback was not used"
    assert out["afterFire"] == ["x"]
    assert out["cancelled"] == [7], "cancel must call cancelIdleCallback"
    assert out["afterCancelFire"] == ["x"], "a cancelled run still fired"
    assert out["handleType"] == "object"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_cancel_before_fire_prevents_the_callback():
    """cancel on a pending setTimeout handle clears the timer AND makes
    the callback a no-op even if the timer somehow still fires."""
    out = _node(_TIMERS + """
      const got = [];
      const h = window.deferred.run(1, v => got.push(v));
      window.deferred.cancel(h);
      tick();
      console.log(JSON.stringify({got, cleared}));
    """)
    assert out["got"] == [], "a cancelled run still fired"
    assert out["cleared"] == [1], "cancel must clearTimeout the handle"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_cancel_idle_handle_without_cancel_idle_callback_is_a_noop():
    """An idle handle on a page with no cancelIdleCallback must still be
    neutralised by the cancelled flag (the guard, not the platform)."""
    out = _node("""
      const idle = [];
      global.requestIdleCallback = (cb) => { idle.push(cb); return 3; };
      global.cancelIdleCallback = undefined;
      const got = [];
      const h = window.deferred.run(1, v => got.push(v));
      window.deferred.cancel(h);
      idle[0]();
      console.log(JSON.stringify({got}));
    """)
    assert out["got"] == []


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_cancel_guards_missing_and_already_cancelled_handles():
    """cancel(null) and a second cancel must be safe no-ops."""
    out = _node(_TIMERS + """
      window.deferred.cancel(null);
      window.deferred.cancel(undefined);
      const h = window.deferred.run(1, () => {});
      window.deferred.cancel(h);
      window.deferred.cancel(h);
      console.log(JSON.stringify({cleared}));
    """)
    assert out["cleared"] == [1], "a repeat cancel must not clear twice"
