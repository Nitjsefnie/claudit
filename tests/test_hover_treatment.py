"""Source-level pins for issue #697: the bar panels' ONE hover treatment
and the crosshair's snap.

The suite cannot render React (node parses no JSX and nothing here
renders it), so both properties are pinned the way test_panel_wiring.py
pins its wiring shapes: against the source text, per panel.

The one treatment is TimeSeriesPanel's dim field -- bars rest at 0.3
and the hovered bar lifts to 0.85 -- already carried by Cost by Context
Size (through its module constants) and pinned there by test_panel_wiring
.py. These guards extend it to the whole bar family: HBar, VBar and the
Prompt-Cache TTL Split panel, which brightened to full opacity under a
white hover stroke instead.

The crosshair guards pin WHERE the dashed vertical is drawn: at the x of
the datum the tooltip describes -- the bucket/bar centre on a bucketed
panel, the interpolated point on a line panel -- never at the raw
cursor. Cursor-following is the TOOLTIP's job; a crosshair at tip.x
describes nothing (issue #697).
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHARTS = ROOT / "src" / "dashboard-charts.jsx"
EXTRA = ROOT / "src" / "dashboard-charts-extra.jsx"
TTL = ROOT / "src" / "cache-ttl-panel.jsx"

# The treatment's two levels, as the reference spells them.
FIELD = "fillOpacity={isHover ? 0.85 : 0.3}"
# The other family's marker -- the brighten-to-1.0-with-a-white-stroke
# treatment this issue retires. Banned on every bar mark.
HOVER_STROKE = "stroke={isHover ? '#fff' : 'none'}"


def _strip_line_comments(src: str) -> str:
    """Drop `//` line comments so prose ABOUT a mistake is not read as the
    mistake (same stripper test_panel_wiring.py uses; spares `https://`)."""
    return re.sub(r"(?<![:'\"\w])//.*$", "", src, flags=re.M)


def _charts_panel(name: str) -> str:
    """One component's body out of dashboard-charts.jsx: slice from the
    component to the next top-level `function`/`window.` line."""
    src = _strip_line_comments(CHARTS.read_text(encoding="utf-8"))
    start = src.index(f"function {name}(")
    nxt = re.search(r"^(?:function |window\.)", src[start + 1:], re.M)
    end = start + 1 + nxt.start() if nxt else len(src)
    return src[start:end]


def _extra_panel(name: str) -> str:
    """One component's body out of dashboard-charts-extra.jsx."""
    src = _strip_line_comments(EXTRA.read_text(encoding="utf-8"))
    start = src.index(f"function {name}(")
    nxt = re.search(r"^(?:function |window\.)", src[start + 1:], re.M)
    end = start + 1 + nxt.start() if nxt else len(src)
    return src[start:end]


# Every dashed-vertical crosshair in the scanned panels carries
# strokeDasharray="2,3"; nothing else in these panels does.
_CROSSHAIR = re.compile(r"<line ([^>]*strokeDasharray=\"2,3\"[^>]*)/>", re.S)


def _crosshair_x1(src: str, panel: str) -> str:
    """The one crosshair's x1 expression, failing loudly when the panel
    grew a second dashed vertical (the guard would silently check only
    the first)."""
    hits = _CROSSHAIR.findall(src)
    assert len(hits) == 1, (
        f"{panel}: expected exactly one dashed crosshair, found {len(hits)} "
        "-- another dashed vertical joined the panel; scope this guard to "
        "the real crosshair before trusting it")
    m = re.search(r"x1=\{([^}]*)\}", hits[0])
    assert m, f"{panel}: crosshair carries no x1"
    return m.group(1).strip()


def test_every_bar_panel_carries_the_one_field_treatment():
    """HBar, VBar and CacheTTLPanel join TimeSeriesPanel's dim-field
    treatment: bars rest at 0.3, the hovered bar lifts to 0.85. Cost by
    Context Size already spells it through its BAR_OPACITY constants
    (pinned by test_panel_wiring.py). No bar mark keys a white stroke on
    hover -- the white ring belongs to the cumulative LINE's halo, not
    to a bar."""
    tsp = _charts_panel("TimeSeriesPanel")
    hbar = _charts_panel("HBar")
    vbar = _charts_panel("VBar")
    ttl = _strip_line_comments(TTL.read_text(encoding="utf-8"))
    cbc = _extra_panel("CostByContextPanel")

    # The reference itself, so a treatment move is caught at its source.
    assert FIELD in tsp, "TimeSeriesPanel is the reference treatment; reread it"
    assert FIELD in hbar, "HBar bars must rest at 0.3 and lift to 0.85 on hover"
    assert FIELD in vbar, "VBar bars must rest at 0.3 and lift to 0.85 on hover"
    # TTL: same levels, with the peak bin resting pre-lifted.
    assert ttl.count("fillOpacity={isHover || isPeak ? 0.85 : 0.3}") == 2, (
        "both stacked TTL segments must carry the field treatment "
        "(the peak bin rests at 0.85)")
    # Cost by Context spells the levels through its module constants;
    # their values are pinned by test_panel_wiring.py.
    assert "BAR_OPACITY_HOVER : BAR_OPACITY" in cbc

    for panel, src in (("TimeSeriesPanel", tsp), ("HBar", hbar),
                       ("VBar", vbar), ("CacheTTLPanel", ttl),
                       ("CostByContextPanel", cbc)):
        assert HOVER_STROKE not in src, (
            f"{panel} keys a white stroke on hover -- the brighten-to-1.0 "
            f"treatment retired by #697; bars mark hover by the 0.85 lift "
            f"alone")


def test_crosshairs_snap_to_the_hovered_datum_not_the_cursor():
    """Wherever a crosshair is drawn it marks the x of the datum the
    tooltip describes: the bucket/bar centre on a bucketed panel, the
    interpolated point on a line panel. A crosshair at tip.x is the raw
    cursor and describes nothing (#697)."""
    tsp = _charts_panel("TimeSeriesPanel")
    ttl = _strip_line_comments(TTL.read_text(encoding="utf-8"))
    cbc = _extra_panel("CostByContextPanel")
    rs = _extra_panel("ResponseSizesPanel")
    tu = _extra_panel("ToolUsagePanel")
    rl = _extra_panel("ReplyLatencyPanel")

    # Bucketed panels: the crosshair sits on the hovered bucket/bar centre.
    assert _crosshair_x1(tsp, "TimeSeriesPanel") == "tip.cx"
    assert _crosshair_x1(cbc, "CostByContextPanel") == "padL + tip.idx * bw + bw / 2"
    assert _crosshair_x1(ttl, "CacheTTLPanel") == "xScale(bins[tip.idx].start) + barW / 2"
    # ...and the reference's cx is the hovered bar's centre, not the cursor.
    assert "cx: bar.x + bar.width / 2," in tsp

    # Line panels snap to the interpolated point (the outlier branch to
    # the outlier dot). The tooltip keeps following the cursor; the
    # crosshair does not.
    for name, src, snaps in (
        ("ResponseSizesPanel", rs, ["cx: xScale(best.ts),"]),
        ("ToolUsagePanel", tu, [("cx: xScale(grid.ts[bIdx]),", 2)]),
        ("ReplyLatencyPanel", rl,
         ["cx: xScale(best.ts),", "cx: xScale(bestO.tsMs),"]),
    ):
        assert _crosshair_x1(src, name) == "tip.cx", (
            f"{name}: the crosshair must read the snapped cx, not the cursor")
        for snap in snaps:
            if isinstance(snap, tuple):
                # Both branches of this panel's hover handler snap: the
                # "Other" band and a promoted tool. One branch alone
                # satisfying a membership pin leaves the other branch's
                # crosshair at x=0.
                expr, n = snap
                assert src.count(expr) == n, (
                    f"{name}: expected {expr!r} at exactly {n} site(s), "
                    f"found {src.count(expr)}")
            else:
                assert snap in src, (
                    f"{name}: no snap computation for {snap!r} -- the tip must "
                    f"carry the datum's x the crosshair draws at")
