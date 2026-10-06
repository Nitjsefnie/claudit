"""Source-level pins for the Activity Heatmap's responsive layout (#636, #637).

The grid's cell width carried a floor of 8 that engaged at a 320px
viewport -- the grid was pinned at 304px against a ~292px panel and the
plot and the axis labels escaped the panel's right edge -- and the
footer legend's svg drew its gradient rect at a fixed 120 user units
while the flex row around it shrank the svg element itself, so the bar
overflowed the shrunk element. The rendered half is asserted by the
browser guard (scripts/ci/panel_layout.mjs), whose FILED ledger this
fix empties; this module is the source-level half, which fails on the
revert before any browser runs.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXTRA = ROOT / "src" / "dashboard-charts-extra.jsx"


def _geometry_block() -> str:
    """The heatmap's geometry lines, verbatim: `const padL` .. `sumRowY`.

    The panel's own arithmetic is evaluated rather than restated: a test
    that recomputes the width its own way would stay green beside a
    panel that computes something else.
    """
    src = EXTRA.read_text(encoding="utf-8")
    m = re.search(
        r"^  const padL = [^\n]*\n"
        r"  const SUM_GAP = [^\n]*\n"
        r"  const cellW = [^\n]*\n"
        r"  const cellH = [^\n]*\n"
        r"  const h = [^\n]*\n"
        r"  const sumColX = [^\n]*\n"
        r"  const sumRowY = [^\n]*\n",
        src, re.M)
    assert m, (
        "the heatmap's geometry block moved or was renamed; the pins "
        "below would be checking arithmetic the panel no longer runs")
    return m.group(0).rstrip("\n")


def _geometry_at(widths: list[int]) -> dict:
    block = _geometry_block()
    script = (
        "const out = {};\n"
        "for (const w of " + json.dumps(widths) + ") {\n"
        + block
        + "\n  out[w] = { cellW,"
          " gridW: padL + 25 * cellW + 23 * gap + SUM_GAP + padR };\n"
        "}\n"
        "console.log(JSON.stringify(out));"
    )
    proc = subprocess.run(
        ["node"], input=script, capture_output=True, text=True, timeout=60,
        check=False, encoding="utf-8", errors="strict",
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_the_grid_fits_the_measured_width_at_every_panel_width():
    """#636: the floor of 8 engaged at a 320px viewport, pinning the drawn
    grid at 312px (25 cells at the floor) against a ~292px panel. cellW may
    only fall back where the pads and gaps alone outrun 25 cells -- below
    the ~162px panel no real viewport reaches -- so the grid fits every
    real width by construction."""
    widths = [240, 292, 320, 347, 375, 600, 1200]
    got = _geometry_at(widths)
    for w, g in got.items():
        assert g["cellW"] > 0, f"a non-positive cell width at {w}px"
        assert g["gridW"] <= int(w), (
            f"the grid is {g['gridW']}px wide against a {w}px panel -- "
            "cells no longer scale to the measured width (#636)")


def test_the_legend_gradient_is_drawn_in_units_that_scale():
    """#637: the legend svg carried no viewBox, so the flex row could
    shrink the svg element while the gradient rect inside kept its fixed
    120px width -- the bar overflowed the shrunk element by 59px. A
    viewBox with preserveAspectRatio="none" draws the rect in user units
    the browser maps onto whatever width the flex row leaves the svg, so
    the bar scales instead of overflowing."""
    m = re.search(
        r'data-panel="Activity Heatmap — legend"'
        r'[^>]*viewBox=\{`0 0 \$\{legendW\} 10`\}'
        r'[^>]*preserveAspectRatio="none"',
        EXTRA.read_text(encoding="utf-8"))
    assert m, (
        'the legend svg lost its viewBox + preserveAspectRatio="none" -- '
        "the gradient rect no longer scales with the svg the flex row "
        "shrinks (#637)")
    assert re.search(
        r'<rect data-role="legend" x="0" y="0" width=\{legendW\}',
        EXTRA.read_text(encoding="utf-8")), (
        "the legend rect is no longer drawn at the viewBox's user width")
