"""Issue #364: the Inspector timeline selection when a search shrinks it.

node cannot parse JSX and nothing here renders React, so the selection
semantics live in the plain-JS helper src/timeline-selection.js (node
executes it, the same boundary as test_dashboard_binning.py) and the
SessionView wiring is pinned at source level.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "src" / "app.jsx"
HELPER = ROOT / "src" / "timeline-selection.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available"
)


def _run_helper(script: str) -> dict:
    proc = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_selection_recovers_when_a_search_shrinks_the_timeline():
    """The #364 sequence: row 3 of four selected, then a search leaves two
    rows. The active option, the selected row and the detail row derive
    from ONE clamped index -- the second visible row -- and ArrowUp moves
    from IT (to the first visible row), not from the stale index two rows
    past the end. An in-range selection is untouched (identity).
    """
    script = f"""
      global.window = {{}};
      require({str(HELPER)!r});
      // select row 3 of 4, then the search leaves 2 rows visible
      const active = window.activeTimelineIndex(3, 2);
      console.log(JSON.stringify({{
        active,
        up: window.moveTimelineIndex(active, 2, -1),
        down: window.moveTimelineIndex(active, 2, 1),
        enter: active,
        inRangeUntouched: window.activeTimelineIndex(2, 4),
        topClamp: window.moveTimelineIndex(0, 4, -1),
        bottomClamp: window.moveTimelineIndex(3, 4, 1),
        step: window.moveTimelineIndex(3, 4, -1),
        empty: window.activeTimelineIndex(0, 0),
        emptyMove: window.moveTimelineIndex(0, 0, 1),
      }}));
    """
    result = _run_helper(script)
    assert result == {
        "active": 1,             # the second visible row, not nothing
        "up": 0,                 # ArrowUp lands on the FIRST visible row
        "down": 1,               # ArrowDown recovers the active row
        "enter": 1,
        "inRangeUntouched": 2,   # unfiltered semantics unchanged
        "topClamp": 0,
        "bottomClamp": 3,
        "step": 2,
        "empty": -1,             # no rows -> no active option
        "emptyMove": -1,
    }


def test_detail_row_derives_from_the_clamped_index():
    """The detail pane's row is visible[activeIdx], never the raw stored
    index: the index a filter shrank under must not reach any consumer
    unclamped."""
    src = APP.read_text(encoding="utf-8")
    assert "const activeIdx = window.activeTimelineIndex(selected, visible.length);" in src, (
        "app.jsx derives activeIdx no longer through the shared helper; "
        "relocate this pin with it")
    assert "const sel = visible[activeIdx] || null;" in src, (
        "the detail row is visible[selected] again -- a search that "
        "shrinks the timeline under the selection empties the detail pane")
    assert "visible[selected]" not in src, (
        "a consumer still reads the raw selected index straight out of "
        "the filtered rows")


def test_rows_and_arrows_derive_from_the_clamped_index():
    """The rows and both arrow moves use the same clamped index: a row is
    selected iff it IS the active option, and the arrows move from it."""
    src = APP.read_text(encoding="utf-8")
    assert "selected={idx === activeIdx}" in src, (
        "rows no longer mark the active option selected")
    assert "idx === selected" not in src, (
        "a row compares against the raw selected index -- after a search "
        "shrinks the list, no row is selected while aria-activedescendant "
        "points elsewhere")
    assert "rowId(activeIdx)" in src, (
        "the listbox's aria-activedescendant no longer names the clamped "
        "index -- the active option would disagree with the selected row")
    assert "rowId(selected)" not in src, (
        "aria-activedescendant names the raw stored index -- after a "
        "search shrinks the list it points at a row nothing selected")
    handler_m = re.search(r"const onTimelineKeyDown = \(ev\) => \{(.*?)\n  \};",
                          src, re.S)
    assert handler_m, (
        "the onTimelineKeyDown handler moved; relocate this pin with it")
    handler = handler_m.group(1)
    moves = handler.count("window.moveTimelineIndex(activeIdx, visible.length,")
    assert moves == 2, (
        f"expected both arrow moves to route through the shared move "
        f"helper at the clamped index, found {moves}")
    assert "selected +" not in handler and "+ 1" not in handler.replace(
        "moveTimelineIndex", ""), (
        "an arrow move still does raw index arithmetic on the stale state")


def test_helper_is_loaded_before_app_jsx():
    """app.jsx reaches the helper through window at render time; the
    helper's plain script must still be a direct (non-babel) script tag,
    present before app.jsx in index.html."""
    html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    helper_m = re.search(r'<script src="/src/timeline-selection\.js">', html)
    assert helper_m, (
        "src/timeline-selection.js is not loaded by index.html -- "
        "window.activeTimelineIndex would be undefined at render")
    app_m = re.search(r'<script type="text/babel" src="/src/app\.jsx">', html)
    assert app_m and app_m.start() > helper_m.start(), (
        "app.jsx loads before the timeline-selection helper")
