"""The browser telemetry client, driven through node (issue #436).

`src/perf.js` is plain JS on purpose -- no React, no JSX -- so node can
execute it, exactly as `tests/test_dashboard_fetch_js.py` drives the real
`src/dashboard-fetch.js`. Nothing in the suite can otherwise see this
code: the beacons it emits only exist in a browser, and the panel that
reads them back is JSX node cannot parse.

Every assertion here was proven red against a missing `src/perf.js`
before the fix. The fakes stand in for the browser globals the module
touches (`performance`, `PerformanceObserver`, `navigator`,
`sessionStorage`, the timers), so no test depends on a real browser --
and none of them may send anything cross-origin or surface an error: a
telemetry failure must never reach a render.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PERF_JS = ROOT / "src" / "perf.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available"
)

# The journey names the /api/metrics contract closes over. A name outside
# this set is a 400, so a typo in a call site must not become a beacon.
JOURNEYS = ("dashboard_open", "inspector_open", "signin")

_PREAMBLE = r"""
  const CFG = __CFG__;
  const sent = [];      // sendBeacon bodies
  const posted = [];    // fetch() fallbacks
  const timers = [];
  let clock = 1000;     // the fake performance clock, in ms

  // node ships real `navigator`, `performance` and `fetch` globals, and
  // plain assignment onto those does not take -- defineProperty, or the
  // fakes are silently ignored and every test measures node's own.
  const fake = (name, value) => Object.defineProperty(globalThis, name, {
    value, writable: true, configurable: true, enumerable: true,
  });

  fake('window', {
    addEventListener: (n, f) => { (window._l = window._l || {})[n] = f; },
  });
  fake('performance', {
    timeOrigin: 1000000,
    now: () => clock,
    getEntriesByType: t => (CFG.nav && t === 'navigation') ? [CFG.nav] : [],
  });
  fake('sessionStorage', {
    _d: Object.assign({}, CFG.session || {}),
    getItem(k) { return Object.prototype.hasOwnProperty.call(this._d, k) ? this._d[k] : null; },
    setItem(k, v) { this._d[k] = String(v); },
    removeItem(k) { delete this._d[k]; },
  });
  fake('document', {
    visibilityState: 'visible',
    addEventListener: (n, f) => { (document._l = document._l || {})[n] = f; },
  });
  fake('Blob', class {
    constructor(parts, o) { this.parts = parts; this.type = (o || {}).type; }
  });
  fake('setTimeout', (fn) => { timers.push(fn); return timers.length; });
  fake('clearTimeout', (id) => { if (id) timers[id - 1] = null; });
  global.__tick = () => { const live = timers.filter(Boolean); timers.length = 0; live.forEach(f => f()); };

  fake('navigator', CFG.sendBeacon
    ? { sendBeacon: (url, blob) => { sent.push({url, body: blob.parts.join('')}); return true; } }
    : {});
  fake('fetch', CFG.fetchOk === false ? undefined : (url, init) => {
    posted.push({ url, init });
    return Promise.resolve({ ok: true });
  });

  if (CFG.observers) {
    const OBS = [];
    fake('PerformanceObserver', class {
      constructor(cb) { this.cb = cb; this.types = []; OBS.push(this); }
      observe(opts) {
        if (CFG.observers.indexOf(opts.type) < 0) throw new TypeError('unsupported entry type');
        this.types.push(opts.type);
      }
    });
    global.__observers = OBS;
    global.__emit = (type, entries) => OBS.forEach(o => {
      if (o.types.indexOf(type) >= 0) o.cb({ getEntries: () => entries });
    });
  } else {
    fake('PerformanceObserver', undefined);
    global.__emit = () => {};
  }

  require(__PERF__);

  // One entry per request, in transport order.
  const batches = () => sent.concat(
    posted.map(p => ({ url: p.url, body: p.init.body }))
  ).map(r => JSON.parse(r.body).beacons);
  // Every beacon of every batch, flattened.
  const beacons = () => batches().reduce((a, b) => a.concat(b), []);
"""


def _run(body: str, **cfg) -> dict:
    """Run `body` against the real src/perf.js, with faked browser globals.

    The body must emit exactly one JSON object on stdout.
    """
    script = (_PREAMBLE
              .replace("__CFG__", json.dumps(cfg))
              .replace("__PERF__", json.dumps(str(PERF_JS)))
              + body)
    # Over STDIN, not -e: the payload embeds JSON and Windows refuses a
    # CreateProcess command line over 32k (WinError 206).
    proc = subprocess.run(
        ["node"], input=script, capture_output=True, text=True, timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert len(lines) == 1, (
        f"the driver body must print exactly one JSON object, got "
        f"{len(lines)}: {proc.stdout!r}")
    return json.loads(lines[0])


# --- journeys ---------------------------------------------------------

def test_journey_reports_its_measured_parts_only():
    """total / fetch / client, in the shape the endpoint's closed
    metric x part table admits."""
    out = _run("""
      window.perf.openJourney('dashboard_open');
      clock = 1400;
      window.perf.closeFetch('dashboard_open');
      clock = 2000;
      window.perf.closeJourney('dashboard_open');
      __tick();
      console.log(JSON.stringify({ beacons: beacons() }));
    """, sendBeacon=True, observers=["layout-shift", "longtask"])
    rows = out["beacons"]
    assert [r["part"] for r in rows] == ["total", "fetch", "client"], rows
    assert all(r["metric"] == "dashboard_open" for r in rows)
    assert [r["value"] for r in rows] == [1000, 400, 600], rows


def test_a_journey_that_never_measured_its_fetch_is_not_sent():
    """A part that was never timed is not sent at all. A fabricated 0
    would read on the panel as a real measurement -- 'the client took no
    time at all' -- which is a measurement nobody took."""
    out = _run("""
      window.perf.openJourney('inspector_open');
      clock = 1500;
      window.perf.closeJourney('inspector_open');
      __tick();
      console.log(JSON.stringify({ beacons: beacons() }));
    """, sendBeacon=True, observers=[])
    rows = out["beacons"]
    assert [r["part"] for r in rows] == ["total"], (
        f"a journey with no closeFetch reported a fetch/client split: {rows}")


def test_a_journey_beacon_carries_no_region_and_no_phase():
    """Journeys name themselves; a region or a phase on a journey beacon
    is a 400, and the server CLAMPS values rather than refusing them --
    so a rejected beacon would vanish silently instead of surfacing."""
    out = _run("""
      window.perf.openJourney('signin');
      window.perf.markUsable();
      __tick();
      console.log(JSON.stringify({ beacons: beacons() }));
    """, sendBeacon=True, observers=[], nav={"responseStart": 300, "startTime": 0})
    assert out["beacons"], out
    for row in out["beacons"]:
        assert set(row) == {"metric", "part", "value"}, (
            f"a journey beacon carries keys outside the contract: {sorted(row)}")


def test_closing_a_journey_that_never_opened_sends_nothing():
    """SessionView renders rows for whatever transcript it was handed;
    a close with no open journey is not a measurement."""
    out = _run("""
      window.perf.closeJourney('dashboard_open');
      window.perf.closeFetch('dashboard_open');
      __tick();
      console.log(JSON.stringify({ beacons: beacons() }));
    """, sendBeacon=True, observers=[])
    assert out["beacons"] == []


def test_a_journey_still_reports_without_performance_observer():
    """A browser with no PerformanceObserver must not lose the journeys
    -- and must not throw at load either."""
    out = _run("""
      window.perf.openJourney('dashboard_open');
      clock = 1250;
      window.perf.closeJourney('dashboard_open');
      __tick();
      console.log(JSON.stringify({ beacons: beacons() }));
    """, sendBeacon=True, observers=None)
    assert [r["part"] for r in out["beacons"]] == ["total"]
    assert out["beacons"][0]["value"] == 250


def test_an_unsupported_entry_type_does_not_take_the_supported_one_down():
    """`observe({type})` throws in a browser that lacks the entry type,
    so registration is per-observer and one refusal costs one metric."""
    out = _run("""
      window.perf.openJourney('signin');
      window.perf.markUsable();
      __tick();
      const registered = __observers.reduce((a, o) => a.concat(o.types), []);
      console.log(JSON.stringify({ beacons: beacons(), registered }));
    """, sendBeacon=True, observers=["layout-shift"])
    assert [r["metric"] for r in out["beacons"]] == ["signin"]
    assert out["registered"] == ["layout-shift"], (
        "an unsupported entry type took the supported one down with it")


# --- the observed metrics --------------------------------------------

def test_a_user_caused_shift_is_not_a_defect():
    """hadRecentInput: a shift the user caused (scrolling, clicking) is
    excluded from the Layout Instability score by the API's own rule, and
    reporting it would make every interaction a 'defect'."""
    out = _run("""
      __emit('layout-shift', [
        { value: 0.31, hadRecentInput: true, sources: [{node: {}}] },
      ]);
      __tick();
      console.log(JSON.stringify({ beacons: beacons() }));
    """, sendBeacon=True, observers=["layout-shift"])
    assert out["beacons"] == [], out


def test_a_shift_is_tagged_with_its_region_and_phase():
    out = _run("""
      const inside = {
        getAttribute: n => (n === 'data-perf-region' ? 'panel_grid' : null),
        parentElement: { getAttribute: () => null, parentElement: null },
      };
      __emit('layout-shift', [{ value: 0.04, hadRecentInput: false, sources: [{node: inside}] }]);
      __tick();
      console.log(JSON.stringify({ beacons: beacons(), phase: window.perf.phase() }));
    """, sendBeacon=True, observers=["layout-shift"])
    rows = out["beacons"]
    assert rows == [{"metric": "layout_shift", "part": "shift", "value": 0.04,
                     "region": "panel_grid", "phase": "pre_paint"}], rows


def test_a_shift_with_no_sources_falls_back_to_other():
    """`entry.sources` is an array that CAN be empty -- it is, whenever
    the shifted nodes have already been detached. `sources[0].node` on
    that is a TypeError inside an observer callback, which would take the
    whole observer with it."""
    out = _run("""
      __emit('layout-shift', [
        { value: 0.02, hadRecentInput: false, sources: [] },
        { value: 0.03, hadRecentInput: false },
        { value: 0.01, hadRecentInput: false, sources: [{}] },
      ]);
      __tick();
      console.log(JSON.stringify({ beacons: beacons() }));
    """, sendBeacon=True, observers=["layout-shift"])
    rows = out["beacons"]
    assert [r["region"] for r in rows] == ["other", "other", "other"], rows
    assert [r["value"] for r in rows] == [0.02, 0.03, 0.01], rows


def test_an_unrecognised_region_attribute_is_not_echoed():
    """region is a closed set; an unknown value is a 400, and a 400
    takes every beacon in the batch with it."""
    out = _run("""
      const node = {
        getAttribute: n => (n === 'data-perf-region' ? 'not-a-region' : null),
        parentElement: null,
      };
      __emit('layout-shift', [{ value: 0.05, hadRecentInput: false, sources: [{node}] }]);
      __tick();
      console.log(JSON.stringify({
        beacons: beacons(), direct: window.perf.region(node) }));
    """, sendBeacon=True, observers=["layout-shift"])
    assert out["direct"] == "other", out
    assert all(r["region"] in ("panel_grid", "inspector", "signin", "other")
               for r in out["beacons"]), out


def test_a_long_task_carries_a_phase_and_no_region():
    """The /api/metrics table makes region REQUIRED for layout_shift and
    FORBIDDEN for longtask."""
    out = _run("""
      __emit('longtask', [{ duration: 812.5 }]);
      __tick();
      console.log(JSON.stringify({ beacons: beacons() }));
    """, sendBeacon=True, observers=["longtask"])
    assert out["beacons"] == [{"metric": "longtask", "part": "block",
                               "value": 812.5, "phase": "pre_paint"}], out


def test_the_phase_moves_with_the_page_and_the_sse_tick():
    """pre_paint -> post_usable -> sse_update, and back: a shift during
    the SSE-driven repaint is a different defect from one during the
    first paint."""
    out = _run("""
      const seen = [window.perf.phase()];
      window.perf.markUsable(); seen.push(window.perf.phase());
      window.perf.sseUpdate();   seen.push(window.perf.phase());
      __emit('longtask', [{ duration: 100 }]);
      window.perf.openJourney('dashboard_open');
      window.perf.closeJourney('dashboard_open');
      seen.push(window.perf.phase());
      __tick();
      console.log(JSON.stringify({ seen, beacons: beacons() }));
    """, sendBeacon=True, observers=["longtask"])
    assert out["seen"] == ["pre_paint", "post_usable", "sse_update",
                           "post_usable"], out
    assert out["beacons"][0]["phase"] == "sse_update", out


def test_region_walks_up_to_the_nearest_ancestor_that_names_one():
    out = _run("""
      const root = { getAttribute: n => (n === 'data-perf-region' ? 'inspector' : null),
                     parentElement: null };
      const mid = { getAttribute: () => null, parentElement: root };
      const deep = { getAttribute: () => null, parentElement: mid };
      console.log(JSON.stringify({
        deep: window.perf.region(deep),
        mid: window.perf.region(mid),
        nothing: window.perf.region(null),
        plain: window.perf.region({}),
      }));
    """, sendBeacon=True, observers=[])
    assert out == {"deep": "inspector", "mid": "inspector",
                   "nothing": "other", "plain": "other"}, out


# --- batching and transport ------------------------------------------

def test_a_burst_is_one_request_not_one_per_entry():
    """A page produces a burst of shifts during progressive load; one
    request per entry would be absurd."""
    out = _run("""
      const entries = [];
      for (let i = 0; i < 12; i++) {
        entries.push({ value: 0.01, hadRecentInput: false, sources: [] });
      }
      __emit('layout-shift', entries);
      const before = sent.length;
      __tick();
      console.log(JSON.stringify({ before, after: sent.length,
                                    beacons: beacons().length }));
    """, sendBeacon=True, observers=["layout-shift"])
    assert out["before"] == 0, "a beacon escaped the buffer before the flush"
    assert out["after"] == 1, out
    assert out["beacons"] == 12


def test_hiding_the_page_flushes_what_is_buffered():
    """The last journey of a session must not be lost to an unflushed
    buffer -- there is no later tick to flush it on."""
    out = _run("""
      __emit('longtask', [{ duration: 55 }]);
      const before = sent.length;
      document.visibilityState = 'hidden';
      document._l.visibilitychange();
      const after = sent.length;
      document._l.visibilitychange();
      console.log(JSON.stringify({ before, after, again: sent.length,
                                    beacons: beacons().length }));
    """, sendBeacon=True, observers=["longtask"])
    assert out == {"before": 0, "after": 1, "again": 1, "beacons": 1}, out


def test_becoming_visible_does_not_flush():
    """visibilitychange fires on the way back too; flushing then would
    send every beacon of a background/foreground cycle twice."""
    out = _run("""
      __emit('longtask', [{ duration: 55 }]);
      document._l.visibilitychange();
      console.log(JSON.stringify({ sent: sent.length, beacons: beacons().length }));
    """, sendBeacon=True, observers=["longtask"])
    assert out == {"sent": 0, "beacons": 0}, out


def test_pagehide_flushes_too():
    """A bfcache eviction fires pagehide, not visibilitychange."""
    out = _run("""
      __emit('longtask', [{ duration: 70 }]);
      window._l.pagehide();
      console.log(JSON.stringify({ sent: sent.length, beacons: beacons().length }));
    """, sendBeacon=True, observers=["longtask"])
    assert out == {"sent": 1, "beacons": 1}, out


def test_a_flush_of_an_empty_buffer_sends_nothing():
    """A page that measured nothing must not post an empty batch: the
    endpoint requires a non-empty list, so the request would be a 400 on
    every single page view."""
    out = _run("""
      document._l.visibilitychange();
      window._l.pagehide();
      __tick();
      console.log(JSON.stringify({ sent: sent.length, posted: posted.length }));
    """, sendBeacon=True, observers=["layout-shift"])
    assert out == {"sent": 0, "posted": 0}, out


def test_a_burst_wider_than_the_endpoint_cap_is_split_not_rejected():
    """The sink caps a request at 50 beacons; a wider burst is split, or
    the whole page view's telemetry is lost to one oversized batch."""
    out = _run("""
      const entries = [];
      for (let i = 0; i < 51; i++) {
        entries.push({ value: 0.01, hadRecentInput: false, sources: [] });
      }
      __emit('layout-shift', entries);
      __tick();
      const bs = batches();
      console.log(JSON.stringify({
        requests: sent.length,
        sizes: bs.map(b => b.length),
        total: bs.reduce((n, b) => n + b.length, 0),
      }));
    """, sendBeacon=True, observers=["layout-shift"])
    assert out["total"] == 51, out
    assert all(n <= 50 for n in out["sizes"]), out
    assert out["requests"] == len(out["sizes"]), out


def test_sendbeacon_is_preferred_when_present():
    out = _run("""
      __emit('longtask', [{ duration: 10 }]);
      __tick();
      console.log(JSON.stringify({ sent: sent.length, posted: posted.length,
                                    url: sent[0].url }));
    """, sendBeacon=True, observers=["longtask"])
    assert out["posted"] == 0, "sendBeacon was available and fetch was used anyway"
    assert out["url"] == "/api/metrics", out


def test_fetch_is_the_fallback_where_sendbeacon_is_absent():
    out = _run("""
      __emit('longtask', [{ duration: 10 }]);
      __tick();
      console.log(JSON.stringify({ sent: sent.length, init: posted[0].init,
                                    url: posted[0].url }));
    """, sendBeacon=False, observers=["longtask"])
    assert out["sent"] == 0
    init = out["init"]
    # Same-origin and same-session: the sink sits behind the auth
    # middleware, and keepalive is what lets the batch outlive the page.
    assert init["method"] == "POST", init
    assert init["credentials"] == "same-origin", init
    assert init["keepalive"] is True, init
    assert init["headers"]["Content-Type"] == "application/json", init
    assert json.loads(init["body"]) == {"beacons": [
        {"metric": "longtask", "part": "block", "value": 10,
         "phase": "pre_paint"}]}, init
    assert out["url"] == "/api/metrics", out


def test_a_batch_is_dropped_rather_than_thrown_when_nothing_can_send():
    """No sendBeacon and no fetch: the batch is dropped. It must never
    surface into a render."""
    out = _run("""
      __emit('longtask', [{ duration: 10 }]);
      __tick();
      document._l.visibilitychange();
      console.log(JSON.stringify({ ok: true }));
    """, sendBeacon=False, fetchOk=False, observers=["longtask"])
    assert out == {"ok": True}, out


def test_a_send_beacon_that_throws_falls_through_rather_than_losing_the_batch():
    out = _run("""
      global.navigator.sendBeacon = () => { throw new Error('quota'); };
      __emit('longtask', [{ duration: 10 }]);
      __tick();
      console.log(JSON.stringify({ posted: posted.length, ok: true }));
    """, sendBeacon=True, observers=["longtask"])
    assert out == {"posted": 1, "ok": True}, out


# --- the sign-in journey across the redirect -------------------------

# The marker the login page writes on submit, and the wall-clock epoch it
# corresponds to. The login document's origin is 1000000 + 500 - 1000000
# behind this one, so the elapsed journey is only knowable by bridging
# the two time origins through epoch ms.
_SIGNED_IN = {
    "claudit.signin.start": "400",
    "claudit.signin.origin": "999500",
}
_MALFORMED = {
    "claudit.signin.start": "not-a-number",
    "claudit.signin.origin": "999500",
}
_NAV = {"responseStart": 300, "startTime": 0}
# A navigation timing that claims more fetch than the whole journey took.
_LATE_NAV = {"responseStart": 1500, "startTime": 0}
# Both observed entry types supported.
_BOTH = ["layout-shift", "longtask"]


def test_the_signin_journey_closes_when_the_signed_in_page_is_usable():
    """The submit is stamped in the login page's time origin and the page
    arrives in a NEW one, so the elapsed journey is bridged through
    wall-clock epoch ms. The fetch part comes from the new document's own
    Navigation Timing -- a real measurement of the same navigation, not a
    zero and not a guess."""
    out = _run("""
      clock = 1500;                 // 1.5s into the signed-in document
      window.perf.markUsable();
      __tick();
      console.log(JSON.stringify({ beacons: beacons() }));
    """, sendBeacon=True, observers=[], session=_SIGNED_IN, nav=_NAV)
    rows = {r["part"]: r["value"] for r in out["beacons"]}
    assert all(r["metric"] == "signin" for r in out["beacons"]), out
    # Submitted at epoch 999500 + 400 = 999900; usable at 1000000 + 1500.
    assert rows["total"] == 1600, rows
    assert rows["fetch"] == 300, rows
    assert rows["client"] == 1300, rows


def test_the_start_marker_is_consumed_so_a_reload_is_not_another_journey():
    out = _run("""
      console.log(JSON.stringify({
        start: sessionStorage.getItem('claudit.signin.start'),
        origin: sessionStorage.getItem('claudit.signin.origin'),
      }));
    """, sendBeacon=True, observers=[])
    assert out == {"start": None, "origin": None}, (
        "the sign-in start marker survived the page that consumed it -- "
        "a reload would report the same journey again")


def test_a_navigation_entry_without_a_marker_is_not_a_signin_journey():
    """Navigation Timing exists on every navigation, including a plain
    page load. Adopting it unconditionally would report a sign-in the
    user never performed."""
    out = _run("""
      clock = 5000;
      window.perf.markUsable();
      __tick();
      console.log(JSON.stringify({ beacons: beacons() }));
    """, sendBeacon=True, observers=[], nav=_NAV)
    assert out["beacons"] == [], out


def test_a_malformed_marker_is_ignored_rather_than_reported():
    out = _run("""
      clock = 5000;
      window.perf.markUsable();
      __tick();
      console.log(JSON.stringify({ beacons: beacons() }));
    """, sendBeacon=True, observers=[], nav=_NAV, session=_MALFORMED)
    assert out["beacons"] == [], out


def test_a_signin_whose_fetch_outlives_its_total_reports_the_total_only():
    """The server CLAMPS an out-of-range value rather than refusing it,
    so a fetch part longer than its own total must be dropped at the
    source: clamped, it would be reported as a measurement."""
    out = _run("""
      clock = 900;                  // journey total 1000ms
      window.perf.markUsable();
      __tick();
      console.log(JSON.stringify({ beacons: beacons() }));
    """, sendBeacon=True, observers=[], session=_SIGNED_IN, nav=_LATE_NAV)
    rows = {r["part"]: r["value"] for r in out["beacons"]}
    assert rows == {"total": 1000}, (
        f"a fetch part longer than its total was reported: {rows}")


def test_a_signin_journey_named_by_hand_is_not_double_reported():
    """The marker adopts the journey at load; an app.jsx call site that
    opened one anyway must not produce a second sign-in reading."""
    out = _run("""
      window.perf.openJourney('signin');
      window.perf.markUsable();
      __tick();
      console.log(JSON.stringify({ beacons: beacons() }));
    """, sendBeacon=True, observers=[], session=_SIGNED_IN, nav=_NAV)
    metrics = [r["metric"] for r in out["beacons"]]
    assert metrics == ["signin"], out


# --- the shape the endpoint admits -----------------------------------

def test_every_beacon_the_module_emits_is_inside_the_contract():
    """metric x part is a CLOSED table and region/phase are closed sets.
    Anything outside is a 400 that takes the whole batch with it, so the
    walk runs over all five metrics at once."""
    allowed = {
        ("dashboard_open", "fetch"), ("dashboard_open", "client"),
        ("dashboard_open", "total"), ("inspector_open", "fetch"),
        ("inspector_open", "client"), ("inspector_open", "total"),
        ("signin", "fetch"), ("signin", "client"), ("signin", "total"),
        ("layout_shift", "shift"), ("longtask", "block"),
    }
    out = _run("""
      for (const name of %s) {
        window.perf.openJourney(name);
        window.perf.closeFetch(name);
        window.perf.closeJourney(name);
      }
      const mk = reg => ({ getAttribute: n => (n === 'data-perf-region' ? reg : null),
                           parentElement: null });
      __emit('layout-shift', [
        { value: 0.1, hadRecentInput: false, sources: [{node: mk('panel_grid')}] },
        { value: 0.2, hadRecentInput: false, sources: [{node: mk('inspector')}] },
        { value: 0.3, hadRecentInput: false, sources: [{node: mk('signin')}] },
      ]);
      __emit('longtask', [{ duration: 42 }, { duration: 43 }]);
      __tick();
      console.log(JSON.stringify({ beacons: beacons() }));
    """ % json.dumps(list(JOURNEYS)), sendBeacon=True, observers=_BOTH)
    assert out["beacons"], "no beacons at all -- the walk proves nothing"
    regions = {"panel_grid", "inspector", "signin", "other"}
    phases = {"pre_paint", "post_usable", "sse_update"}
    for row in out["beacons"]:
        assert (row["metric"], row["part"]) in allowed, (
            f"{row['metric']} x {row['part']} is not in the closed table")
        if row["metric"] in ("dashboard_open", "inspector_open", "signin"):
            assert "region" not in row and "phase" not in row, (
                "a journey beacon carried a region or phase: a 400")
        elif row["metric"] == "layout_shift":
            assert set(row) == {"metric", "part", "value", "region", "phase"}, row
            assert row["region"] in regions, row
            assert row["phase"] in phases, row
        else:
            assert set(row) == {"metric", "part", "value", "phase"}, (
                f"longtask must NOT carry a region: {sorted(row)}")
            assert row["phase"] in phases, row
        assert isinstance(row["value"], (int, float)), row
        assert row["value"] == row["value"], f"NaN value: {row}"
        assert row["value"] >= 0, row


def test_the_module_installs_itself_as_one_global():
    """The call sites in app.jsx are one line each; a typo'd name is a
    TypeError inside a render."""
    out = _run("""
      console.log(JSON.stringify({ api: Object.keys(window.perf).sort() }));
    """, sendBeacon=True, observers=[])
    assert out["api"] == [
        "closeFetch", "closeJourney", "markUsable", "openJourney",
        "phase", "region", "sseUpdate",
    ], out


def test_an_unknown_journey_name_is_never_reported():
    """The journey names are a closed set; a typo in a call site would
    otherwise become a beacon the sink rejects as a 400."""
    out = _run("""
      window.perf.openJourney('dashboard-open');
      window.perf.closeFetch('dashboard-open');
      window.perf.closeJourney('dashboard-open');
      __tick();
      console.log(JSON.stringify({ beacons: beacons() }));
    """, sendBeacon=True, observers=[])
    assert out["beacons"] == [], out
