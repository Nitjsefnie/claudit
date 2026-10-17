"""Place the Tool Error Rate legend below its graph like every other panel.
The suite does not render React, so source checks pin the placement wiring.
The browser legend-below-plot rule in scripts/ci/panel_layout_rules.mjs
checks those same strips across the rendered dashboard."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL_ERROR = ROOT / "src" / "tool-error-panel.jsx"


def _strip_line_comments(src: str) -> str:
    """Drop `//` line comments so prose ABOUT a mistake is not read as the
    mistake. The lookbehind spares `https://` (a colon precedes those
    slashes), which is the only // that shows up mid-expression here.
    """
    return re.sub(r"(?<![:'\"\w])//.*$", "", src, flags=re.M)


def test_tool_error_legend_strips_follow_the_below_chart_convention():
    """Both legend strips carry the flexbox shape every other panel uses:
    `order: 99` (the chart's wrapper stays at 0, so the strip paints
    after the chart whatever the JSX order) and a TOP border, because the
    strip now sits under the chart. The header keeps the panel's one
    bottom border — a second one belongs to a strip painted above it.

    The counts read the whole file with only `//` comments stripped, so
    the pin stays exact only while JSX prose avoids the pinned literals:
    never write the spellings this file counts — `order: 99`, `borderTop`,
    `borderBottom` — inside a `{/* */}` block comment here, where the
    stripper cannot see them; say "order 99" instead.
    """
    src = _strip_line_comments(TOOL_ERROR.read_text(encoding="utf-8"))
    assert src.count("order: 99") == 2, (
        "both Tool Error Rate legend strips must carry order: 99 — the "
        "below-chart convention every other panel uses; without it the "
        "strips paint in JSX order, above the chart")
    assert src.count("borderTop") == 2, (
        "both legend strips sit below the chart and take the separating "
        "border on their top edge")
    assert src.count("borderBottom") == 1, (
        "only the header keeps a bottom border; a second one belongs to "
        "a strip still rendered above the chart")
