"""The layout-consistency harness's testable halves, driven through node.

Issue #772: the dashboard's class-less self-fetch wrapper took the panels
from Tool Usage Ratio over Time downward out of ``.dashboard``'s flex-gap
context, and those sections rendered flush against their neighbours. Every
other check was green because nothing in the suite measured rendered
geometry.

The guard proper is ``scripts/ci/panel_layout_rules.mjs`` and it runs only
in the browser leg (panel-layout.yml): nothing in pytest can read a
bounding box. What the suite CAN drive are the harness's pure halves --
the rule registry and the vertical-gap classifier -- plus the wiring that
keeps the guard honest: the workflow step, the marks its measured universe
relies on, and the seed each rule carries for its red proof.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "scripts" / "ci" / "panel_layout_rules.mjs"
WORKFLOW = ROOT / ".github" / "workflows" / "panel-layout.yml"


def _node(body: str):
    """Import the harness in node and evaluate `body` against its exports.

    Over STDIN with --input-type=module, as test_panel_interactions_js.py
    does. The import is itself an assertion: the harness must launch its
    browser only when executed directly, never when imported.
    """
    url = MODULE.as_uri()
    script = (
        f"const mod = await import({json.dumps(url)});\n"
        f"{body}\n"
    )
    proc = subprocess.run(
        ["node", "--input-type=module"], input=script, capture_output=True,
        text=True, timeout=60, check=False, encoding="utf-8", errors="strict",
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return json.loads(proc.stdout)


# A stable box builder: x, y, w, h — the same four numbers the in-page
# probe reports, so every case below reads as the geometry it describes.
def _boxes(*specs):
    return [
        {"name": name, "x": x, "y": y, "w": w, "h": h}
        for name, x, y, w, h in specs
    ]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
class TestGapViolations:
    """The vertical-gap classifier over measured boxes."""

    def test_detects_a_zero_gap_stacked_pair(self):
        out = _node(
            "console.log(JSON.stringify(mod.gapViolations("
            + json.dumps(_boxes(("A", 0, 0, 800, 300), ("B", 0, 300, 800, 200)))
            + ")))" + ";"
        )
        v = out
        assert len(v) == 1
        assert {v[0]["upper"], v[0]["lower"]} == {"A", "B"}
        assert v[0]["gap"] < 2

    def test_normal_section_gap_passes(self):
        out = _node(
            "console.log(JSON.stringify(mod.gapViolations("
            + json.dumps(_boxes(("A", 0, 0, 800, 300), ("B", 0, 314, 800, 200)))
            + ")))" + ";"
        )
        assert out == []

    def test_sub_pixel_gap_still_fails(self):
        # Chromium rounds boxes to 1/10 px; a truly flush pair can read as
        # 0.2px apart after rounding. That is still no gap.
        out = _node(
            "console.log(JSON.stringify(mod.gapViolations("
            + json.dumps(
                _boxes(("A", 0, 0, 800, 300), ("B", 0, 300.2, 800, 200)))
            + ")))" + ";"
        )
        v = out
        assert len(v) == 1

    def test_slight_overlap_reads_as_missing_gap(self):
        # A box starting just ABOVE the upper's bottom edge has a NEGATIVE
        # gap: flush or overlapped, there is no gap, so the rule claims it.
        # Deep interleaving stays panel_layout.mjs's overlap domain (the
        # interleaved test below).
        out = _node(
            "console.log(JSON.stringify(mod.gapViolations("
            + json.dumps(
                _boxes(("A", 0, 0, 800, 300), ("B", 0, 299.5, 800, 300)))
            + ")))" + ";"
        )
        assert len(out) == 1
        assert out[0]["gap"] <= 0

    def test_gap_below_threshold_fails(self):
        # 1.5px between stacked sections is rendering noise away from
        # flush: under the 2px threshold, it is a finding.
        out = _node(
            "console.log(JSON.stringify(mod.gapViolations("
            + json.dumps(
                _boxes(("A", 0, 0, 800, 300), ("B", 0, 301.5, 800, 200)))
            + ")))" + ";"
        )
        v = out
        assert len(v) == 1

    def test_side_by_side_columns_are_not_adjacent(self):
        out = _node(
            "console.log(JSON.stringify(mod.gapViolations("
            + json.dumps(
                _boxes(("A", 0, 0, 390, 300), ("B", 400, 0, 390, 300)))
            + ")))" + ";"
        )
        assert out == []

    def test_grid_rows_at_12px_pass(self):
        # The narrowest legitimate gap in the dashboard is the 12px grid
        # gap; a threshold that reds it would be wrong, not strict.
        out = _node(
            "console.log(JSON.stringify(mod.gapViolations("
            + json.dumps(
                _boxes(("A", 0, 0, 390, 300), ("B", 0, 312, 390, 200)))
            + ")))" + ";"
        )
        assert out == []

    def test_contained_box_is_skipped(self):
        # A box contained in another (within the 2px tolerance) is never a
        # gap pair — the containment skip claims it, not the interleave
        # arm: the inner box starts within NOISE of the outer's bottom and
        # pokes at most 2px past it, so without the skip the pair reads
        # adjacent with a negative gap and this pin would fail.
        out = _node(
            "console.log(JSON.stringify(mod.gapViolations("
            + json.dumps(
                _boxes(("wrap", 0, 0, 800, 300), ("card", 0, 299, 800, 2)))
            + ")))" + ";"
        )
        assert out == []

    def test_interleaved_boxes_are_not_this_rule(self):
        # Boxes whose vertical ranges interleave are overlapping —
        # panel_layout.mjs's domain, not a missing-gap finding.
        out = _node(
            "console.log(JSON.stringify(mod.gapViolations("
            + json.dumps(
                _boxes(("A", 0, 0, 800, 300), ("B", 0, 150, 800, 300)))
            + ")))" + ";"
        )
        assert out == []

    def test_threshold_is_a_parameter(self):
        out = _node(
            "console.log(JSON.stringify(mod.gapViolations("
            + json.dumps(_boxes(("A", 0, 0, 800, 300), ("B", 0, 314, 800, 200)))
            + ", 20)));"
        )
        v = out
        assert len(v) == 1

    def test_minority_horizontal_overlap_is_not_adjacency(self):
        # A narrow footer centred under a wide panel shares x-range but a
        # pair that barely overlaps horizontally is not the stacked
        # sections this rule reads.
        out = _node(
            "console.log(JSON.stringify(mod.gapViolations("
            + json.dumps(
                _boxes(("A", 0, 0, 800, 300), ("B", 750, 300, 100, 50)))
            + ")))" + ";"
        )
        assert out == []

    def test_zero_boxes_are_skipped(self):
        # display:contents wrappers report an all-zero rect; they generate
        # no box and must not pair with anything.
        out = _node(
            "console.log(JSON.stringify(mod.gapViolations("
            + json.dumps(
                _boxes(("wrapper", 0, 0, 0, 0), ("A", 0, 0, 800, 300),
                       ("B", 0, 300, 800, 200)))
            + ")))" + ";"
        )
        v = out
        assert len(v) == 1
        assert {v[0]["upper"], v[0]["lower"]} == {"A", "B"}


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
class TestRuleRegistry:
    """Every rule ships its red proof and a unique id, or it guards nothing."""

    def test_registry_is_not_empty(self):
        out = _node("console.log(JSON.stringify(mod.RULES.map(r => r.id)));")
        assert out, "RULES is empty"

    def test_ids_are_unique(self):
        out = _node("console.log(JSON.stringify(mod.RULES.map(r => r.id)));")
        ids = out
        assert len(ids) == len(set(ids))

    def test_every_rule_carries_seed_and_run_and_description(self):
        out = _node(
            "console.log(JSON.stringify(mod.RULES.map(r => ({"
            " id: r.id,"
            " description: typeof r.description === 'string' && r.description,"
            " seed: typeof r.seed === 'function',"
            " run: typeof r.run === 'function' }))));"
        )
        for r in out:
            assert r["description"], f"{r['id']}: no description"
            assert r["seed"], f"{r['id']}: no seed (its red proof)"
            assert r["run"], f"{r['id']}: no run"

    def test_collect_selector_covers_cards_and_sections(self):
        # The measured universe must stay generic: every panel card and
        # every direct section of .dashboard, so a new panel is covered
        # without being listed here.
        out = _node("console.log(JSON.stringify(mod.COLLECT_SELECTOR));")
        sel = out
        assert "[data-panel]" in sel
        assert "[data-list-panel]" in sel
        assert ".dashboard" in sel


def test_workflow_runs_the_harness():
    """The browser leg must run the harness, or the classifier above is
    dead code and #772 ships green again."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "node scripts/ci/panel_layout_rules.mjs" in text
