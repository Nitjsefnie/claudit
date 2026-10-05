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
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAYOUT_JS = ROOT / "src" / "panel-layout.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available"
)

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
    proc = subprocess.run(
        ["node"], input=script, capture_output=True, text=True, timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# --- fitText -----------------------------------------------------------

def test_text_that_already_fits_is_returned_unchanged():
    got = _node(
        "console.log(JSON.stringify(window.panelLayout.fitText('Cost (USD)',"
        f" {ADV}, 400)))"
    )
    assert got == "Cost (USD)"


def test_text_too_wide_is_shortened_with_an_ellipsis_and_then_fits():
    got = _node(f"""
      const t = 'claude-sonnet-4-6 vs claude-opus-4-8  ·  median per turn';
      const short = window.panelLayout.fitText(t, {ADV}, 239);
      console.log(JSON.stringify({{short, width: short.length * {ADV}}}));
    """)
    assert got["short"].endswith("…")
    assert len(got["short"]) < 55
    assert got["width"] <= 239


def test_a_width_too_narrow_for_even_one_character_still_yields_one_glyph():
    # The degenerate case must return something drawable, not an empty
    # string: an empty label is a hole where a model name belongs.
    got = _node(f"console.log(JSON.stringify(window.panelLayout.fitText('abc', {ADV}, 2)))")
    assert got == "…"


def test_a_non_positive_advance_or_width_is_passed_through():
    # No measured advance yet (the first paint) or a degenerate width must
    # not crash the panel or blank the title.
    got = _node(
        "console.log(JSON.stringify([window.panelLayout.fitText('abc', 0, 100),"
        " window.panelLayout.fitText('abc', 6, 0), window.panelLayout.fitText(null, 6, 100)]))"
    )
    assert got == ["abc", "abc", ""]


# --- clusterAt ---------------------------------------------------------

def test_the_rule_and_the_count_sit_after_the_labels_own_width():
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


def test_the_count_text_names_the_files_it_summarises():
    got = _node(f"console.log(JSON.stringify(window.panelLayout.clusterAt('m', 1314, {ADV}, 0).countText))")
    assert got == "median (1,314 files)"


def test_a_cluster_wider_than_its_slot_is_shortened_to_fit_it():
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


def test_a_shortened_cluster_keeps_its_identity_and_loses_only_the_text():
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


def test_two_models_that_shorten_alike_stay_two_distinct_clusters():
    got = _node(f"""
      const a = window.panelLayout.clusterAt('claude-opus-4-8', 12, {ADV}, 120);
      const b = window.panelLayout.clusterAt('claude-opus-4-5', 12, {ADV}, 120);
      console.log(JSON.stringify({{labels: [a.label, b.label],
        texts: [a.text, b.text]}}));
    """)
    assert got["labels"][0] != got["labels"][1]
    assert got["texts"][0] == got["texts"][1], (
        "both shorten alike; only the identity keeps them apart")


def test_the_slot_budget_is_divided_by_the_advance_not_compared_with_it():
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


def test_a_very_wide_advance_still_shortens_one_of_the_two_labels():
    got = _node("""
      const c = window.panelLayout.clusterAt(
        'claude-sonnet-4-6', 14, 40.0, 300);
      console.log(JSON.stringify({w: c.width, count: c.countText,
        text: c.text}));
    """)
    assert got["w"] <= 300
    assert got["count"].endswith("…") or got["text"].endswith("…")


# --- packLegend --------------------------------------------------------

def test_clusters_that_fit_share_one_row():
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


def test_clusters_that_do_not_fit_wrap_to_a_second_row():
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


def test_no_row_is_packed_wider_than_the_available_width():
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


def test_the_rows_handed_back_carry_the_placement_the_panel_draws_at():
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


def test_nothing_to_legend_is_no_rows_rather_than_a_crash():
    # An empty comparison ("select models above to compare") must render
    # the panel, not throw inside the pack.
    got = _node(
        "console.log(JSON.stringify(window.panelLayout.packLegend([], 6, 239, 16, 18)))"
    )
    assert got == {"rows": [], "width": 0, "height": 0}


def test_a_single_cluster_wider_than_the_slot_still_gets_a_row():
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