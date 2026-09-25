"""Source-level guards for the issue #117 accessibility wiring.

Same boundary as test_panel_wiring.py: node cannot parse JSX and nothing
here renders React, so a panel can carry missing or broken accessibility
wiring while the whole suite stays green. These guards read the sources
directly and pin the three requirements:

1. the muted TEXT tokens meet WCAG AA (>= 4.5:1) on every surface muted
   text paints over (the hex tokens are parsed from app.css and the WCAG
   ratio computed in the test, so re-darkening a token fails the suite);
2. every chart panel <svg> carries an accessible name derived from the
   data the panel already holds, with a visually-hidden description
   where a data alternative adds value;
3. app.jsx carries the polite live region that announces the SSE
   ingest_done refetch, its text set only after the refetch completes,
   and never takes focus.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "public" / "app.css"
CHARTS = ROOT / "src" / "dashboard-charts.jsx"
EXTRA = ROOT / "src" / "dashboard-charts-extra.jsx"
CGV = ROOT / "src" / "context-growth-view.jsx"
APP = ROOT / "src" / "app.jsx"

_SVG_FILES = (CHARTS, EXTRA, CGV)

AA = 4.5


def _strip_line_comments(src: str) -> str:
    """Drop `//` line comments so prose ABOUT the wiring is not read as
    the wiring. The lookbehind spares `https://` (a colon precedes those
    slashes), the only // that shows up mid-expression here.
    """
    return re.sub(r"(?<![:'\"\w])//.*$", "", src, flags=re.M)


# -- Contrast ----------------------------------------------------------

def _hex_token(name: str) -> str:
    """The hex value app.css declares for a --token."""
    m = re.search(rf"^\s*--{name}:\s*(#[0-9a-fA-F]{{6}})\s*;",
                  CSS.read_text(encoding="utf-8"), re.M)
    assert m, f"no hex --{name} token in app.css"
    return m.group(1)


def _rel_lum(hex_color: str) -> float:
    """WCAG 2.x relative luminance of an #rrggbb colour."""
    h = hex_color.lstrip("#")
    vals = []
    for i in (0, 2, 4):
        c = int(h[i:i + 2], 16) / 255
        vals.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    return 0.2126 * vals[0] + 0.7152 * vals[1] + 0.0722 * vals[2]


def _ratio(fg: str, bg: str) -> float:
    hi, lo = sorted((_rel_lum(fg), _rel_lum(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def css_src() -> str:
    return CSS.read_text(encoding="utf-8")


def _surfaces() -> dict:
    """Every surface muted TEXT paints over, read from app.css.

    Enumerated from the rules that put a muted text token over a
    background: --bg (body, .muted, .trow-time/.trow-glyph, .pp-btn,
    .loading, .cache-view th, .ctx tables' th), the topbar gradient
    (.logo-sub), --bg-soft (.navbtn, .filename-pill, .dt-btn, .srow.shead,
    .chip/.fchip, .pp-count), --bg-card (.drop-sub and the inline-styled
    Token Breakdown / Cost by Agent Type card headers), --panel
    (.chart-tooltip-key), --panel-2 (.stat-label/.stat-unit/.stat-delta
    inside .dash-summary and .session-header, the lightest surface and so
    the binding constraint for --muted) and --code-bg (<code> inherits
    .muted in the sessions page footer).
    """
    surfaces = {name: _hex_token(name) for name in
                ("bg", "bg-soft", "bg-card", "panel", "panel-2", "code-bg")}
    m = re.search(r"linear-gradient\(to bottom,\s*(#[0-9a-fA-F]{6})",
                  css_src())
    assert m, "the topbar gradient's first stop is gone from app.css"
    surfaces["topbar-top"] = m.group(1)
    return surfaces


def test_muted_meets_aa_on_every_surface_muted_text_paints():
    muted = _hex_token("muted")
    for name, bg in _surfaces().items():
        r = _ratio(muted, bg)
        assert r >= AA, (
            f"--muted {muted} over --{name} computes {r:.2f}:1, below "
            f"the 4.5:1 AA text floor; re-lift the token or narrow where "
            f"it paints")
    # Upper sanity bound on the binding (lightest) surface: muted is a
    # dim STEP, and a value tuned far past the floor would be a
    # different token than the design describes.
    lightest = _hex_token("panel-2")
    assert _ratio(muted, lightest) <= 6.0, (
        "--muted has drifted far brighter than the AA floor needs; it "
        "is a dim step, not a foreground token")


def test_muted_stays_below_fg2_and_fg():
    """The hierarchy: muted is a dim step between the background and
    --fg-2, never brighter than --fg-2 or --fg on any surface.
    """
    muted, fg2, fg = (_hex_token(t) for t in ("muted", "fg-2", "fg"))
    for name, bg in _surfaces().items():
        m, f2, f = (_ratio(t, bg) for t in (muted, fg2, fg))
        assert m < f2 < f, (
            f"contrast hierarchy broken over --{name}: muted {m:.2f}, "
            f"fg-2 {f2:.2f}, fg {f:.2f}")


def test_fg2_meets_aa_where_it_paints_text():
    """.logout-btn/.open-btn paint --fg-2 over --bg-soft; .trow-one,
    .ctx-summary and the cache/ctx tables' first columns over --bg.
    """
    fg2 = _hex_token("fg-2")
    for name in ("bg", "bg-soft"):
        r = _ratio(fg2, _hex_token(name))
        assert r >= AA, (
            f"--fg-2 {fg2} over --{name} computes {r:.2f}:1, below AA")


def test_muted2_has_no_non_decorative_text_use():
    """--muted-2 stays below AA on every surface (2.45:1 over --bg,
    2.20:1 over --panel-2 at #4a4f6a) and that is the documented,
    deliberate shape: its only text renderings are decorative
    separators (the "/" prefix of .logo-sub and the slash between the
    +/- line-churn figures in app.jsx), which WCAG 1.4.3's
    incidental-text exception covers; everywhere else it is the
    .stat-label marker dot's fill (non-text). This guard fires if
    anything NEW paints --muted-2 as real text: recheck AA first.
    """
    for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css_src()):
        if "var(--muted-2)" in body:
            assert sel.strip().endswith("::before"), (
                f"a non-pseudo rule paints --muted-2: {sel.strip()!r} -- "
                f"muted-2 computes 2.20:1 on --panel-2; recheck AA before "
                f"widening its use")
    app = APP.read_text(encoding="utf-8")
    for line in app.splitlines():
        if "--muted-2" in line:
            assert re.search(
                r"color: 'var\(--muted-2\)'\s*\}\}>\s*/\s*</span>", line), (
                f"--muted-2 paints real text in app.jsx: {line.strip()!r} "
                f"-- only the decorative +/- separator may use it")


# -- Chart ARIA --------------------------------------------------------

def _svg_tags(path):
    """Every <svg ...> opening tag with its 1-based line number,
    JSX-aware: a tag ends at the first `>` outside {...} bindings and
    string literals (onMouseLeave={() => setTip(null)} contains a bare
    `>`).
    """
    src = _strip_line_comments(path.read_text(encoding="utf-8"))
    out = []
    for m in re.finditer(r"<svg\b", src):
        i = m.start()
        depth = 0
        quote = None
        while i < len(src):
            ch = src[i]
            if quote:
                if ch == quote:
                    quote = None
            elif ch in "'\"":
                quote = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            elif ch == ">" and depth == 0:
                break
            i += 1
        out.append((src.count("\n", 0, m.start()) + 1, src[m.start():i + 1]))
    return out


def test_every_chart_svg_is_named_or_explicitly_hidden():
    """role="img" plus a data-bound aria-label on every chart panel
    svg; the heatmap's gradient legend bar is the one explicitly-hidden
    exception (its 0/max labels are real text beside it).
    """
    total = hidden = named = 0
    for path in _SVG_FILES:
        for line, tag in _svg_tags(path):
            total += 1
            if "aria-hidden" in tag:
                hidden += 1
                assert 'data-panel="Activity Heatmap' in tag and "legend" in tag, (
                    f"{path.name}:{line}: the only hidden svg must be "
                    f"the heatmap legend")
                continue
            named += 1
            assert 'role="img"' in tag, (
                f"{path.name}:{line}: chart svg carries no role=\"img\"")
            assert "aria-label={" in tag, (
                f"{path.name}:{line}: aria-label is not a data binding, "
                f"so it cannot follow the panel's data")
    assert total >= 15, (
        f"only {total} <svg> tags found -- the guard would pass "
        f"vacuously")
    assert hidden <= 1, (
        f"{hidden} svgs are aria-hidden -- only the heatmap legend may be")
    assert named >= 13, (
        f"only {named} named chart svgs -- the guard would pass "
        f"vacuously")
    assert total == named + hidden


def test_describedby_sites_have_a_hidden_summary_element():
    """Each aria-describedby binding must point at a visually-hidden
    <span className="sr-only"> whose id is bound to the SAME expression,
    rendered beside that svg. A describedby that names nothing (or names
    another panel's summary) describes the wrong panel.
    """
    sites = 0
    for path in _SVG_FILES:
        src = _strip_line_comments(path.read_text(encoding="utf-8"))
        for line, tag in _svg_tags(path):
            m = re.search(r"aria-describedby=\{([^}]+)\}", tag)
            if not m:
                continue
            sites += 1
            expr = m.group(1).strip()
            assert expr != "undefined", (
                f"{path.name}:{line}: aria-describedby=undefined binds "
                f"nothing; the panel lost its summary")
            span_re = re.compile(
                r'<span className="sr-only" id=\{' + re.escape(expr) + r'\}>')
            assert span_re.search(src), (
                f"{path.name}:{line}: aria-describedby={{{expr}}} has no "
                f'<span className="sr-only" id={{{expr}}}> summary beside '
                f"it")
    assert sites >= 8, (
        f"only {sites} aria-describedby sites -- the guard would pass "
        f"vacuously")


def test_labels_are_bound_not_stale_literals():
    """A literal label cannot follow the data, so no chart svg may carry
    aria-label="..." -- every label is a {binding} evaluated at render.
    """
    for path in _SVG_FILES:
        for line, tag in _svg_tags(path):
            assert 'aria-label="' not in tag, (
                f"{path.name}:{line}: a literal aria-label goes stale; "
                f'bind aria-label={{...}} to the panel data')


def test_description_ids_come_from_useid_not_from_titles():
    """ContextSubPanel and ToolErrorSubPanel mount once per model and
    TimeSeriesPanel/HBar once per metric, so a title-derived id collides
    the second time a family mounts two instances; the summary id must
    come from React.useId() via the shared helper.
    """
    src = _strip_line_comments(CHARTS.read_text(encoding="utf-8"))
    m = re.search(r"function useChartA11y\([^)]*\) \{(.*?)\n\}", src, re.S)
    assert m, "the useChartA11y helper is missing from dashboard-charts.jsx"
    body = m.group(1)
    assert "React.useId()" in body, (
        "useChartA11y derives the summary id from something collidable; "
        "use React.useId()")
    assert "descText: description || null" in body, (
        "useChartA11y must expose descText so call sites render the "
        "hidden summary beside the svg")


def test_helper_is_reachable_where_scripts_load_out_of_order():
    """index.html loads context-growth-view.jsx BEFORE dashboard-charts
    .jsx, so ContextChart must reach the shared helper through window at
    render time (a bare reference resolves at script-parse time, before
    the helper's file has loaded).
    """
    charts = CHARTS.read_text(encoding="utf-8")
    assert "window.useChartA11y = useChartA11y;" in charts
    html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    assert re.search(
        r'<script[^>]*src="/src/context-growth-view\.jsx"[^>]*>'
        r'.*?<script[^>]*src="/src/dashboard-charts\.jsx"',
        html, re.S), (
        "context-growth-view.jsx no longer loads before dashboard-charts"
        ".jsx; the window.useChartA11y rationale may be stale")
    cgv = CGV.read_text(encoding="utf-8")
    assert "window.useChartA11y(" in cgv, (
        "ContextChart must call window.useChartA11y(...) -- its file "
        "loads before the helper's file")


# -- Live region -------------------------------------------------------

REGION_RE = re.compile(
    r'<div aria-live="polite" aria-atomic="true" className="sr-only">')


def test_live_region_exists_outside_the_panel_tree():
    """The region is part of App's own render, not inside a panel:
    panels re-render and remount on every refetch, and an unmounted
    region cannot announce. Polite and atomic; no role="alert" and no
    focus() anywhere in the file.
    """
    src = APP.read_text(encoding="utf-8")
    assert REGION_RE.search(src), (
        'no <div aria-live="polite" aria-atomic="true" '
        'className="sr-only"> live region in app.jsx')
    assert 'role="alert"' not in src
    assert ".focus(" not in src
    start = src.index("function App(")
    end_m = re.search(r"^\}", src[start:], re.M)
    assert end_m is not None, "function App( has no closing brace"
    app_fn = src[start:end_m.start() + start]
    assert REGION_RE.search(app_fn), (
        "the live region left App's own render; an unmounted region "
        "cannot announce")
    assert '<div className="app-root">' in app_fn


def test_live_region_announces_only_after_the_refetch_lands():
    """The dashboard refetch effect is where the announcement is set;
    the SSE handler only records the pending what-changed line and bumps
    the refetch nonce. Announcing on the event itself would describe a
    refresh that has not landed yet.
    """
    src = _strip_line_comments(APP.read_text(encoding="utf-8"))
    i = src.index("/api/dashboard?range=")
    start = src.rindex("useEffect(", 0, i)
    dep = "[backendOn, activeProject, activeRange, dashNonce]);"
    end = src.index(dep, i) + len(dep)
    effect = src[start:end]
    assert "setBackendDash(b)" in effect, (
        "the dashboard refetch effect moved; relocate this guard with it")
    assert effect.index("setBackendDash(b)") < effect.index("refreshRef"), (
        "the announcement fires before the refetch's data lands")
    j = src.index("addEventListener('ingest_done', onIngest)")
    sse_start = src.rindex("useEffect(", 0, j)
    sse_dep = "}, [backendOn]);"
    sse_end = src.index(sse_dep, j) + len(sse_dep)
    sse = src[sse_start:sse_end]
    assert "refreshRef.current = ingestChangeSummary(e)" in sse, (
        "the SSE handler must record the pending what-changed line")
    assert "setDashNonce(n => n + 1)" in sse
    assert "setRefreshMsg" not in sse, (
        "the SSE handler must not set the announcement itself -- that "
        "would describe a refresh that has not landed")


def test_ingest_change_summary_degrades_on_unknown_payloads():
    """The payload is JSON the backend owns, but the reader must not
    throw on a shape it cannot read: the announcement then carries no
    what-changed line instead of crashing the SSE handler.
    """
    src = APP.read_text(encoding="utf-8")
    m = re.search(r"function ingestChangeSummary\(e\) \{(.*?)\n\}", src, re.S)
    assert m, "the ingestChangeSummary helper is missing from app.jsx"
    body = m.group(1)
    assert "try {" in body and "catch" in body
    assert "inserted" in body and "reparsed" in body and "deleted" in body


def test_sr_only_hides_visually_but_not_from_readers():
    """.sr-only is the shared visually-hidden class used by both the
    chart descriptions and the live region.
    """
    body_m = re.search(r"\.sr-only\s*\{([^}]*)\}", css_src())
    assert body_m, "no .sr-only rule in app.css"
    for prop in ("position: absolute", "width: 1px", "height: 1px",
                 "overflow: hidden", "clip:"):
        assert prop in body_m.group(1), (
            f".sr-only lost {prop!r} -- it must hide visually only")


def test_sr_only_spans_exist_in_the_chart_files():
    sites = 0
    for path in _SVG_FILES:
        src = path.read_text(encoding="utf-8")
        sites += len(re.findall(r'<span className="sr-only"', src))
    assert sites >= 8, (
        f"only {sites} sr-only spans across the chart files -- the "
        f"guard would pass vacuously")
