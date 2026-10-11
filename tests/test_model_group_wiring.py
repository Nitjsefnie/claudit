"""Issue #936 — Cost/Tokens by Model group to one row per model by default.

The two panels used to draw one flat row per (model, host) pair, which
turns an OpenRouter deploy into a wall of near-identical rows for one
model. They now collapse to ONE row per model — summed value, today's
descending sort, today's share-of-panel labels — and a single checkbox
per panel flips the whole panel back to today's per-host rows. Hovering a
collapsed multi-host row lists each host's share of that MODEL's total.

Neither half is reachable by running the suite the usual way: node cannot
parse the JSX and nothing here renders React (the boundary
test_panel_wiring.py documents), so a panel can pass every other test
while drawing the wrong rows. The pure grouping/expand/hover logic is
therefore a plain module driven through node the way
tests/test_model_select_js.py drives src/model-select.js, and the wiring
that cannot run — the mounts, the ONE checkbox, the hover lines — is
pinned at source level.

The single-checkbox shape is the point of the panel-level control: a
per-row toggle was considered and rejected (maintainer, 2026-10-11), so
these pins fail on either a second checkbox or a checkbox that has moved
into the row loop.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GROUP_JS = ROOT / "src" / "model-group.js"
ADVANCE_JS = ROOT / "src" / "mono-advance.js"
CHARTS = ROOT / "src" / "dashboard-charts.jsx"
APP = ROOT / "src" / "app.jsx"
INDEX = ROOT / "public" / "index.html"

# The colour lookup the browser already has; the module reads it off
# window, so node hands it the same shape.
_PRELUDE = (
    "global.window = { modelColors: { 'm-a': '#aaa', 'm-b': '#bbb',"
    " 'm-c': '#ccc' } };\n"
    "require(" + repr(str(GROUP_JS)) + ");\n"
)


def _node(script: str):
    # Over STDIN, not -e: on Windows a raw backslash path inside a -e argv
    # is eaten as JS escapes (see test_model_select_js._node).
    proc = subprocess.run(
        ["node"], input=script, capture_output=True, text=True, timeout=60,
        check=False, encoding="utf-8", errors="strict",
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _call(expr: str):
    return _node(_PRELUDE + f"console.log(JSON.stringify({expr}));\n")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_rows_sharing_a_model_collapse_to_their_summed_value():
    """One row per model, sorted desc on the SUMMED value — not on any
    single host's value, which would reorder the panel against today's
    descending sort whenever one model is served by several hosts."""
    rows = _call(
        "window.modelGroup.group({"
        "'m-a · DeepInfra': 75, 'm-a · StreamLake': 15,"
        "'m-b': 40, 'm-c · OpenRouter': 10})")
    assert [r["label"] for r in rows] == ["m-a", "m-b", "m-c · OpenRouter"]
    assert [r["value"] for r in rows] == [90, 40, 10]
    # The collapsed row carries the hosts it stands for, biggest first,
    # and every row keeps its MODEL's colour: a host-split row has no
    # fixedColors entry of its own (window.modelColors is keyed by model).
    m_a = rows[0]
    assert m_a["color"] == "#aaa"
    assert [(p["provider"], p["value"]) for p in m_a["parts"]] == [
        ("DeepInfra", 75), ("StreamLake", 15)]
    # Single-entry rows render exactly as today: the original label (a
    # bare id stays bare, a host-split label stays split), no parts, no
    # per-row affordance of any kind.
    assert "parts" not in rows[1]
    assert rows[1]["color"] == "#bbb"
    assert "parts" not in rows[2]
    assert rows[2]["color"] == "#ccc"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_zero_valued_rows_drop_before_the_grouping():
    """Today's panels drop zero rows first; dropping them AFTER grouping
    would leave a zero-value model in the chart, and a surviving host
    would be relabelled as if the model had only that one host."""
    rows = _call(
        "window.modelGroup.group({'m-a · DeepInfra': 5,"
        "'m-a · StreamLake': 0, 'm-a · Zed': -2})")
    assert len(rows) == 1
    assert rows[0]["label"] == "m-a · DeepInfra"
    assert rows[0]["value"] == 5
    assert "parts" not in rows[0]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_checked_is_todays_per_host_rows_in_todays_order():
    """The checkbox flips the panel back to what it drew before #936: one
    row per (model, host), globally descending — NOT the model's hosts
    stacked in place under their collapsed row, which would read as a
    different sort to anyone comparing against a saved screenshot."""
    grouped = (
        "window.modelGroup.group({"
        "'m-a · DeepInfra': 75, 'm-a · StreamLake': 15,"
        "'m-b': 40})")
    collapsed = _call(grouped)
    assert _call(f"window.modelGroup.shown({grouped}, false)") == collapsed
    flat = _call(f"window.modelGroup.shown({grouped}, true)")
    assert [(r["label"], r["value"]) for r in flat] == [
        ("m-a · DeepInfra", 75), ("m-b", 40), ("m-a · StreamLake", 15)]
    # Expanding keeps the collapsed row's model colour on its hosts.
    assert [r["color"] for r in flat] == ["#aaa", "#bbb", "#aaa"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_the_hover_lists_each_hosts_share_of_the_MODEL_total():
    """`DeepInfra 928.03M (75%)` — the share is of the model's own total,
    not of the panel: the row already states its panel share in the bar
    label, and a host line repeating it would say nothing about how the
    model splits. A row with no hosts contributes no lines, so every
    other HBar mount's tooltip is untouched."""
    lines = _call(
        "window.modelGroup.hostLines("
        "{value: 100, parts: ["
        "{provider: 'DeepInfra', value: 75},"
        "{provider: 'StreamLake', value: 25}]}, v => v + 'u')")
    assert lines == [["DeepInfra", "75u (75.0%)"],
                     ["StreamLake", "25u (25.0%)"]]
    assert _call("window.modelGroup.hostLines({value: 10}, v => 'x')") == []
    # A host nobody named keeps the bare model label — the row is the
    # lane's own row, with no invented host name (modelProviderLabel).
    assert _call(
        "window.modelGroup.hostLines({value: 4, parts: [{label: 'm-b',"
        "provider: null, value: 4}]}, v => v + 'u')") == [["m-b", "4u (100.0%)"]]


def _strip(src: str) -> str:
    """Drop `//` line comments so prose ABOUT a mistake is not read as the
    mistake (the lookbehind spares `https://`)."""
    return re.sub(r"(?<![:'\"\w])//.*$", "", src, flags=re.M)


def _mount(title: str) -> tuple[str, str]:
    """The 400-char window after a panel's title, and the 40 chars before
    it — the slice test_panel_wiring.py established for a mount."""
    src = _strip(APP.read_text(encoding="utf-8"))
    idx = src.index(f'title="{title}"')
    return src[idx:idx + 400], src[max(0, idx - 40):idx]


@pytest.mark.parametrize("title,rows_name,map_name,part_fmt", [
    ("Cost by Model", "costByModel", "byModel",
     "partFmt={v => window.humanCurrency(v)}"),
    ("Tokens by Model", "tokensByModel", "tokensByModel",
     "partFmt={v => window.humanFmt(v)}"),
])
def test_both_panels_mount_the_grouped_rows_with_one_panel_control(
        title, rows_name, map_name, part_fmt):
    """Both mounts pass the grouped rows, ask for the grouping treatment,
    and hand HBar the value formatter the host lines need — the cost
    panel in dollars, the token panel in humanFmt. Dropping partFmt from
    either mount would render the cost panel's host values as bare token
    counts (928.03M where the bar says $11.5M); spelling it as a ROW
    formatter (r => …(r.value)) instead is the bug the browser probe
    caught: hostLines hands it a value, so humanCurrency received
    undefined and the cost panel's hover threw TypeError on toFixed."""
    window, before = _mount(title)
    assert "<window.HBar" in before
    assert f"rows={{{rows_name}}}" in window
    assert "groupByModel" in window, f"{title} does not opt into grouping"
    assert part_fmt in window, (
        f"{title} does not pass partFmt as a VALUE formatter ({part_fmt})")
    # The rows themselves are the module's, sorted and summed there —
    # one implementation behind both panels, not a copy per mount.
    assert f"window.modelGroup.group(totals.{map_name})" in _strip(
        APP.read_text(encoding="utf-8"))


def test_the_group_module_loads_before_the_panels_that_call_it():
    """A module reached only through window still has to be INSTALLED:
    index.html loads each /src/* as its own script, and app.jsx calls
    window.modelGroup at render time, so a dropped tag throws on the
    first Overview paint rather than failing any test."""
    html = INDEX.read_text(encoding="utf-8")
    assert '<script src="/src/model-group.js"></script>' in html
    assert html.index("/src/model-group.js") < html.index(
        "/src/dashboard-charts.jsx")
    assert html.index("/src/model-group.js") < html.index("/src/app.jsx")


def _hbar_body() -> str:
    """Just HBar's body: its declaration to the next top-level
    `function`/`window.` (the slice test_panel_wiring._panel_src uses).

    Scoping is the point of both pins below: a count over the whole
    module trips on an unrelated component's own checkbox and reports
    HBar's as the culprit, while a slice that runs past HBar (to the
    `window.HBar = HBar;` export, say) swallows VBar on the way.
    """
    src = _strip(CHARTS.read_text(encoding="utf-8"))
    start = src.index("function HBar(")
    nxt = re.search(r"^(?:function |window\.)", src[start + 1:], re.M)
    end = start + 1 + nxt.start() if nxt else len(src)
    return src[start:end]


def test_the_panel_carries_exactly_one_checkbox_outside_the_row_loop():
    """One control flips the WHOLE panel (maintainer, 2026-10-11 — the
    per-row checkbox the issue first described was rejected). Three
    regressions this pins: a second checkbox, a checkbox that moved into
    the row map (one per row again), and a control that is no longer
    gated on the panel having opted into grouping — every other HBar
    mount (Cost/Tokens by Agent Type) must stay untouched.

    The count and the position check run over HBar's body alone, so a
    future checkbox in another component of this module (a legend
    expander, a table control) is that component's business and does not
    fail here with a message naming the wrong panel."""
    body = _hbar_body()
    assert body.count('type="checkbox"') == 1, (
        "HBar renders more than one checkbox -- the control belongs to "
        "the panel as a whole, not to its rows")
    gate = re.search(
        r"groupByModel && rows\.some\(r => r\.parts\) && \(\s*"
        r"<label data-model-group-toggle", body)
    assert gate, (
        "the toggle is not gated on groupByModel AND on there being a "
        "model worth splitting")
    # Declared once, OUTSIDE the svg: everything between the row map
    # (the `shown.map(` that follows the plot rect) and the closing
    # </svg> is per-row markup, which is where the per-row design the
    # maintainer rejected lived.
    loop = body.index("shown.map(", body.index('data-role="plot"'))
    svg_end = body.index("</svg>", loop)
    assert not loop < gate.start() < svg_end, (
        "the checkbox sits inside the svg's row loop -- that is the "
        "per-row design the maintainer rejected")
    assert re.search(r"const \[open, setOpen\] = React\.useState\(false\)",
                     body[:1200]), (
        "the panel's open state is not declared in HBar")
    # Default UNCHECKED: the grouped rows are what the mount passes.
    assert re.search(r"checked=\{open\}", body)


# HBar's readers of a row LIST. `rows` is the mount's grouped input;
# only `shown` (the list the checkbox controls) may be read for geometry,
# scale, a11y, the row map or the hover's tip.
_ROW_USE = re.compile(
    r"\b(rows|shown)\.(?:length|map|reduce|slice)\b"
    r"|\browTip\(\s*(rows|shown)\[hover\]")


def test_every_hbar_row_consumer_reads_the_rendered_rows():
    """The mount hands HBar GROUPED rows; `shown` is what it draws — the
    same list collapsed, the per-host rows once the box is checked. A
    consumer reverted to `rows` draws the ungrouped list while every
    other pin stays green: the svg height, the bar scale, the a11y
    summary, the row map and the hover would all disagree with the
    checkbox's state, which is exactly the shape a bad merge reverts
    silently.

    One deliberate `rows.` reader is NOT in this pattern, and must not
    be "fixed" into one: the checkbox gate's `rows.some(r => r.parts)`
    reads the GROUPED input on purpose — `shown` carries no `parts` once
    expanded, so a gate on it would unmount the control and strand the
    panel open with no way back.

    Pinned as a single pattern over HBar's own body, so a revert
    anywhere in the component trips it and the failure names the exact
    expression that still reads `rows`."""
    body = _hbar_body()
    hits = [m.group(0) for m in _ROW_USE.finditer(body)]
    assert len(hits) >= 6, (
        f"found only {len(hits)} row consumers in HBar ({hits}) -- too "
        f"few to mean anything; the pin would pass vacuously")
    wrong = [h for h in hits
             if h.startswith("rows.") or h.startswith("rowTip(rows")]
    assert not wrong, (
        f"HBar reads the raw grouped rows here: {wrong} -- these must "
        f"read `shown`, the list the panel's checkbox actually controls")


def test_the_collapsed_rows_hover_lines_come_from_the_module():
    """rowTip is shared by every HBar mount, so the host lines are an
    additive spread the module returns EMPTY for rows without hosts —
    not a rewrite of the tip the other panels rely on."""
    charts = _strip(CHARTS.read_text(encoding="utf-8"))
    start = charts.index("function HBar(")
    end = charts.index("window.HBar = HBar;", start)
    body = charts[start:end]
    assert "partFmt" in body.split(") {", 1)[0], (
        "HBar takes no partFmt prop, so the host lines cannot format a "
        "cost panel's values")
    assert "window.modelGroup.hostLines(" in body
    assert re.search(r"const shown = groupByModel \? "
                     r"window\.modelGroup\.shown\(rows, open\) : rows;", body), (
        "HBar does not expand through the module -- a second expansion "
        "path would drift from the one node tests drive")


def test_the_monospace_advance_probe_lives_where_the_size_ratchet_put_it():
    """dashboard-charts.jsx and app.jsx sit exactly on their recorded
    size entries (.github/ci-thresholds.json; entries only ever move
    DOWN), so #936 cannot add lines to either without relocating some.
    monoAdvancePx — a self-contained measurement helper used only by
    dashboard-charts.jsx — moved to its own module for the room; this
    pins the seam (call site, export, load order) so the next reader
    finds it and a re-inline fails the size gate at its source."""
    charts = _strip(CHARTS.read_text(encoding="utf-8"))
    assert "function monoAdvancePx(" not in charts
    assert "window.monoAdvancePx(" in charts
    html = INDEX.read_text(encoding="utf-8")
    assert '<script src="/src/mono-advance.js"></script>' in html
    assert html.index("/src/mono-advance.js") < html.index(
        "/src/dashboard-charts.jsx")
    assert ADVANCE_JS.is_file()
    assert re.search(r"^function monoAdvancePx\(", ADVANCE_JS.read_text(
        encoding="utf-8"), re.M), (
        "mono-advance.js does not define the probe top-level, so the "
        "cross-file globals check finds no definition for it")
