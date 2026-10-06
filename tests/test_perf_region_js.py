"""Layout-shift REGION attribution in src/perf.js (node-driven).

The region family of `tests/test_perf_js.py`, plus the #643 all-sources
walk. The module reads the same faked browser globals the parent harness
builds (`_run` there stands in for `performance`,
`PerformanceObserver`, `navigator`, `sessionStorage` and the timers), so
every assertion here runs against the real shipped client. Split from
the parent file for the size ratchet: the region family grew past the
space that file had left.
"""
from __future__ import annotations

from test_perf_js import _run


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


def test_region_falls_back_to_the_nearest_data_panel():
    """#647: only the grid and the Inspector carry `data-perf-region`,
    so a live node inside one panel — under no named region — landed in
    `other`, and `other` is what most production shifts carried. The
    nearest `[data-panel]` ancestor is the attribution the page already
    names; in the sink's closed vocabulary it is `panel_grid`. The
    region walk keeps precedence, and the fallback widens nothing it
    cannot see: a node outside every panel keeps `other`."""
    out = _run("""
      const mk = (attrs, parent) => ({
        getAttribute: n => (n in attrs ? attrs[n] : null), parentElement: parent });
      const panel = mk({ 'data-panel': 'Cost by Model' }, null);
      const between = mk({ 'data-panel': 'Cost by Model' },
        mk({ 'data-perf-region': 'inspector' }, null));
      console.log(JSON.stringify({
        inner: window.perf.region(mk({}, panel)),
        own: window.perf.region(panel),
        precedence: window.perf.region(mk({}, between)),
        bare: window.perf.region(mk({}, null)),
      }));
    """, sendBeacon=True, observers=[])
    assert out == {"inner": "panel_grid", "own": "panel_grid",
                   "precedence": "inspector", "bare": "other"}, out


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


def test_attribution_walks_every_source_before_other():
    """#643: one Layout Instability entry names SEVERAL moved nodes, and
    the FIRST one does not always resolve — the production 0.697 shifts
    reported `other` because sources[0] was a bare section wrapper while
    the panel it pushed around moved as a later source. The attribution
    must walk every source and take the first that names a region, so a
    shift lands `other` only when no source sits inside one."""
    out = _run("""
      const bare = { getAttribute: () => null, parentElement: null };
      const panelled = {
        getAttribute: n => (n === 'data-perf-region' ? 'panel_grid' : null),
        parentElement: null,
      };
      __emit('layout-shift', [
        { value: 0.697, hadRecentInput: false,
          sources: [{node: bare}, {node: panelled}] },
      ]);
      __tick();
      console.log(JSON.stringify({ beacons: beacons() }));
    """, sendBeacon=True, observers=["layout-shift"])
    assert out["beacons"] == [{"metric": "layout_shift", "part": "shift",
                               "value": 0.697, "region": "panel_grid",
                               "phase": "pre_paint"}], out


def test_attribution_keeps_source_order():
    """The walk stops at the FIRST source that resolves: a named region
    on a later source does not override an earlier source's own
    attribution — `other` on a source is a miss, not a vote."""
    out = _run("""
      const bare = { getAttribute: () => null, parentElement: null };
      const inGrid = {
        getAttribute: n => (n === 'data-perf-region' ? 'panel_grid' : null),
        parentElement: null };
      const inspector = {
        getAttribute: n => (n === 'data-perf-region' ? 'inspector' : null),
        parentElement: null };
      __emit('layout-shift', [
        { value: 0.1, hadRecentInput: false,
          sources: [{node: inGrid}, {node: inspector}] },
      ]);
      __tick();
      console.log(JSON.stringify({ beacons: beacons() }));
    """, sendBeacon=True, observers=["layout-shift"])
    assert out["beacons"][0]["region"] == "panel_grid", out


def test_a_detached_first_source_does_not_stop_attribution():
    """A detached node resolves like any other — a detached tree keeps
    its parent chain — and a source that resolves to nothing does not
    end the walk: the attached source behind it still names the region."""
    out = _run("""
      const orphan = { getAttribute: () => null, parentElement: null };
      const attached = {
        getAttribute: n => (n === 'data-perf-region' ? 'inspector' : null),
        parentElement: null };
      __emit('layout-shift', [
        { value: 0.2, hadRecentInput: false,
          sources: [{node: orphan}, {node: attached}] },
      ]);
      __tick();
      console.log(JSON.stringify({ beacons: beacons() }));
    """, sendBeacon=True, observers=["layout-shift"])
    assert out["beacons"][0]["region"] == "inspector", out
