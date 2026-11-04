"""The interaction guard's height snapshot and final log, driven through node."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from tests.node_run import run_node

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "scripts" / "ci" / "panel_interactions_report.mjs"
GUARD = ROOT / "scripts" / "ci" / "panel_interactions.mjs"
ROWS_HEADER = "    rows (marked when absent from the other world):"

# A DOM double modelling only what the capture reads: an HTML panel with
# three child regions under a data-max-h host, an svg-rooted chart, and a
# zero-height panel the capture must skip.
DOM = """
  globalThis.HTMLElement = class {};
  const box = h => () => ({height: h});
  const node = (proto, fields, attrs = {}) => Object.assign(Object.create(proto), {
    getAttribute: name => attrs[name] ?? null,
    hasAttribute: name => name in attrs, ...fields});
  const html = (fields, attrs) => node(HTMLElement.prototype, fields, attrs);
  const host = html({}, {'data-max-h': '640'});
  const page = html({getBoundingClientRect: box(604.4),
    closest: sel => (sel === '[data-max-h]' ? host : null),
    innerText: ` Title \\n\\n${'x'.repeat(300)}\\n`
      + Array.from({length: 44}, (_, i) => `row ${i}`).join('\\n'),
    children: [
      html({localName: 'div', getBoundingClientRect: box(130.2)},
        {'data-role': 'title', class: 'ignored'}),
      html({localName: 'div', getBoundingClientRect: box(56)}, {class: ' a  b '}),
      html({localName: 'table', getBoundingClientRect: box(80)})]},
    {'data-panel': 'Page performance'});
  const rect = node(Object.prototype, {localName: 'rect', getBoundingClientRect: box(9)});
  const chart = node(Object.prototype, {getBoundingClientRect: box(300),
    closest: () => null, children: [rect, rect],
    querySelectorAll: sel => (sel === 'text'
      ? [{textContent: ' 0 '}, {textContent: ''}, {textContent: '10K'}] : null)},
    {'data-panel': 'Chart'});
  const hidden = html({getBoundingClientRect: box(0)}, {'data-panel': 'Hidden'});
  globalThis.document = {querySelectorAll: sel =>
    (sel === '[data-panel]' ? [page, chart, hidden] : null)};
"""


def _node(body: str):
    """Import the module over STDIN and print `body`'s JSON result."""
    script = f"const mod = await import({json.dumps(MODULE.as_uri())});\n{body}\n"
    proc = run_node(
        ["node", "--input-type=module"], input=script, capture_output=True,
        text=True, timeout=60, check=False, encoding="utf-8", errors="strict",
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return json.loads(proc.stdout)


def _world(label: str, height: int, regions: list[str], rows: list[str]) -> list[str]:
    head = ["    regions:", *regions] if regions else ["    regions: none"]
    return [f"  {label} ({height}px)", *head, ROWS_HEADER, *rows]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_the_capture_keeps_every_row_and_skips_svg_internals():
    """Rows and regions arrive whole, so the report can count what it omits;
    an svg root yields its text nodes and no primitive regions."""
    out = _node(DOM + "console.log(JSON.stringify(mod.capturePanels()));")
    rows = ["Title", "x" * 300, *[f"row {i}" for i in range(44)]]
    assert out == [
        ["Page performance", 604, False, "640", rows, [
            {"label": "1:div[data-role=title]", "h": 130},
            {"label": "2:div.a.b", "h": 56}, {"label": "3:table", "h": 80}]],
        ["Chart", 300, False, None, ["0", "10K"], []]]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_the_height_diagnostic_marks_only_rows_one_world_lacks():
    out = _node("""
      const two = ['P', 173, false, null, ['shared', 'only two'],
        [{label: '1:div[data-role=title]', h: 18}]];
      const many = ['P', 219, false, null, ['shared', 'only many'],
        [{label: '1:div[data-role=title]', h: 31}, {label: '2:table', h: 84}]];
      console.log(JSON.stringify({both: mod.describeHeightGrowth(two, many),
        alone: mod.describeHeightGrowth(['P', 5, false, null, ['r'], []], null)}));
    """)
    assert out["both"] == [
        *_world("2 models", 173, ["      1:div[data-role=title]: 18px"],
                ["      shared", "    - only two"]),
        *_world("30+ models", 219, ["      1:div[data-role=title]: 31px",
                                    "      2:table: 84px"],
                ["      shared", "    + only many"])]
    assert out["alone"] == [*_world("2 models", 5, [], ["    - r"]),
                            "  30+ models (not rendered)"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_the_height_diagnostic_caps_rows_regions_and_widths_at_the_boundary():
    """40 rows, 40 regions, 240-char rows and 100-char labels print whole;
    one more of each is clipped or counted."""
    out = _node("""
      const world = (h, n, row, label) => ['P', h, false, null,
        [row, ...Array.from({length: n - 1}, (_, i) => `r${i}`)],
        [{label, h: 5}, ...Array.from({length: n - 1}, (_, i) => ({label: `g${i}`, h: 1}))]];
      console.log(JSON.stringify(mod.describeHeightGrowth(
        world(10, 40, 'a'.repeat(240), 'L'.repeat(100)),
        world(20, 41, 'b'.repeat(241), 'M'.repeat(101)))));
    """)
    rest = [f"r{i}" for i in range(39)]
    groups = [f"      g{i}: 1px" for i in range(39)]
    assert out == [
        *_world("2 models", 10, [f"      {'L' * 100}: 5px", *groups],
                ["    - " + "a" * 240, *[f"      {r}" for r in rest]]),
        *_world("30+ models", 20, [f"      {'M' * 99}…: 5px", *groups,
                                   "      ... 1 region(s) omitted"],
                ["    + " + "b" * 239 + "…", *[f"      {r}" for r in rest],
                 "      ... 1 row(s) omitted"])]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_the_report_prints_verdicts_and_summary_before_capped_blocks():
    """Every FAIL line and the summary come first; each height-growth finding
    then gets one block per width from that width's snapshot, eight at most."""
    out = _node("""
      const entry = (name, h) => [name, h, false, null, [], []];
      const snapshots = new Map([320, 375, 1024, 1440].map(w => [w, {
        two: [entry('Page performance', 1), ...['Page performance', 'Agents', 'X']
          .map(n => entry(n, w))],
        many: ['Page performance', 'Agents', 'X'].map(n => entry(n, w + 12))}]));
      const f = (kind, panel, detail, widths, issue = null) =>
        ({kind, panel, detail, widths: new Set(widths), issue});
      console.log(JSON.stringify(mod.reportLines([
        f('height-growth', 'Page performance', 'grew', [1440, 375]),
        f('cold-cls', '(page)', 'cls 0.2', [320], 77),
        f('height-growth', 'Agents', 'over', [1440, 320, 1024, 375]),
        f('height-growth', 'X', 'grew', [375, 320, 1440, 1024]),
      ], [{issue: 9, kind: 'hover-style', panel: null}], 3, snapshots)));
    """)
    blocks = []
    for name, widths in (("Page performance", (375, 1440)),
                         ("Agents", (320, 375, 1024, 1440)), ("X", (320, 375))):
        for width in widths:
            blocks += [f"HEIGHT      {name} at {width}px",
                       *_world("2 models", width, [], []),
                       *_world("30+ models", width + 12, [], [])]
    assert out == [
        "KNOWN cold-cls         (page): cls 0.2  [320px] (filed as #77)",
        "FAIL height-growth    Page performance: grew  [375, 1440px]",
        "FAIL height-growth    Agents: over  [320, 375, 1024, 1440px]",
        "FAIL height-growth    X: grew  [320, 375, 1024, 1440px]",
        "STALE LEDGER  #9 hover-style on every panel never fired — the fix "
        "landed or the sweep cannot see the defect any more. Delete the "
        "line, or fix the sweep.",
        "panel-interactions: 3 new violation(s), known defects #77, "
        "1 STALE ledger entrie(s)",
        *blocks, "... 2 height-growth block(s) omitted"]


def test_the_guard_exits_without_dropping_piped_output():
    """process.exit() drops stdout still queued for a pipe; the guard sets
    the exit code and lets node exit once the browser and server close."""
    src = GUARD.read_text(encoding="utf-8")
    assert "process.exitCode = failures ? 1 : 0;" in src
    assert "process.exit(failures" not in src
