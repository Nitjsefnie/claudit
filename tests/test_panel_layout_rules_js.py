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


class TestLegendBelowViolations:
    """The legend-placement classifier over owned chart groups (#773).

    Convention under test: every legend renders below every plot of its
    OWN chart. A group missing either side checks nothing -- a chart
    with no legend and a legend with no plot are conforming by having
    nothing to compare, which is the branch that keeps a new panel
    covered without being listed.
    """

    @staticmethod
    def _group(panel, plots, legends):
        return {
            "panel": panel,
            "plots": [{"x": x, "y": y, "w": w, "h": h}
                      for x, y, w, h in plots],
            "legends": [{"label": label, "x": x, "y": y, "w": w, "h": h}
                        for label, x, y, w, h in legends],
        }

    def _violations(self, groups):
        out = _node(
            "console.log(JSON.stringify(mod.legendBelowViolations("
            + json.dumps(groups) + ")));"
        )
        return out

    def test_legend_below_plot_passes(self):
        groups = [self._group("P", [(0, 100, 800, 200)],
                              [("show:", 0, 320, 700, 30)])]
        assert self._violations(groups) == []

    def test_legend_above_plot_fires(self):
        groups = [self._group("P", [(0, 100, 800, 200)],
                              [("show:", 0, 40, 700, 30)])]
        v = self._violations(groups)
        assert len(v) == 1
        assert v[0]["upper"] == 'P: legend "show:"'
        assert v[0]["lower"] == "plot"
        assert v[0]["gap"] == -260.0

    def test_legend_touching_within_noise_passes(self):
        # Plot bottom 300; a legend starting at 299 sits within the 1px
        # rounding noise and still reads as below.
        groups = [self._group("P", [(0, 100, 800, 200)],
                              [("show:", 0, 299, 700, 30)])]
        assert self._violations(groups) == []

    def test_legend_a_fraction_above_fires(self):
        groups = [self._group("P", [(0, 100, 800, 200)],
                              [("show:", 0, 298.9, 700, 30)])]
        v = self._violations(groups)
        assert len(v) == 1
        assert v[0]["gap"] == -1.1

    def test_chart_without_legends_checks_nothing(self):
        groups = [self._group("P", [(0, 100, 800, 200)], [])]
        assert self._violations(groups) == []

    def test_legend_without_plots_checks_nothing(self):
        groups = [self._group("P", [], [("show:", 0, 320, 700, 30)])]
        assert self._violations(groups) == []

    def test_side_by_side_legend_fires(self):
        # The rule is the vertical convention only: a legend beside its
        # plot (x-disjoint, y-overlapping) is not "below" and is the
        # same outlier the convention forbids.
        groups = [self._group("P", [(0, 100, 400, 200)],
                              [("show:", 850, 150, 300, 30)])]
        v = self._violations(groups)
        assert len(v) == 1

    def test_legend_between_two_plots_fires_against_the_lower(self):
        # Every (legend, plot) pair of the chart must read legend-below;
        # a legend sandwiched between two stacked plots violates against
        # the lower one only.
        groups = [self._group("P", [(0, 100, 800, 100), (0, 300, 800, 100)],
                              [("show:", 0, 210, 700, 30)])]
        v = self._violations(groups)
        assert len(v) == 1
        assert v[0]["gap"] == -190.0

    def test_legend_of_another_chart_does_not_pair(self):
        # Ownership bounds the pairing: a legend below ITS OWN plot but
        # above a LATER chart's plot is the conforming shape (two
        # stacked charts, each with its strip under it), not a finding.
        groups = [
            self._group("A", [(0, 100, 800, 200)], [("show:", 0, 320, 700, 30)]),
            self._group("B", [(0, 400, 800, 200)], []),
        ]
        assert self._violations(groups) == []

    def test_legend_rule_is_registered(self):
        out = _node("console.log(JSON.stringify(mod.RULES"
                    ".map((r) => r.id)));")
        assert "legend-below-plot" in out


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
class TestGapConsistencyViolations:
    """The gap-consistency classifier over a flex-column stack (#796).

    Convention under test: a stack's children sit at the stack's own flex
    gap PLUS the pair's own margins (flex adds the gap to whatever the
    margins contribute), and the dashboard-wide convention is that the
    stack gap is .dashboard's row gap. The probe supplies the groups (the
    dashboard itself and each flex-column section); the classifier is
    pure over their boxes.
    """

    @staticmethod
    def _violations(boxes, expected, tol=1):
        return _node(
            "console.log(JSON.stringify(mod.gapConsistencyViolations("
            + json.dumps(boxes) + ", " + json.dumps(expected) + ", "
            + json.dumps(tol) + ")));"
        )

    @staticmethod
    def _boxes(*specs):
        # name, x, y, w, h, mt, mb — margins optional, as the probe
        # reports them only when the layout gives them.
        return [
            {"name": n, "x": x, "y": y, "w": w, "h": h, "mt": mt, "mb": mb}
            for n, x, y, w, h, mt, mb in specs
        ]

    def test_pairs_at_the_expected_gap_pass(self):
        out = self._violations(
            self._boxes(("A", 0, 0, 800, 300, 0, 0),
                        ("B", 0, 314, 800, 200, 0, 0)), 14)
        assert out == []

    def test_only_adjacent_pairs_are_measured(self):
        # A, B, C stacked: A->B at the expected 14, B->C drifted to 86.
        # Exactly ONE finding (B->C): the A->C distance is not a flex
        # gap, it is whatever the stack adds up to between neighbours —
        # the all-pairs reading fired on sections pages apart.
        out = self._violations(
            self._boxes(("A", 0, 0, 800, 300, 0, 0),
                        ("B", 0, 314, 800, 200, 0, 0),
                        ("C", 0, 600, 800, 200, 0, 0)), 14)
        assert len(out) == 1
        assert {out[0]["upper"], out[0]["lower"]} == {"B", "C"}

    def test_a_drifted_pair_fires(self):
        out = self._violations(
            self._boxes(("A", 0, 0, 800, 300, 0, 0),
                        ("B", 0, 304, 800, 200, 0, 0)), 14)
        assert len(out) == 1
        assert out[0]["upper"] == "A"
        assert out[0]["lower"] == "B"
        assert out[0]["gap"] == 4.0
        assert out[0]["expected"] == 14.0

    def test_a_wider_pair_fires_too(self):
        out = self._violations(
            self._boxes(("A", 0, 0, 800, 300, 0, 0),
                        ("B", 0, 318, 800, 200, 0, 0)), 14)
        assert len(out) == 1
        assert out[0]["gap"] == 18.0

    def test_rounding_within_tolerance_passes(self):
        # 13.2 is 0.8 under the expected 14: within the 1px rounding
        # slack both gap rules share (NOISE), it still reads as the
        # expected gap.
        out = self._violations(
            self._boxes(("A", 0, 0, 800, 300, 0, 0),
                        ("B", 0, 313.2, 800, 200, 0, 0)), 14)
        assert out == []

    def test_margins_extend_the_expected_distance(self):
        # The header shape the real page renders: page-head carries
        # margin-bottom 18px, so its pair distance is 14 + 18 = 32 and a
        # bare comparison against 14 would fire on a conforming page.
        out = self._violations(
            self._boxes(("page-head", 0, 0, 800, 60, 0, 18),
                        ("dash-summary", 0, 92, 800, 40, 0, 0)), 14)
        assert out == []
        # And the same pair 10px SHORT of its expected 32 is the drift
        # the rule exists for.
        out = self._violations(
            self._boxes(("page-head", 0, 0, 800, 60, 0, 18),
                        ("dash-summary", 0, 82, 800, 40, 0, 0)), 14)
        assert len(out) == 1
        assert out[0]["expected"] == 32.0

    def test_side_by_side_and_contained_boxes_never_pair(self):
        # Same exclusions as the vertical-gap classifier: grid columns
        # and contained cards are not stacked sections.
        out = self._violations(
            self._boxes(("A", 0, 0, 390, 300, 0, 0),
                        ("B", 400, 0, 390, 300, 0, 0)), 14)
        assert out == []
        out = self._violations(
            self._boxes(("wrap", 0, 0, 800, 300, 0, 0),
                        ("card", 0, 299, 800, 2, 0, 0)), 14)
        assert out == []

    def test_zero_boxes_are_skipped(self):
        # display:contents wrappers report an all-zero rect; they
        # generate no box and must not pair with anything.
        out = self._violations(
            self._boxes(("wrapper", 0, 0, 0, 0, 0, 0),
                        ("A", 0, 0, 800, 300, 0, 0),
                        ("B", 0, 314, 800, 200, 0, 0)), 14)
        assert out == []


# The in-page halves (the probes and the seeds) run only in the browser
# leg: node cannot drive a DOM. What the suite CAN pin is the source
# signatures they read, so a rename in the module turns these pins into
# failures instead of silent drift (the test_picker_fit_js pattern).


def _module_text() -> str:
    return MODULE.read_text(encoding="utf-8")


def test_untagged_legend_detector_reads_checkbox_colour_keys():
    """The legend-shape detector must read the one shape every strip in
    src/ shares (#474 made the checkbox the colour key): direct-child
    labels carrying checkboxes. A detector keyed on anything rarer
    misses the next panel's legend."""
    assert 'input[type="checkbox"]' in _module_text(), (
        "the untagged-legend detector does not read checkbox colour keys"
    )


def test_wrapper_gap_seed_drives_the_style():
    """The seeded red case for the gap-consistency rule must shrink the
    wrapper's own flex gap on the live page (a DOM injection, not an app
    change)."""
    assert "style.gap" in _module_text(), (
        "seedWrapperGap does not set the wrapper's style.gap"
    )


def test_gap_consistency_probe_reads_flex_direction():
    """The consistency probe scopes its groups by computed flex
    direction: .dashboard plus every flex-column section — not a listed
    class set."""
    text = _module_text()
    assert "flexDirection" in text, (
        "the gap-consistency probe does not read flexDirection"
    )
    assert "rowGap" in text, (
        "the gap-consistency probe does not read the container's row gap"
    )


def test_multichart_legend_rules_share_the_probe():
    """Both legend-below rules measure the SAME probe output — the
    multichart rule is the same convention with a different seed, not a
    parallel implementation."""
    text = _module_text()
    assert "legendBelowViolations" in text
    assert "legend-below-plot-multichart" in text


def test_multichart_rule_id_and_tagged_rule_id_registered():
    out = _node("console.log(JSON.stringify(mod.RULES"
                ".map((r) => r.id)));")
    assert "section-gap-consistency" in out
    assert "legend-strips-tagged" in out
    assert "legend-below-plot-multichart" in out


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
class TestTopPlotBox:
    """The multichart legend rule pairs a section legend against the
    section's TOPMOST plot: the convention it holds is 'not above the
    charts', and a strip sitting under chart 1 of a stacked grid must
    not fire against chart 2 below it (a union box would)."""

    def test_lowest_y_wins(self):
        out = _node(
            "console.log(JSON.stringify(mod.topPlotBox("
            + json.dumps([
                {"x": 0, "y": 300, "w": 800, "h": 100},
                {"x": 0, "y": 100, "w": 800, "h": 100},
            ])
            + ")));"
        )
        assert out["y"] == 100

    def test_ties_keep_the_first(self):
        out = _node(
            "console.log(JSON.stringify(mod.topPlotBox("
            + json.dumps([
                {"x": 0, "y": 100, "w": 800, "h": 100},
                {"x": 400, "y": 100, "w": 800, "h": 100},
            ])
            + ")));"
        )
        assert out["x"] == 0

    def test_empty_is_null(self):
        out = _node("console.log(JSON.stringify(mod.topPlotBox([])));")
        assert out is None

    def test_single_is_itself(self):
        out = _node(
            "console.log(JSON.stringify(mod.topPlotBox("
            + json.dumps([{"x": 0, "y": 100, "w": 800, "h": 100}])
            + ")));"
        )
        assert out["y"] == 100


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
class TestPairSectionLegendsTop:
    """The multichart rule's wiring decision, pinned where a union
    refactor fails (#797): a SECTION group's legends pair against the
    section's TOPMOST plot only. The discriminating case is the strip
    UNDER chart 1 of a stacked grid — topmost pairing passes it, a
    union-of-plots box fires it against chart 2 (the union mutant the
    review's M4 plant proved every leg survives)."""

    @staticmethod
    def _group(panel, plots, legends, section=False):
        return {
            "panel": panel,
            "section": section,
            "plots": [{"x": x, "y": y, "w": w, "h": h}
                      for x, y, w, h in plots],
            "legends": [{"label": label, "x": x, "y": y, "w": w, "h": h}
                        for label, x, y, w, h in legends],
        }

    def _violations(self, groups):
        return _node(
            "console.log(JSON.stringify(mod.legendBelowViolations("
            "mod.pairSectionLegendsTop(" + json.dumps(groups) + "))));"
        )

    def test_strip_under_chart1_of_a_stacked_grid_passes(self):
        # The union mutant FIRES here: chart 2's plot bottom lies below
        # the strip, so a union box reads the strip as above-plot.
        # Topmost pairing passes: the strip is under chart 1.
        groups = [self._group(
            "dash-grid (multi-chart section)",
            [(0, 100, 800, 200), (0, 400, 800, 200)],
            [("show:", 0, 320, 700, 30)], section=True)]
        assert self._violations(groups) == []

    def test_strip_above_the_top_plot_fires(self):
        groups = [self._group(
            "dash-grid (multi-chart section)",
            [(0, 100, 800, 200), (0, 400, 800, 200)],
            [("seeded:", 0, 40, 700, 30)], section=True)]
        v = self._violations(groups)
        assert len(v) == 1
        assert v[0]["gap"] == -260.0

    def test_mark_group_passes_through_unchanged(self):
        # A mark-owned group keeps ALL its plots: a legend sandwiched
        # between two plots of one chart still fires against the lower
        # one — the substitution touches section groups only.
        groups = [self._group(
            "P",
            [(0, 100, 800, 100), (0, 300, 800, 100)],
            [("show:", 0, 210, 700, 30)], section=False)]
        v = self._violations(groups)
        assert len(v) == 1
        assert v[0]["gap"] == -190.0

    def test_section_without_plots_checks_nothing(self):
        groups = [self._group(
            "sec", [], [("show:", 0, 320, 700, 30)], section=True)]
        assert self._violations(groups) == []


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
class TestMultichartRuleRunWiring:
    """The rule's own run() applies the topmost substitution.

    Node drives run() through a fake page serving the between-charts
    groups. The review's mutation (the shared run skipping the
    pairSectionLegendsTop substitution) passed every pure test AND the
    browser harness — the seed sits above every chart, so both wirings
    fire it — and this test exists to die on exactly that mutant.
    """

    def _run_multichart(self):
        groups = [
            {"panel": "dash-grid (multi-chart section)",
             "section": True,
             "plots": [{"x": 0, "y": 100, "w": 800, "h": 200},
                       {"x": 0, "y": 400, "w": 800, "h": 200}],
             "legends": [{"label": "show:", "x": 0, "y": 320,
                          "w": 700, "h": 30}]},
        ]
        return _node(
            "const rule = mod.RULES.find((r) => "
            "r.id === 'legend-below-plot-multichart');"
            "const ctx = { page: { evaluate: async () => "
            + json.dumps(groups) + " } };"
            "console.log(JSON.stringify(await rule.run(ctx)));"
        )

    def test_run_pairs_the_between_charts_strip_against_the_top_plot(self):
        # The strip sits under chart 1 (y 320; plot bottoms 300 and
        # 600): the substitution must limit pairing to the top plot, or
        # chart 2's plot claims the strip and run() returns a violation
        # instead of [].
        assert self._run_multichart() == []
