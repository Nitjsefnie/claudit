"""The Tool Error Rate panel's legend keys match the dashes drawn.

Source-level pins (node cannot parse JSX and nothing here renders
React, so a panel can pass every test while drawing a key whose dash
pattern differs from the line it labels — issue #682, found by a human
reading the legend against the chart).

The dash rule is ONE map: every picker entry takes its dash from its
PICKER position, whether checked or not, and BOTH readers — the per-tool
line loop and the legend — go through that map. Indexing TOOL_DASHES by
the position among the CHECKED tools only (the #682 bug) or by any
second, independently computed index reintroduces the drift the map
closes.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PANEL = ROOT / "src" / "tool-error-panel.jsx"


def _src() -> str:
    """The panel source without `//` comments, so prose ABOUT the rule is
    not read as the rule."""
    src = PANEL.read_text(encoding="utf-8")
    return re.sub(r"(?<![:'\"\w])//.*$", "", src, flags=re.M)


def test_tool_dashes_is_indexed_in_exactly_one_place():
    """The shared map build is the only TOOL_DASHES index site."""
    assert len(re.findall(r"TOOL_DASHES\[", _src())) == 1, (
        "TOOL_DASHES must be indexed only inside the shared toolDash map "
        "build — a second index site is a second dash rule and the "
        "#682 legend/line drift again")


def test_both_dash_readers_go_through_the_shared_map():
    """The per-tool line and the legend key read `toolDash`, keyed by the
    tool's display label — never an independently computed index."""
    src = _src()
    sites = re.findall(r"strokeDasharray=\{toolDash\[", src)
    assert len(sites) == 2, (
        f"expected the line loop and the legend each to read toolDash[], "
        f"found {len(sites)} readers")


def test_the_map_is_keyed_from_the_picker_position():
    """Every picker entry, checked or not, takes its dash from its picker
    index — never from the picked-only order."""
    m = re.search(r"toolDash\s*=\s*React\.useMemo(.*?)(?=\n  const )",
                  _src(), re.S)
    assert m, "could not locate the toolDash map build"
    body = m.group(1)
    assert "toolPickerEntries" in body, (
        "toolDash must be built from toolPickerEntries — keying it from "
        "the picked-only order is the #682 bug again")
    for banned in ("selTools", "pickedLabels", "indexOf", ".filter("):
        assert banned not in body, (
            f"toolDash's build names {banned!r} — a checked-subset or "
            "indexOf keying is the #682 bug again")
