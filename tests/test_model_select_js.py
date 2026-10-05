"""The single-chart model panels: selection rule, shape, and module pins.

Issues #649/#652/#638: the Per-Session Context Growth panel drew one
sub-chart per model (a 17-row grid at 33 models) and so did Tool Error
Rate. Both are now ONE chart per panel with model checkboxes, on shared
axes, so a panel's height no longer grows with the model count. The
height pin lives here as a SELECTION rule (the cheapest thing that would
fail on regression): the default checked set is the top 2 by the sort
order given, whatever the model count — a reintroduced per-model grid or
an all-models default blows both.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODEL_SELECT_JS = ROOT / "src" / "model-select.js"
RATE_SERIES_JS = ROOT / "src" / "rate-series.js"
EXTRA = ROOT / "src" / "dashboard-charts-extra.jsx"
COMPARISON = ROOT / "src" / "context-growth-comparison.jsx"
TOOL_PANEL = ROOT / "src" / "tool-error-panel.jsx"
INDEX = ROOT / "public" / "index.html"
GUARD = ROOT / "scripts" / "ci" / "panel_layout.mjs"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available"
)


def _node(script: str):
    # Over STDIN, not -e, and the path embedded with repr(): on Windows a
    # raw backslash path inside a -e argv is eaten as JS escapes, and the
    # 32k CreateProcess ceiling looms (see test_panel_layout_js._node).
    # encoding="utf-8" is load-bearing: text=True alone decodes with the
    # locale encoding, cp1252 on a windows-latest runner.
    proc = subprocess.run(
        ["node"], input=script, capture_output=True, text=True, timeout=60,
        check=False, encoding="utf-8", errors="strict",
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _node_lists(expr: str):
    return _node(
        "global.window = {};\n"
        f"require({str(MODEL_SELECT_JS)!r});\n"
        f"console.log(JSON.stringify({expr}));\n"
    )


# -- The selection rule (node-driven) ---------------------------------

def test_default_selection_is_top_two_whatever_the_model_count():
    """#649/#652: the default checked set is top-2, for 2 .. 40 models.

    This is the height pin in its cheapest form: the panels render one
    chart whose height follows the CHECKED count, so an all-models
    default is the regression that grows a panel with its model count.
    A per-model grid reintroduced is caught by the source pins below and
    by the rendered-layout guard in CI.
    """
    sizes = _node_lists(
        "Array.from({length: 39}, (_, i) => "
        "window.modelSelect.topDefaultSelection("
        "  Array.from({length: i + 2}, (_, j) => ({model: 'm' + j, count: j})), "
        "  {}, 2).size)")
    assert sizes == [2] * 39


def test_an_override_layers_over_the_default():
    got = _node_lists(
        "Array.from(window.modelSelect.topDefaultSelection("
        "  [{model: 'a', count: 9}, {model: 'b', count: 8}, {model: 'c', count: 7}], "
        "  {c: true}, 2))")
    assert got == ["a", "b", "c"]


def test_unchecking_a_default_model_removes_it():
    got = _node_lists(
        "Array.from(window.modelSelect.topDefaultSelection("
        "  [{model: 'a', count: 9}, {model: 'b', count: 8}], "
        "  {a: false}, 2))")
    assert got == ["b"]


# -- Shape pins: one chart, no per-model grid -------------------------

def _strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"//[^\n]*", "", src)


def _panel_src(name: str, src: str) -> str:
    start = src.index(f"function {name}(")
    nxt = re.search(r"^(?:function |window\.|const \w+ =)", src[start + 1:], re.M)
    end = start + 1 + nxt.start() if nxt else len(src)
    return src[start:end]


def test_the_growth_panel_has_no_per_model_grid():
    """#649: the sub-panel grid is gone — no ContextSubPanel anywhere,
    no per-model row pairing in the panel, one ComparisonRow mount."""
    src = _strip_comments(EXTRA.read_text(encoding="utf-8"))
    assert "ContextSubPanel" not in src, (
        "ContextSubPanel survives — the per-model grid is supposed to be "
        "gone (#649)")
    panel = _panel_src("ContextGrowthPanel", src)
    assert "models.slice(i, i + 2)" not in src, (
        "the per-model row pairing is back")
    assert panel.count("<window.ComparisonRow") == 1


def test_the_growth_chart_draws_the_full_breakdown():
    """#649: median, p25–p75 band and p90 per checked model, and the
    session traces gated by the showSessions prop."""
    src = _strip_comments(COMPARISON.read_text(encoding="utf-8"))
    assert "showSessions" in src, "the sessions toggle prop is missing"
    body = _panel_src("ComparisonRow", src)
    for needle, what in [
        ("s.stats.p25", "the p25–p75 band"),
        ("s.stats.p90", "the p90 line"),
        ("s.stats.median", "the median line"),
    ]:
        assert needle in body, f"{what} is not drawn from the per-turn stats"
    assert "s.sessions.map(" in body or "s.stats" in body


def test_the_growth_sessions_toggle_defaults_off():
    """#649 point 4: the sessions toggle is OFF by default."""
    src = _strip_comments(EXTRA.read_text(encoding="utf-8"))
    panel = _panel_src("ContextGrowthPanel", src)
    assert re.search(r"useState\(false\)", panel), (
        "the sessions toggle does not default to off")


def test_the_growth_tooltip_names_the_breakdown():
    """#649 point 5: the tooltip lists median, p25, p75, p90 and the
    session count per checked model."""
    src = _strip_comments(COMPARISON.read_text(encoding="utf-8"))
    body = _panel_src("ComparisonRow", src)
    for needle in ["p25", "p75", "p90", "median"]:
        assert needle in body, f"tooltip lost {needle}"
    assert "count" in body


def test_the_tool_panel_is_its_own_module_on_the_page():
    """#652: the panel lives in its own module (dashboard-charts-extra
    .jsx sits at its measured size entry, so growth moves out first) and
    the page loads the module after the theme's file."""
    assert TOOL_PANEL.exists()
    html = INDEX.read_text(encoding="utf-8")
    tool_at = html.index("/src/tool-error-panel.jsx")
    charts_at = html.index("/src/dashboard-charts.jsx")
    app_at = html.index("/src/app.jsx")
    assert charts_at < tool_at < app_at
    assert html.index("/src/model-select.js") < tool_at


def test_the_tool_panel_is_one_chart_with_checkboxes():
    """#652: one chart, model checkboxes (the shared LegendCheckboxRow),
    a per-tool toggle that defaults off, and the aggregate line drawn
    for every checked model."""
    src = _strip_comments(TOOL_PANEL.read_text(encoding="utf-8"))
    panel = _panel_src("ToolErrorRatePanel", src)
    # Two pickers, both through the shared rule: models (top 2 by
    # settled calls) and tools (top 3 by calls).
    assert panel.count("window.modelSelect.topDefaultSelection") == 2
    assert re.search(r"topDefaultSelection\(\s*models, modelOverrides, 2\)", panel), (
        "the tool panel's default checked set is not top-2")
    assert re.search(r"topDefaultSelection\(\s*toolPickerEntries, toolOverrides, 3\)",
                     panel), (
        "the tool picker's default is not top-3")
    assert "window.LegendCheckboxRow" in src
    assert re.search(r"useState\(false\)", panel), (
        "the per-tool toggle does not default to off")
    assert "strokeDasharray" in src, "per-tool lines carry no dash pattern"


def test_the_tool_panel_never_returns_to_a_per_model_grid():
    """#652's general shape: no sub-chart-per-model pairing anywhere in
    the tool module — a third grid cannot ship through this file."""
    src = _strip_comments(TOOL_PANEL.read_text(encoding="utf-8"))
    assert "slice(i, i + 2)" not in src
    assert "ToolErrorSubPanel" not in src


def test_the_638_sub_panel_legend_exemption_is_gone():
    """#638: the sub-panels and their overflowing legends are deleted, so
    the rendered-layout guard's ledger no longer carries the exemption —
    any legend overflow the replacement panel shows fails the leg."""
    src = GUARD.read_text(encoding="utf-8")
    assert "issue: 638" not in src, (
        "the #638 exemption survives a panel that no longer renders the "
        "violating sub-panels")
    assert "638" not in src


def test_the_shared_selection_helper_is_node_runnable_plain_js():
    """The selection rule lives in plain JS the suite can drive (the
    panels are JSX and node parses none of them), and the panels reach it
    through window at render time."""
    assert MODEL_SELECT_JS.exists()
    src = MODEL_SELECT_JS.read_text(encoding="utf-8")
    assert "window.modelSelect" in src
    assert "import" not in src.split("window.modelSelect")[0], (
        "model-select.js must stay plain JS (no imports) so node can "
        "require it")


# -- The rate/EMA math (node-driven, src/rate-series.js) --------------

def _rs(expr: str):
    return _node(
        "global.window = {};\n"
        f"require({str(RATE_SERIES_JS)!r});\n"
        f"console.log(JSON.stringify({expr}));\n"
    )


def _md():
    """One model's grouped data: two tools over three buckets, the third
    bucket sparse for t2 and empty for t1."""
    return """{
      buckets: [100, 200, 300],
      perBucketTool: new Map([
        [100, new Map([['t1', {n_total: 10, n_error: 1}],
                       ['t2', {n_total: 4, n_error: 2}]])],
        [200, new Map([['t1', {n_total: 10, n_error: 0}]])],
        [300, new Map([['t1', {n_total: 5, n_error: 5}],
                       ['t9', {n_total: 1, n_error: 0}]])],
      ]),
      totalsByTool: new Map([['t1', 25], ['t2', 4], ['t9', 1]]),
    }"""


def test_aggregate_folds_every_tool_and_skips_sparse_buckets():
    got = _rs(
        f"Array.from(window.rateSeries.buildModelSeries({_md()},"
        "['t1', 't2'], ['t9'], '__OTHER__').get('__AGG__'))")
    # Bucket 100: 3/14; 200: 0/10; 300: 5/6 (t9 folds in). Every bucket
    # is non-sparse for the aggregate, so all three survive.
    assert [(p["rate"], p["n_total"]) for p in got] == [
        (3 / 14, 14), (0.0, 10), (5 / 6, 6)]


def test_a_tool_sequence_drops_buckets_without_settled_calls():
    got = _rs(
        f"Array.from(window.rateSeries.buildModelSeries({_md()},"
        "['t1', 't2'], ['t9'], '__OTHER__').get('t2'))")
    # t2 settled only at bucket 100 (2/4); 200 and 300 carry no t2 row.
    assert [(p["rate"], p["n_total"]) for p in got] == [(0.5, 4)]


def test_other_folds_the_tools_outside_the_picker():
    got = _rs(
        f"Array.from(window.rateSeries.buildModelSeries({_md()},"
        "['t1', 't2'], ['t9'], '__OTHER__').get('__OTHER__'))")
    assert [(p["rate"], p["n_total"]) for p in got] == [(0.0, 1)]


def test_the_other_series_is_absent_when_nothing_folds():
    keys = _rs(
        f"Array.from(window.rateSeries.buildModelSeries({_md()},"
        "['t1', 't2'], [], '__OTHER__').keys())")
    assert keys == ["__AGG__", "t1", "t2"]


def test_ema_carries_the_first_point_and_recurses_with_alpha():
    got = _rs(
        "(() => { const m = new Map([['s', ["
        "{rate: 1}, {rate: 0}, {rate: 1}]]]); "
        "window.rateSeries.emaSeries(m, 0.5); "
        "return Array.from(m.get('s'), p => p.ema); })()")
    # ema[0] = 1 (carried, no warm-up); then 0.5*0 + 0.5*1 = 0.5;
    # 0.5*1 + 0.5*0.5 = 0.75.
    assert got == [1, 0.5, 0.75]
