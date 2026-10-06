"""The chart panels' layout arithmetic, driven through node.

Issue #630: the Per-Session Context Growth comparison panel laid its
legend out on fixed offsets, so a 17-character model name rendered
108.5px wide and ran 9.5px into the "median (N files)" label beside it
and straight through the swatch rule -- and the legend's wrapped row fell
off the bottom of a fixed-height svg, which clipped it away. The panel
was unfixed, unfixed-shaped, and green: nothing in this suite renders a
browser.

So the arithmetic moved into `src/panel-layout.js` -- plain JS, no React
-- and this file pins it. What CANNOT be pinned here is the browser's
own text measurement: the advance is an input, measured off a real
rendered string by the panel itself, and `scripts/ci/panel_layout.mjs`
is what reads the resulting boxes.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAYOUT_JS = ROOT / "src" / "panel-layout.js"

# The advance the browser measured for these panels' 9.5px monospace in
# headless Chromium (issue #630): wider than the 0.6em a naive reader
# would predict, which is exactly why it is measured rather than guessed.
ADV = 6.382


def _node(body: str):
    """Run `body` against the real src/panel-layout.js in node."""
    script = f"""
      global.window = {{}};
      require({str(LAYOUT_JS)!r});
      {body}
    """
    # Over STDIN, not -e: the payload below embeds JSON, and Windows
    # refuses a CreateProcess command line over 32k (WinError 206).
    #
    # encoding="utf-8" is load-bearing, not tidiness. `text=True` alone
    # decodes with the LOCALE encoding, which on a windows-latest runner
    # is cp1252 — and node writes the ellipsis these panels shorten labels
    # with as UTF-8 bytes (e2 80 a6), which cp1252 cannot represent. Every
    # such byte came back as U+FFFD and six assertions failed on Windows
    # only, against a panel that renders correctly. errors="strict" keeps a
    # future encoding problem loud instead of comparing mojibake.
    proc = subprocess.run(
        ["node"], input=script, capture_output=True, text=True, timeout=60,
        check=False, encoding="utf-8", errors="strict",
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# --- fitText -----------------------------------------------------------

CTX_JS = ROOT / "src" / "ctx-axis.js"

GRID_JSX = ROOT / "src" / "dashboard-charts-extra.jsx"

COMPARE_JSX = ROOT / "src" / "context-growth-comparison.jsx"

SESSION_JSX = ROOT / "src" / "context-growth-view.jsx"

ESLINT_CONFIG = ROOT / "eslint.config.mjs"


def _strip_js_comments(src: str) -> str:
    """Blank out JS comments, keeping the line structure.

    src/ctx-axis.js names both removed symbols in its header, explaining
    what it replaced and why. Reading that prose as the mistake would
    make the absence assertion below unsatisfiable by any honest file
    that documents the change -- and deleting the explanation instead
    would be the wrong trade. Same shape as test_panel_wiring's helper.
    """
    out, i, n = [], 0, len(src)
    while i < n:
        if src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append("".join(c if c == "\n" else " " for c in src[i:j]))
            i = j
        else:
            out.append(src[i])
            i += 1
    return "".join(out)


def _ctx_node(body: str):
    """Run `body` against the real src/ctx-axis.js in node."""
    script = f"""
      global.window = {{}};
      require({str(CTX_JS)!r});
      {body}
    """
    proc = subprocess.run(
        ["node"], input=script, capture_output=True, text=True, timeout=60,
        check=False, encoding="utf-8", errors="strict",
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# A model's sessions, as the two dashboard panels are handed them. Two
# sessions, uneven turn counts, and a peak that is neither the first
# turn nor the last one -- so "take the last", "take the first" and
# "take the longest session" all give a different wrong answer than
# "take the largest ctx" does.
BUNDLE = json.dumps([
    {"seq": [{"t": 0, "ctx": 12_000}, {"t": 1, "ctx": 48_500},
             {"t": 2, "ctx": 31_000}]},
    {"seq": [{"t": 0, "ctx": 9_000}, {"t": 1, "ctx": 210_000}]},
])

# A second model whose peak is higher than BUNDLE's, so a comparison
# that folds it must move and one that does not must not.
TALLER = json.dumps([
    {"seq": [{"t": 0, "ctx": 500_000}, {"t": 1, "ctx": 4_000}]},
])


def _y_axis_expression(path: Path) -> str:
    """A panel's context y-axis expression, verbatim, or fail.

    The assignment's RIGHT-HAND side only, so it can be evaluated in
    node with the panel's own locals in scope. `re.search` anchors on the
    `window.ctxAxis` call itself, so a panel that goes back to folding
    its own constant here stops matching and the test says so.
    """
    src = _strip_js_comments(path.read_text(encoding="utf-8"))
    match = re.search(
        r"const yMax(?:Abs)? = (window\.ctxAxis\.ctxAxis(?:Top|TopFor)\([^;]*\));",
        src)
    assert match, (
        f"{path.name} no longer derives its context y axis from "
        "window.ctxAxis.ctxAxisTop/ctxAxisTopFor; the shared rule was "
        "bypassed or renamed")
    return match.group(1)


def _series_expression(path: Path) -> str:
    """The comparison panel's `series` construction, verbatim, or fail.

    This is the bundle the comparison's axis expression actually reads.
    Handing the expression a series the TEST builds instead would leave
    the panel's own wiring unpinned, and the field that matters most is
    the one an edit is most likely to drop: `sessions` is new in #648, so
    a refactor that rebuilt this bundle without it would silently scale
    the comparison off MIN_TOP while the grid kept its real axis --
    precisely the disagreement #648 exists to close, and invisible to a
    test that supplies its own bundle.
    """
    src = _strip_js_comments(path.read_text(encoding="utf-8"))
    match = re.search(
        r"const series = React\.useMemo\(\(\) => (.*?), \[models, byModel\]\);",
        src, re.S)
    assert match, (
        f"{path.name} no longer builds `series` the way this test extracts "
        "it; the axis is checked against something the panel does not use")
    return match.group(1)


CAP_NAMES = ("capForModel", "MODEL_CAPS")


def _cap_readers(root: Path) -> list[str]:
    """Files naming a per-model context cap, as `path:line`."""
    found = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in {".js", ".jsx", ".mjs", ".html"}:
            continue
        for n, line in enumerate(_strip_js_comments(
                path.read_text(encoding="utf-8")).splitlines(), 1):
            if any(name in line for name in CAP_NAMES):
                found.append(f"{path.relative_to(root)}:{n}")
    return found


class TestNodeDrivenPanelAndCtxAxis:
    # Every test below drives node; the class mark is the file's node skip (#746).
    pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")

    def test_text_that_already_fits_is_returned_unchanged(self):
        got = _node(
            "console.log(JSON.stringify(window.panelLayout.fitText('Cost (USD)',"
            f" {ADV}, 400)))"
        )
        assert got == "Cost (USD)"

    def test_text_too_wide_is_shortened_with_an_ellipsis_and_then_fits(self):
        got = _node(f"""
          const t = 'claude-sonnet-4-6 vs claude-opus-4-8  ·  median per turn';
          const short = window.panelLayout.fitText(t, {ADV}, 239);
          console.log(JSON.stringify({{short, width: short.length * {ADV}}}));
        """)
        assert got["short"].endswith("…")
        assert len(got["short"]) < 55
        assert got["width"] <= 239

    def test_a_width_too_narrow_for_even_one_character_still_yields_one_glyph(self):
        # The degenerate case must return something drawable, not an empty
        # string: an empty label is a hole where a model name belongs.
        got = _node(f"console.log(JSON.stringify(window.panelLayout.fitText('abc', {ADV}, 2)))")
        assert got == "…"

    def test_a_non_positive_advance_or_width_is_passed_through(self):
        # No measured advance yet (the first paint) or a degenerate width must
        # not crash the panel or blank the title.
        got = _node(
            "console.log(JSON.stringify([window.panelLayout.fitText('abc', 0, 100),"
            " window.panelLayout.fitText('abc', 6, 0), window.panelLayout.fitText(null, 6, 100)]))"
        )
        assert got == ["abc", "abc", ""]

    def test_the_rule_and_the_count_sit_after_the_labels_own_width(self):
        # THE #630 REGRESSION. At the fixed offsets the panel used (rule at
        # 86, count at 108) this label is 108.5px wide starting at x=9, so it
        # ran through the rule and 9.5px into the count. The offsets must be
        # derived from the label instead.
        got = _node(f"""
          const c = window.panelLayout.clusterAt('claude-sonnet-4-6', 14, {ADV}, 0);
          console.log(JSON.stringify({{
            label: c.label, text: c.text, ruleX: c.ruleX, countX: c.countX,
            ruleGap: c.ruleX - (9 + c.text.length * {ADV}),
            countGap: c.countX - (c.ruleX + 16),
          }}));
        """)
        assert got["label"] == "claude-sonnet-4-6"
        assert got["text"] == "claude-sonnet-4-6"
        assert got["ruleGap"] == pytest.approx(8)
        assert got["countGap"] == pytest.approx(6)
        # The count begins strictly after the label's own box ends.
        assert got["countX"] >= 9 + len(got["text"]) * ADV

    def test_the_count_text_names_the_files_it_summarises(self):
        got = _node(f"console.log(JSON.stringify(window.panelLayout.clusterAt('m', 1314, {ADV}, 0).countText))")
        assert got == "median (1,314 files)"

    def test_a_cluster_wider_than_its_slot_is_shortened_to_fit_it(self):
        got = _node(f"""
          const c = window.panelLayout.clusterAt('claude-opus-4-8', 12, {ADV}, 239);
          console.log(JSON.stringify({{w: c.width, label: c.label, text: c.text,
            count: c.countText}}));
        """)
        assert got["w"] <= 239
        assert got["text"].endswith("…") or got["count"].endswith("…")
        # The rule still follows the SHORTENED text, not the original one.
        assert got["w"] == pytest.approx(
            9 + len(got["text"]) * ADV + 8 + 16 + 6 + len(got["count"]) * ADV,
            abs=0.01,
        )

    def test_a_shortened_cluster_keeps_its_identity_and_loses_only_the_text(self):
        # #630 review finding 1: the panel keys its React children and looks
        # the model's colour up by `label`. A cluster that returned the
        # SHORTENED string as its identity gave the legend entry a #888
        # fallback swatch beside its own model-coloured median line, and two
        # sibling models that shortened alike collided as duplicate keys.
        got = _node(f"""
          const c = window.panelLayout.clusterAt('claude-opus-4-8', 12, {ADV}, 120);
          console.log(JSON.stringify({{label: c.label, text: c.text}}));
        """)
        assert got["label"] == "claude-opus-4-8"
        assert got["text"] != got["label"] and got["text"].endswith("…")

    def test_two_models_that_shorten_alike_stay_two_distinct_clusters(self):
        got = _node(f"""
          const a = window.panelLayout.clusterAt('claude-opus-4-8', 12, {ADV}, 120);
          const b = window.panelLayout.clusterAt('claude-opus-4-5', 12, {ADV}, 120);
          console.log(JSON.stringify({{labels: [a.label, b.label],
            texts: [a.text, b.text]}}));
        """)
        assert got["labels"][0] != got["labels"][1]
        assert got["texts"][0] == got["texts"][1], (
            "both shorten alike; only the identity keeps them apart")

    def test_the_slot_budget_is_divided_by_the_advance_not_compared_with_it(self):
        # #630 review finding 2: `adv` is a WIDTH per character. Treating it
        # as a character count (Math.max(adv, ...)) sized the cluster off the
        # font instead of off the room available, so a large advance overflowed
        # the slot and shortened nothing.
        widths = [
            _node(f"""
              console.log(JSON.stringify(window.panelLayout.clusterAt(
                'claude-sonnet-4-6', 14, {adv}, 300).width));
            """)
            for adv in (4.0, 6.382, 12.0, 20.0, 40.0)
        ]
        for width in widths:
            assert width <= 300, f"cluster overflowed its slot: {widths}"

    def test_a_very_wide_advance_still_shortens_one_of_the_two_labels(self):
        got = _node("""
          const c = window.panelLayout.clusterAt(
            'claude-sonnet-4-6', 14, 40.0, 300);
          console.log(JSON.stringify({w: c.width, count: c.countText,
            text: c.text}));
        """)
        assert got["w"] <= 300
        assert got["count"].endswith("…") or got["text"].endswith("…")

    def test_clusters_that_fit_share_one_row(self):
        got = _node(f"""
          const p = window.panelLayout.packLegend(
            [{{model: 'claude-opus-4-8', count: 12}},
             {{model: 'claude-sonnet-4-5', count: 8}}], {ADV}, 1304, 16, 18);
          console.log(JSON.stringify({{rows: p.rows.length,
            height: p.height, width: p.width, n: p.rows.flat().length}}));
        """)
        assert got["rows"] == 1
        assert got["n"] == 2
        assert got["height"] == 16
        assert got["width"] <= 1304

    def test_clusters_that_do_not_fit_wrap_to_a_second_row(self):
        # What the fixed 270px pitch got wrong at phone width: the second
        # cluster landed on a row the fixed-height svg clipped away. Here the
        # row count is reported so the panel can size the svg to it.
        got = _node(f"""
          const p = window.panelLayout.packLegend(
            [{{model: 'claude-sonnet-4-6', count: 14}},
             {{model: 'claude-opus-4-8', count: 12}}], {ADV}, 239, 16, 18);
          console.log(JSON.stringify({{rows: p.rows.length,
            ys: p.rows.map(r => r.map(c => c.y)),
            width: p.width}}));
        """)
        assert got["rows"] == 2
        assert got["ys"] == [[0], [16]]
        # Each row holds at most one cluster here, and it fits its width.
        assert got["width"] <= 239

    def test_no_row_is_packed_wider_than_the_available_width(self):
        got = _node(f"""
          const models = ['claude-sonnet-4-6', 'gpt-5-codex', 'kimi-k2-turbo'];
          const p = window.panelLayout.packLegend(
            models.map((m, i) => ({{model: m, count: 10 * (i + 1)}})),
            {ADV}, 300, 16, 18);
          console.log(JSON.stringify({{rows: p.rows.map(r => r.map(c =>
            c.x + c.width)), height: p.height}}));
        """)
        assert got["rows"], "no legend row was produced"
        for row in got["rows"]:
            assert row, "an empty row would claim vertical space it does not use"
            assert max(row) <= 300

    def test_the_rows_handed_back_carry_the_placement_the_panel_draws_at(self):
        got = _node(f"""
          const p = window.panelLayout.packLegend(
            [{{model: 'gpt-5-codex', count: 8}}], {ADV}, 239, 16, 18);
          const c = p.rows[0][0];
          console.log(JSON.stringify({{x: c.x, y: c.y, label: c.label,
            text: c.text, countText: c.countText, width: c.width}}));
        """)
        assert got == {"x": 0, "y": 0, "label": "gpt-5-codex",
                       "text": "gpt-5-codex",
                       "countText": "median (8 files)", "width": got["width"]}
        assert got["width"] <= 239

    def test_nothing_to_legend_is_no_rows_rather_than_a_crash(self):
        # An empty comparison ("select models above to compare") must render
        # the panel, not throw inside the pack.
        got = _node(
            "console.log(JSON.stringify(window.panelLayout.packLegend([], 6, 239, 16, 18)))"
        )
        assert got == {"rows": [], "width": 0, "height": 0}

    def test_a_single_cluster_wider_than_the_slot_still_gets_a_row(self):
        # Dropping an entry the reader cannot see is worse than one they can:
        # it gets its own row, shortened to what fits.
        got = _node(f"""
          const p = window.panelLayout.packLegend(
            [{{model: 'a-very-long-model-identifier-indeed', count: 3}}],
            {ADV}, 120, 16, 18);
          console.log(JSON.stringify({{rows: p.rows.length,
            width: p.width, text: p.rows[0][0].text,
            label: p.rows[0][0].label}}));
        """)
        assert got["rows"] == 1
        assert got["width"] <= 120
        assert got["text"].endswith("…")
        assert got["label"] == "a-very-long-model-identifier-indeed"


class TestNodeDrivenCtxAxis:
    # Every test below drives node; the class mark is the file's node skip (#746).
    pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")

    def test_ctx_peak_is_the_largest_turn_across_every_session(self):
        got = _ctx_node(f"""
          const A = window.ctxAxis;
          console.log(JSON.stringify({{
            both: A.ctxPeak({BUNDLE}),
            tall: A.ctxPeak({TALLER}),
            empty: A.ctxPeak([]),
            noSessions: A.ctxPeak(null),
            firstTurnOnly: A.ctxPeak([{{seq: [{{ctx: 7}}, {{ctx: 3}}]}}]),
          }}));
        """)
        assert got["both"] == 210_000, "the peak came from the second session's second turn"
        assert got["tall"] == 500_000
        assert got["empty"] == 0
        assert got["noSessions"] == 0
        # A single-session bundle whose peak is its FIRST turn: a "take the
        # last" reading would answer 3 here.
        assert got["firstTurnOnly"] == 7

    def test_axis_top_is_the_observed_peak_plus_headroom(self):
        # The relationship is computed from the module's own exported headroom,
        # so it pins the rule rather than a restatement of it. The VALUE is
        # pinned once, by literal, below the loop: the module's comment claims
        # 1.1 is the factor the per-session view already used, and a loop that
        # reads HEADROOM back cannot notice that claim going false.
        #
        # Only peaks clear of the MIN_TOP floor take the headroom -- below that
        # the floor wins, which is the next test's subject and is stated here
        # so neither reads as a silent exception to the other.
        got = _ctx_node("""
          const A = window.ctxAxis;
          const out = {};
          for (const p of [1000, 1001, 12_345, 210_000, 500_000, 1_048_576]) {
            out[p] = [A.ctxAxisTop(p), A.ctxAxisTop(p) === p * A.HEADROOM];
          }
          console.log(JSON.stringify({
            headroom: A.HEADROOM,
            // 1.1 has no exact binary form, so `200000 * 1.1` is
            // 220000.00000000003 and an equality assertion would be asserting
            // the rounding, not the headroom. The gap is measured relative to
            // the expected value so it stays a proportional tolerance.
            at200k: A.ctxAxisTop(200000),
            at1m: A.ctxAxisTop(1000000),
            loop: out,
          }));
        """)
        assert got["headroom"] == 1.1, (
            "the axis headroom is "
            f"{got['headroom']}; src/ctx-axis.js states 1.1 is the factor the "
            "per-session view already used, so a session's curve keeps the "
            "height it had")
        for label, got_top, want in (("200k", got["at200k"], 220_000),
                                     ("1M", got["at1m"], 1_100_000)):
            assert abs(got_top - want) <= want * 1e-9, (
                f"ctxAxisTop at {label} is {got_top}, which is not "
                f"{want} (a peak of 10% under the headroom)")
        for peak, (top, is_peak_plus_headroom) in got["loop"].items():
            assert is_peak_plus_headroom, (
                f"ctxAxisTop({peak}) returned {top}, which is not the peak times "
                "the exported headroom")

    def test_an_axis_top_is_always_a_positive_number_a_scale_can_divide_by(self):
        # A zero peak is what an empty model, a model whose sessions carry no
        # ctx, or a panel with nothing checked all produce. Returning 0 here
        # would make every y scale divide by zero; the floor is a division
        # guard and is not a context window (nothing draws it as a line).
        got = _ctx_node("""
          const A = window.ctxAxis;
          const vals = [A.ctxAxisTop(0), A.ctxAxisTop(-5), A.ctxAxisTop(null),
                        A.ctxAxisTop(undefined), A.ctxAxisTop('nonsense'),
                        A.ctxAxisTopFor([]), A.ctxAxisTopFor(null)];
          console.log(JSON.stringify({
            vals,
            finite: vals.every(v => Number.isFinite(v) && v > 0),
            equalsFloor: vals.every(v => v === A.MIN_TOP),
          }));
        """)
        assert got["finite"], got["vals"]
        assert got["equalsFloor"], (
            "the floor is meant to be the same constant for every degenerate "
            f"input, got {got['vals']} against MIN_TOP")

    def test_axis_ticks_start_at_zero_never_pass_the_top_and_step_evenly(self):
        got = _ctx_node("""
          const A = window.ctxAxis;
          const tops = [1000, 1100, 11000, 231000, 550000, 1153796];
          const out = tops.map(top => {
            const t = A.ctxAxisTicks(top, 4);
            const steps = t.slice(1).map((v, i) => v - t[i]);
            return {top, ticks: t, uniform: steps.every(s => s === steps[0])};
          });
          console.log(JSON.stringify({
            out,
            empty: A.ctxAxisTicks(0, 4),
            negative: A.ctxAxisTicks(-10, 4),
          }));
        """)
        for entry in got["out"]:
            ticks = entry["ticks"]
            assert entry["uniform"], f"ticks for {entry['top']} step unevenly: {ticks}"
            assert ticks[0] == 0, f"ticks for {entry['top']} do not start at 0: {ticks}"
            assert ticks[-1] <= entry["top"], (
                f"a tick for {entry['top']} lies past the axis top: {ticks}")
            # A tick above the peak would sit in the headroom band; the top
            # itself may be a tick, and anything higher is spare room drawn
            # as a gridline.
            assert len(ticks) >= 2, f"no room for a gridline at {entry['top']}"
        assert got["empty"] == [0]
        assert got["negative"] == [0]

    def test_the_comparison_scales_from_the_checked_models_observed_peak(self):
        """#648/#649: the comparison's axis is the shared rule over the
        bundle the panel itself builds, not a hand-folded constant.

        Both expressions are extracted from the .jsx and evaluated against
        the real module, so what the assertion reads is what the panel hands
        the rule. The equality side is against ctxAxisTopFor over the SAME
        sessions, and adding a taller model must move it: without that
        second half the equality would hold for an expression that ignores
        its input.
        """
        compare_expr = _y_axis_expression(COMPARE_JSX)
        series_expr = _series_expression(COMPARE_JSX)
        got = _ctx_node(f"""
          const A = window.ctxAxis;
          const one = {BUNDLE};
          const two = {TALLER};
          // The panel's own collaborators: `byModel` is what it is handed and
          // `perTurnStats` the dashboard exports. The stats stub only has to
          // be an object -- the axis expression reads `sessions`, not them.
          const perTurnStats = s => ({{turns: [], median: [], count: [], maxT: 0}});
          let models, byModel;

          models = [{{model: 'a'}}]; byModel = {{a: one}};
          let series = {series_expr};
          const carriesSessions = series[0].sessions === one;
          const axis = {compare_expr}
          const expected = A.ctxAxisTopFor([one]);

          // Re-evaluated with a taller model added: the comparison folds every
          // checked model's sessions and must move.
          models = [{{model: 'a'}}, {{model: 'b'}}];
          byModel = {{a: one, b: two}};
          series = {series_expr};
          const axisTall = {compare_expr}
          const expectedTall = A.ctxAxisTopFor([one, two]);
          console.log(JSON.stringify({{
            axis, expected, equal: axis === expected, carriesSessions,
            axisTall, expectedTall, equalTall: axisTall === expectedTall,
            comparisonMoved: axisTall > axis,
          }}));
        """)
        # Named first, so a bundle that stopped carrying its sessions says so
        # rather than arriving as a mysterious axis disagreement.
        assert got["carriesSessions"], (
            "the comparison panel's `series` bundle no longer carries the "
            "model's `sessions`; its axis reads undefined and falls to the "
            "division-guard floor instead of the data")
        assert got["equal"], (
            f"the comparison's axis ({got['axis']}) is not ctxAxisTopFor over "
            f"the checked sessions (expected {got['expected']})")
        assert got["equalTall"], (
            f"with a taller model checked the axis ({got['axisTall']}) is not "
            f"ctxAxisTopFor over both (expected {got['expectedTall']})")
        # Anti-vacuity: the expression reads its input.
        assert got["comparisonMoved"], (
            "adding a taller model did not raise the comparison's axis, so the "
            "equality above holds for an expression that ignores its input")

    def test_the_session_view_scales_to_its_own_observed_peak(self):
        # The per-session view holds rows rather than sessions, so it takes
        # the peak directly. It is the same rule, and the same headroom the
        # other two use -- which is what keeps a session's curve at the height
        # it had when the cap line was still drawn below it.
        expr = _y_axis_expression(SESSION_JSX)
        got = _ctx_node(f"""
          const A = window.ctxAxis;
          const peakCtx = 210000;
          const yMaxAbs = {expr}
          console.log(JSON.stringify({{
            yMaxAbs,
            matchesRule: yMaxAbs === A.ctxAxisTop(peakCtx),
            matchesPanel: yMaxAbs === A.ctxAxisTopFor([{BUNDLE}]),
          }}));
        """)
        assert got["matchesRule"], got
        assert got["matchesPanel"], (
            "the per-session view and the dashboard panels no longer agree on "
            "the axis for the same observed peak")


def test_no_frontend_file_reads_a_per_model_context_cap():
    # #648 dropped the table. This is an ABSENCE assertion, so it is only
    # worth anything next to a demonstration that the scan fires: the
    # second half runs the same reader over a planted source and requires
    # it to name the line. An absence check whose oracle cannot be shown
    # live is a suite that goes green the day the scan breaks.
    assert not _cap_readers(ROOT / "src")
    assert not _cap_readers(ROOT / "public")
    planted = ROOT / ".ctx-axis-planted"
    planted.mkdir(exist_ok=True)
    (planted / "planted.jsx").write_text(
        "// nothing here\nconst MODEL_CAPS = {'claude-x': 1};\n"
        "function capForModel(m) { return MODEL_CAPS[m]; }\n",
        encoding="utf-8")
    try:
        assert _cap_readers(planted) == [
            "planted.jsx:2", "planted.jsx:3"], (
            "the scan missed a planted cap, so passing it above means "
            "nothing")
    finally:
        (planted / "planted.jsx").unlink()
        planted.rmdir()


def test_the_removed_cap_elements_are_gone_from_every_context_view():
    # Each of these is a string no surviving code path can want: the cap
    # label, the percentage column it fed, the severity ladder computed
    # against it, and the two reference lines drawn at fractions of it.
    needles = ("% cap", "cap}", "ctxAxisTop(cap", "yScale(cap")
    for view in (GRID_JSX, COMPARE_JSX, SESSION_JSX):
        # Comments stripped, like the cap-name scan above: the comparison
        # panel now carries a comment about the cap line it removed, and
        # the next editor to write `yScale(cap` while explaining something
        # must not turn the suite red for saying so.
        text = _strip_js_comments(view.read_text(encoding="utf-8"))
        for needle in needles:
            assert needle not in text, (
                f"{view.name} still contains {needle!r}")


def test_the_axis_module_is_loaded_by_the_page():
    html = (ROOT / "public" / "index.html").read_text(encoding="utf-8")
    assert '<script src="/src/ctx-axis.js"></script>' in html, (
        "public/index.html does not load src/ctx-axis.js, so every "
        "window.ctxAxis call in the panels is undefined at render time")


def test_the_removed_cap_is_not_still_declared_as_an_eslint_global():
    # eslint.config.mjs declared `capForModel` as a browser global. The
    # name it named is gone, and a declaration left behind is how a
    # reintroduced cap sails past the lint gate unnoticed.
    assert "capForModel" not in ESLINT_CONFIG.read_text(encoding="utf-8")
