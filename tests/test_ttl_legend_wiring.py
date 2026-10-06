"""Source-level pins for the Prompt-Cache TTL Split legend (issue #633).

The legend sat at translate(padL + 8, padT + 12) -- INSIDE the plot
rectangle, across the bars it explains -- and ran 8.8px past the panel's
right edge at a 320px viewport. The rendered half is asserted by the
browser guard (scripts/ci/panel_layout.mjs); this module is the
source-level half, which fails on the revert before any browser runs.
The panel itself moved to src/cache-ttl-panel.jsx in the same fix: the
module-size ratchet's committed entry for dashboard-charts-extra.jsx
never rises, so the code the fix adds moved out instead of growing a
file already far over the ceiling.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHARTS = ROOT / "src" / "dashboard-charts.jsx"
EXTRA = ROOT / "src" / "dashboard-charts-extra.jsx"
TTL = ROOT / "src" / "cache-ttl-panel.jsx"


def _strip_line_comments(src: str) -> str:
    """Drop `//` line comments so prose ABOUT a mistake is not read as the
    mistake. The lookbehind spares `https://` (a colon precedes those
    slashes), which is the only // that shows up mid-expression here.
    """
    return re.sub(r"(?<![:'\"\w])//.*$", "", src, flags=re.M)


def _ttl_module() -> Path:
    """The one module that defines CacheTTLPanel. Since #633 it is its own
    file: the module-size ratchet's committed entry for
    dashboard-charts-extra.jsx never rises, so the code the fix adds moved
    out instead of growing a file that is already far over the ceiling."""
    hits = [p for p in (CHARTS, EXTRA, TTL) if p.exists() and re.search(
        r"\bfunction CacheTTLPanel\b", p.read_text(encoding="utf-8"))]
    assert len(hits) == 1, (
        f"CacheTTLPanel must be defined in exactly one module, found "
        f"{[p.name for p in hits]}")
    return hits[0]


def _ttl_src() -> str:
    return _strip_line_comments(_ttl_module().read_text(encoding="utf-8"))


def test_the_page_loads_the_module_that_defines_the_ttl_panel():
    """The panel moved files in #633. A script tag that did not move with
    it would leave window.CacheTTLPanel undefined and the panel silently
    absent -- nothing else in the suite would notice."""
    index = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    assert f"/src/{_ttl_module().name}" in index, (
        f"index.html does not load /src/{_ttl_module().name}, where "
        "CacheTTLPanel is defined")


def test_the_ttl_legend_anchors_below_the_share_strip_not_in_the_plot():
    """#633: the legend sat at translate(padL + 8, padT + 12) -- INSIDE the
    plot rectangle, across the bars it explains -- and ran 8.8px past the
    panel's right edge at a 320px viewport. The rendered-layout guard
    (scripts/ci/panel_layout.mjs) reads the resulting boxes; this pin is
    the source-level half, which fails on the revert before any browser
    runs."""
    m = re.search(
        r"data-role=\"legend\"[^>]*transform=\{`translate\(\$\{padL[^,]*,"
        r"\s*\$\{(\w+)", _ttl_src())
    assert m, "the TTL legend carries no padL-anchored transform at all"
    assert m.group(1) == "shareBot", (
        f"the TTL legend anchors to {m.group(1)!r} -- back inside the "
        "plot, over the bars (#633)")


def test_the_ttl_legend_band_is_sized_from_its_row_count():
    """The issue's suggested shape: the band below the share strip is
    bought per legend row, not from a fixed padding, so a wrapped row
    claims vertical space instead of escaping the panel."""
    src = _ttl_src()
    assert re.search(r"padB = 36 \+ legRows\.length \* LEG_ROW_H", src), (
        "padB is a fixed padding again -- the legend band must grow with "
        "the legend's row count")
    assert "legRows.push([])" in src, (
        "the legend never wraps, so the row-count-sized band is dead "
        "arithmetic and a narrow viewport overflows instead")


def test_the_ttl_hover_hit_tests_in_the_svg_frame():
    """The panel's hit test reads the cursor in the frame its bounds are
    expressed in (#696): padT/shareBot are svg coordinates and the
    container's rect carries the panel's 1px border, so comparing across
    the frames left the share strip's bottom row dead and parked a live
    1px band over the title area. The tip still positions in container
    coordinates (its offsetParent) -- the svg-frame shape the family's
    other panels took in #645."""
    src = _strip_line_comments(_ttl_module().read_text(encoding="utf-8"))
    m = re.search(r"function onMove\(e\) \{(.*?)\n  \}", src, re.S)
    assert m, "could not locate the panel's hover handler"
    body = m.group(1)
    assert ("svgRef.current.getBoundingClientRect()" in body
            and "sy < padT || sy > shareBot" in body), (
        "hover must hit-test and guard in the svg frame (#696)")
    assert "ref.current.getBoundingClientRect()" in body, (
        "the tooltip must still position in container coordinates")
    assert "ref={svgRef}" in src, "the svg must carry the hit-test frame's ref"
    assert "onMouseMove={onMove}" in src and "onMouseLeave" in src
