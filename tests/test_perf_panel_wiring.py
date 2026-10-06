"""Source-level guards for the page-performance panel (issue #436).

Same boundary as `tests/test_panel_wiring.py`: node cannot parse JSX and
nothing here renders React, so a panel can pass every test while drawing
nothing -- or while drawing something that reads as a measurement it never
took. `tests/test_perf_js.py` covers the client that EMITS the beacons;
this file covers the panel that reads them back and the call sites that
wire them up, neither of which anything else in the suite can see.

Every assertion here was proven red before the code it guards existed: the
panel's against a missing `src/perf-panel.jsx`, the call sites' against an
`app.jsx` carrying none.

The panel is a READOUT, and that is what most of the first half pin. It
shows the percentiles `series` carries and never re-derives one in the
browser; it says so when `exact` is false, because on the rollup pass
`series` is an n-weighted blend of per-bucket percentiles and a panel
that cannot tell them apart would be drawing an approximation as a
measurement. It hides entirely when `series` is empty, so an unmeasured
range and a measured one cannot look the same. And a part the client
never measured renders as an em dash rather than a zero.

The second half pins the four journeys' call sites, because a journey
that is opened and never closed reports nothing, and one closed on mount
reports a page that rendered nothing.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PANEL = ROOT / "src" / "perf-panel.jsx"
APP = ROOT / "src" / "app.jsx"


# What an unmeasured part renders as. Named so the assertions below never
# have to spell the character literally.
EM_DASH = "\u2014"


def _strip_line_comments(src: str) -> str:
    """Drop `//` line comments so prose ABOUT a mistake is not read as the
    mistake. The lookbehind spares `https://`, the only mid-expression //
    here."""
    return re.sub(r"(?<![:'\"\w])//.*$", "", src, flags=re.M)


def _panel_src(name: str) -> str:
    """Just ONE component's body.

    Slice from the component to the next top-level `function`/`window.`,
    exactly as test_panel_wiring._panel_src does: searching the whole file
    is how a guard silently checks the wrong component when several of
    them define a handler with the same name.
    """
    src = _strip_line_comments(PANEL.read_text(encoding="utf-8"))
    start = src.index(f"function {name}(")
    nxt = re.search(r"^(?:function |window\.)", src[start + 1:], re.M)
    end = start + 1 + nxt.start() if nxt else len(src)
    return src[start:end]


def test_the_panel_file_defines_the_panel_it_registers():
    """The component and the global the app mounts must be the same
    name: app.jsx references `window.WebMetricsPanel`, and a rename that
    touched only one of them mounts `undefined` and renders nothing."""
    src = PANEL.read_text(encoding="utf-8")
    assert "function WebMetricsPanel(" in src, (
        "perf-panel.jsx no longer defines WebMetricsPanel")
    assert "window.WebMetricsPanel = WebMetricsPanel;" in src, (
        "perf-panel.jsx no longer registers the global app.jsx mounts")


def test_the_panel_hides_entirely_when_nothing_was_measured():
    """A telemetry panel that draws zeros for "we have never measured
    this" is exactly the failure the cost panels are already gated
    against, and an empty dashboard and an unmeasured one must not look
    the same."""
    src = _panel_src("WebMetricsPanel")
    assert "if (!series.length) return null;" in src, (
        "the panel renders with an empty series -- an unmeasured range "
        "would be indistinguishable from a measured one full of zeros")
    # ...and the emptiness test is on series, not on the response object:
    # a payload with buckets but no pooled rows is still unmeasured.
    assert re.search(r"const series = \(?.*\)?\.series\) \|\| \[\]", src) or \
        re.search(r"const series = .*\|\| \[\]", src), (
        "the panel must default a missing `series` key to [] so the "
        "empty-state guard cannot read it as undefined")


def test_the_readout_never_re_derives_a_percentile_in_the_browser():
    """The percentiles are computed over the population server-side and
    pooled there. `series` on the rollup pass is an n-weighted BLEND of
    per-bucket percentiles -- re-averaging it in the browser would
    invent a percentile nobody measured."""
    src = _strip_line_comments(PANEL.read_text(encoding="utf-8"))
    for banned in ("percentile(", "sort((a, b) => a - b)", ".p50s", ".p75s"):
        assert banned not in src, (
            f"the panel computes a percentile itself ({banned!r}); the "
            "values in `series` are already the measurement")


def test_the_panel_says_when_its_percentiles_are_approximate():
    """`exact` is true only on the live pass. On the rollup pass `series`
    is a blend, and a readout that cannot tell them apart is drawing an
    approximation as a measurement."""
    src = _panel_src("WebMetricsPanel")
    assert re.search(r"exact\b", src), (
        "the panel never reads `exact`, so it cannot distinguish a true "
        "percentile from an n-weighted blend")
    assert re.search(r"!exact\b", src), (
        "the panel never branches on exact being false")


def test_a_part_that_was_never_measured_renders_an_em_dash():
    """src/perf.js never sends a part it did not time, so a missing row
    is real information. Rendering it as 0 would read as a measurement."""
    src = _strip_line_comments(PANEL.read_text(encoding="utf-8"))
    assert EM_DASH in src, (
        "a missing series row must render an em dash, not a zero")
    # ...and the em dash must be what a MISSING row yields, not what a
    # measured zero yields: the condition reads the row, never the value.
    assert re.search(r"!row\s*\?\s*['\"]" + EM_DASH + r"['\"]", src), (
        "the em dash is not wired to the missing-row branch")
    assert not re.search(r"!row\)\s*return\s*['\"]0", src), (
        "a missing row renders a zero")


def test_the_panel_fetches_its_own_endpoint_same_origin():
    """The other self-fetching panels all carry `credentials:
    'same-origin'`; the sink sits behind the auth middleware."""
    src = _panel_src("WebMetricsPanel")
    assert "/api/web-metrics?range=" in src, (
        "the panel does not fetch /api/web-metrics")
    assert "credentials: 'same-origin'" in src, (
        "the panel's fetch drops the same-origin credentials every other "
        "panel sends -- the endpoint is session-gated")


def test_the_journey_rows_cover_all_three_journeys():
    """The endpoint's journey table is closed; a panel that names fewer
    than the sink accepts silently omits rows from the readout."""
    src = _panel_src("WebMetricsPanel")
    for name in ("dashboard_open", "inspector_open", "signin"):
        assert f"'{name}'" in src, f"the readout omits the {name} journey"


def test_the_split_that_separates_server_from_browser_is_shown():
    """The whole point of issue #436 item (1): a total that regressed
    while its client share did not is a different problem. Both parts
    must be read, from the rows themselves."""
    src = _panel_src("WebMetricsPanel")
    for part in ("'total'", "'fetch'", "'client'"):
        assert part in src, f"the journey readout never reads the {part} part"


def test_layout_shift_and_longtask_totals_are_summed_not_averaged():
    """A cumulative layout shift is a SUM of shift values and a blocking
    figure is a SUM of task durations -- neither is a percentile, and
    the panel must not sort them into one."""
    src = _panel_src("WebMetricsPanel")
    assert "layout_shift" in src and "longtask" in src, (
        "the panel reports neither layout stability nor main-thread "
        "blocking")
    assert re.search(r"\.reduce\(", src), (
        "the observed metrics are not summed -- a cumulative layout "
        "shift is a sum, not a mean")
    assert re.search(r"\.total\b", src), (
        "the panel reads the rows' summed `total`, not their percentiles")


def test_the_observed_metrics_are_broken_down_by_region_and_phase():
    """Item (2) of the issue is the breakdown: which part of the page
    shifted, and whether it was before first paint or after usable."""
    src = _panel_src("WebMetricsPanel")
    for key in ("region", "phase"):
        assert f"'{key}'" in src or f".{key}" in src, (
            f"the observed metrics carry no {key} breakdown")


def test_the_panel_calls_the_a11y_helper():
    """Every other chart panel takes its label/summary/description from
    the shared helper, whose id comes from useId so two instances cannot
    collide."""
    src = _panel_src("WebMetricsPanel")
    assert "window.useChartA11y(" in src, (
        "the panel skips useChartA11y, so it has no accessible label")
    assert "a11y.label" in src and "a11y.descId" in src, (
        "useChartA11y's return value is computed and then unused")


def test_the_panel_adds_no_css():
    """public/app.css is frozen for this issue; a panel that needed a new
    class would silently not be styled in the served page."""
    src = PANEL.read_text(encoding="utf-8")
    assert "app.css" not in src, "the panel references a stylesheet"
    css = (ROOT / "public" / "app.css").read_text(encoding="utf-8")
    for name in re.findall(r'className="([a-z0-9 _-]+)"', src):
        for cls in name.split():
            assert re.search(rf"\.{re.escape(cls)}\b", css), (
                f"the panel uses .{cls}, which app.css does not define -- "
                "app.css is frozen for this issue")


def test_the_app_mounts_the_panel_once_with_the_other_backend_panels():
    """It is a self-fetching panel: it goes out in parallel with
    /api/dashboard rather than waiting for it, like the four the
    Dashboard's own comment names."""
    src = _strip_line_comments(APP.read_text(encoding="utf-8"))
    assert src.count("<window.WebMetricsPanel") == 1, (
        "the panel is mounted more than once, or not at all")
    window = src[src.index("<window.WebMetricsPanel") - 260:
                 src.index("<window.WebMetricsPanel")]
    assert "backendOn" in window, (
        "the panel mounts with no backend guard -- there is no endpoint "
        "to fetch without one")
    for prop in ("project={activeProject}", "range={activeRange}",
                 "nonce={dashNonce}"):
        assert prop in src[src.index("<window.WebMetricsPanel"):
                           src.index("<window.WebMetricsPanel") + 260], (
            f"the panel is mounted without {prop}")


def test_the_panel_mounts_only_for_an_operator():
    """#629: the readout is operator-only, and an ordinary user must not
    even be offered the panel — the endpoint would refuse it anyway, but
    a page that fetches into a 403 shows an empty panel where telemetry
    should be. node parses no JSX, so the mount CONDITION is pinned here at
    source level, beside the guard above."""
    src = _strip_line_comments(APP.read_text(encoding="utf-8"))
    window = src[src.index("<window.WebMetricsPanel") - 260:
                 src.index("<window.WebMetricsPanel")]
    assert "window.IS_OPERATOR" in window, (
        "the panel mounts for everyone -- it is operator-only, and the "
        "server refuses the readout to anyone else")


def test_the_app_still_fetches_the_dashboard_exactly_once():
    """The wiring guards slice the one /api/dashboard fetch's effect;
    a second site would make the slice ambiguous."""
    src = _strip_line_comments(APP.read_text(encoding="utf-8"))
    assert src.count("/api/dashboard?range=") == 1


# --- the call sites ---------------------------------------------------
#
# A journey that is opened and never closed reports nothing at all, and
# one closed on mount reports a page that rendered nothing -- neither is
# catchable from the panel side, because the panel faithfully draws
# whatever the sink returns.

INDEX = ROOT / "public" / "index.html"


def _effect_for(marker: str, deps: str) -> str:
    """The effect around `marker`, comments stripped, up to its deps."""
    src = _strip_line_comments(APP.read_text(encoding="utf-8"))
    i = src.index(marker)
    start = src.rindex("useEffect(", 0, i)
    return src[start:src.index(deps, i) + len(deps)]


def test_the_dashboard_journey_brackets_the_dashboard_fetch():
    """openJourney sits above the request and closeFetch in its success
    path, after the supersede guard -- a superseded run must not close a
    journey the run that replaced it owns."""
    src = _strip_line_comments(APP.read_text(encoding="utf-8"))
    i = src.index("window.perf.openJourney('dashboard_open')")
    assert "window.dashboardFetch.load(" in src[i:i + 200], (
        "the dashboard journey opens AFTER the fetch it is timing")
    fetch = src[i:src.index(".catch(err =>", i)]
    guard = fetch.index("if (!run.isCurrent()) return;")
    close = fetch.index("window.perf.closeFetch('dashboard_open')")
    assert guard < close, (
        "closeFetch runs before the supersede guard, so an abandoned "
        "response ends the journey the live one is still waiting on")


def test_the_dashboard_journey_closes_on_the_first_data_bearing_render():
    """The dashboard mounts immediately so its self-fetching panels can
    fan out, long before any data exists. Closing on mount would report a
    page that rendered nothing; closing on a request that has merely
    STARTED would close the previous journey early. So the close is
    keyed on the response having landed."""
    effect = _effect_for(
        "window.perf.closeJourney('dashboard_open')",
        "[hasData, dashFetch]);")
    assert "if (dashFetch.status === window.dashboardFetch.LOADING) return;" in effect, (
        "the effect fires while the request is still STARTING, so every "
        "refetch closes the journey before its own data lands")
    assert "hasData && dashFetch.status === window.dashboardFetch.READY" in effect, (
        "the close is not gated on a data-bearing READY, so a journey ends "
        "on an empty render or a failed one over stale `synth`")
    assert "window.perf.markUsable()" in effect, (
        "the phase never leaves pre_paint -- every observed shift would "
        "be attributed to the first paint")


def test_the_phase_leaves_pre_paint_on_a_range_with_no_rows():
    """markUsable is about the request RESOLVING, not about data arriving.

    Gating it on `hasData` left a deploy with no data in pre-paint
    forever, and an adopted sign-in journey -- which closes on
    markUsable -- never closing at all. The fix is that the early return
    above keys on LOADING alone.
    """
    effect = _effect_for(
        "window.perf.closeJourney('dashboard_open')",
        "[hasData, dashFetch]);")
    before, _, usable = effect.partition("window.perf.markUsable()")
    assert usable, "the effect does not reach markUsable at all"
    assert "hasData" not in before, (
        "markUsable is gated on data existing, so an empty range never "
        "leaves pre-paint and an adopted sign-in journey never closes")


def test_the_inspector_journey_brackets_the_transcript_load():
    src = _strip_line_comments(APP.read_text(encoding="utf-8"))
    loader = src[src.index("makeTranscriptLoader({"):
                 src.index("onStart: () => {")]
    assert "window.perf.closeFetch('inspector_open')" in loader, (
        "the Inspector journey never records its network wait, so the "
        "split cannot tell a slow server from a slow parser")
    assert "fetchText: fetchTranscriptText," in loader, (
        "the loader stopped using the shared, checked transcript fetch")
    assert "window.perf.openJourney('inspector_open')" in src[src.index(
        "function loadFromBackend("):src.index("function loadFromBackend(") + 300], (
        "the Inspector journey does not open where the load is requested")


def test_the_inspector_journey_closes_after_its_early_return():
    """SessionView returns early when it holds no transcript. A hook
    added AFTER that return changes the hook count between renders and
    React refuses to render the component at all."""
    src = _strip_line_comments(APP.read_text(encoding="utf-8"))
    view = src[src.index("function SessionView("):
               src.index("function SessionHeader(")]
    guard = view.index("if (!tx) {")
    hook = view.index("window.perf.closeJourney('inspector_open')")
    assert hook < guard, (
        "the close's hook sits after SessionView's `if (!tx)` early "
        "return -- React throws 'rendered more hooks than during the "
        "previous render' the moment a transcript arrives")
    assert "useRef" in view[:hook] and "useEffect" in view[:hook], (
        "the close has no ref/effect guarding it above the early return")


def test_the_sse_tick_moves_the_phase():
    """A shift during the SSE-driven repaint is a different defect from
    one during the first paint, so `ingest_done` is where the phase
    moves."""
    src = _strip_line_comments(APP.read_text(encoding="utf-8"))
    handler = src[src.index("const onIngest = e => {"):]
    handler = handler[:handler.index("};")]
    assert "window.perf.sseUpdate()" in handler, (
        "ingest_done does not move the observed-metric phase")


def test_the_regions_the_panel_groups_by_are_actually_marked_up():
    """`perf.region()` walks up to `data-perf-region`. A region nothing
    carries means every observed metric is attributed to `other` and the
    panel's breakdown has one row."""
    src = _strip_line_comments(APP.read_text(encoding="utf-8"))
    # The project strip lives in src/picker.jsx since #774 extracted it;
    # both strips carry the region, wherever the file sits.
    picker_src = _strip_line_comments(
        (ROOT / "src" / "picker.jsx").read_text(encoding="utf-8"))
    assert 'data-perf-region="panel_grid"' in src, (
        "the panel grid carries no region, so its shifts are attributed "
        "to `other`")
    assert 'data-perf-region="inspector"' in src, (
        "the Inspector carries no region, so its shifts are attributed "
        "to `other`")
    assert 'data-perf-region="topbar"' in src, (
        "the top bar carries no region, so its shifts are attributed "
        "to `other` (#733)")
    assert (src.count('data-perf-region="project_picker"')
            + picker_src.count('data-perf-region="project_picker"')) == 2, (
        "the project picker strip mounts twice (range and project) and "
        "both need the region, or a shift in the unmarked one is "
        "attributed to `other` (#733)")


def test_the_client_and_the_panel_are_loaded_before_the_app():
    """index.html loads each /src file as a classic script, in order. A
    panel that loads after app.jsx mounts is undefined at the first
    render; a client that loads after the panels misses the first-paint
    entries its buffered observers exist to recover."""
    html = INDEX.read_text(encoding="utf-8")
    for name in ("/src/perf.js", "/src/panel-gating.js",
                 "/src/perf-panel.jsx", "/src/app.jsx"):
        assert f'"{name}"' in html, f"index.html never loads {name}"
    assert html.index("/src/perf.js") < html.index("/src/app.jsx"), (
        "the telemetry client loads after the app that calls it")
    assert html.index("/src/perf-panel.jsx") < html.index("/src/app.jsx"), (
        "the panel loads after the app that mounts it")
    assert html.index("/src/panel-gating.js") < html.index("/src/app.jsx"), (
        "the relocated panel gates load after the app that calls them")


def test_the_relocated_helpers_are_still_the_ones_the_tree_uses():
    """app.jsx read these two as bare top-level functions; they now live
    in a plain-JS module and every call site must go through the global,
    or `no-undef` fires in CI and the page throws at runtime."""
    app = _strip_line_comments(APP.read_text(encoding="utf-8"))
    gating = (ROOT / "src" / "panel-gating.js").read_text(encoding="utf-8")
    for name in ("tokenPanels", "hasSeries"):
        assert f"window.{name} = {name};" in gating, (
            f"panel-gating.js no longer publishes window.{name}")
        assert f"window.{name}(" in app, (
            f"app.jsx calls {name} bare -- it moved out of the file")


def test_the_panel_surfaces_the_window_the_answer_was_read_over():
    """`since` is in the payload for this line and nowhere else.

    The rollup holds only closed buckets, so a range wider than the raw
    table is answered over less than it asked for. The endpoint clamps and
    reports the window; if the panel drops it, a reader sees a 30-day label
    over 4 days of data with nothing saying so. The backend docstring names
    the panel as the thing that shows it, so this is that claim pinned.
    """
    src = _strip_line_comments(PANEL.read_text(encoding="utf-8"))
    assert "body.since" in src, (
        "the panel never reads `since`, so the window the answer was read "
        "over is never shown")
    assert "window from" in src, (
        "`since` is read but not rendered — the clamp is still invisible")


def test_the_panel_says_how_much_of_its_traffic_is_anonymous():
    """Disclosure, not exclusion — and a skewed panel must LOOK skewed.

    Every anonymous session shares one `user_id`, so a panel whose numbers
    are mostly one anonymous caller reads as a population when it is not.
    Guests stay IN the readout (this host is guest-heavy; blinding the panel
    to its own traffic is the worse failure) and the share is rendered, so
    the skew is visible rather than silently trusted.
    """
    src = _strip_line_comments(PANEL.read_text(encoding="utf-8"))
    assert "body.guests" in src, "the panel never reads the guest count"
    assert "body.beacons" in src, "the panel never reads the total"
    assert "from guests" in src, (
        "the guest share is computed but never shown, which is disclosure "
        "nobody can see")


def test_the_observed_blocks_are_labelled_as_sums():
    """A summed layout shift is not a CLS, and summed blocking is not a
    per-view time. Both totals are over every visit in the range, across
    every user, so the labels say summed."""
    src = _strip_line_comments(PANEL.read_text(encoding="utf-8"))
    assert "layout shift, summed" in src, (
        "the layout-shift block is labelled as a per-view instability score; "
        "it is a sum over every visit in the range")
    assert "main-thread blocking, summed" in src, (
        "the blocking block is labelled as main-thread blocking per view; it "
        "is a sum over every long task in the range")


def test_the_panel_does_not_send_a_project_the_endpoint_ignores():
    """A parameter the server drops is worse than none: the URL claims a
    narrowing the answer does not have."""
    src = _strip_line_comments(PANEL.read_text(encoding="utf-8"))
    assert "project=" not in src, (
        "the panel appends a project filter the endpoint silently ignores")
    assert "WebMetricsPanel({ range, nonce })" in src, (
        "the panel still takes a `project` prop, so the call site is still "
        "passing one the endpoint will not honour")
