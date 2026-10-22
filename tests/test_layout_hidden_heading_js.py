"""A visually hidden page heading participates in accessibility, not flex gaps."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from tests.node_run import run_node

# The probe consumes element boxes and computed styles; the heading has
# the existing sr-only style's absolute position and nonzero 1px box.
_DOM = """
const element = (name, y, w, h, position = 'static') => ({
  className: name, tagName: 'DIV', children: [], style: {},
  css: {display: 'block', position, rowGap: '14px', marginTop: '0px', marginBottom: '0px'},
  getBoundingClientRect: () => ({x: 0, y, width: w, height: h}),
  getAttribute: () => null,
  querySelector: () => null,
});
const html = element('html', 0, 800, 800);
const dash = element('dashboard', 0, 800, 800);
const hidden = element('sr-only', -1, 1, 1, 'absolute');
const summary = element('dash-summary', 0, 800, 40);
const grid = element('dash-grid', 54, 800, 300);
grid.querySelector = () => ({});
dash.parentElement = html;
dash.children = [hidden, summary, grid];
for (const child of dash.children) child.parentElement = dash;
global.document = {querySelector: () => dash, querySelectorAll: () => dash.children};
global.getComputedStyle = el => el.css;
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_gap_collector_keeps_flow_sections_beside_hidden_heading():
    out = _node(_DOM + """
      const {boxes} = mod.COLLECT_PROBE(mod.COLLECT_SELECTOR);
      console.log(JSON.stringify(boxes.map(b => b.name)));
    """)
    assert out == ["dash-summary", "dash-grid"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_zero_gap_seed_moves_summary_and_grid_instead_of_hidden_heading():
    out = _node(_DOM + """
      const rule = mod.RULES.find(r => r.id === 'vertical-gap');
      const seeded = await rule.seed({evaluate: fn => fn()});
      console.log(JSON.stringify({seeded, margin: grid.style.marginTop}));
    """)
    assert out == {
        "seeded": {"upper": "dash-summary", "lower": "dash-grid", "pulled": 14},
        "margin": "-14px",
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
@pytest.mark.parametrize("gap, expected", [
    (14, []),
    (4, [{"upper": "dash-summary", "lower": "dash-grid", "gap": 4,
          "upperMb": 0, "lowerMt": 0, "expected": 14}]),
])
def test_gap_consistency_ignores_hidden_heading_and_detects_real_drift(gap, expected):
    out = _node(_DOM + f"""
      grid.getBoundingClientRect = () => ({{x: 0, y: {40 + gap}, width: 800, height: 300}});
      const rule = mod.RULES.find(r => r.id === 'section-gap-consistency');
      const findings = await rule.run({{page: {{evaluate: fn => fn()}}}});
      console.log(JSON.stringify(findings));
    """)
    assert out == expected


def _node(body: str):
    module = Path(__file__).resolve().parents[1] / "scripts" / "ci" / "panel_layout_rules.mjs"
    script = f"const mod = await import({json.dumps(module.as_uri())});\n{body}\n"
    proc = run_node(
        ["node", "--input-type=module"], input=script, capture_output=True,
        text=True, timeout=60, check=False, encoding="utf-8", errors="strict",
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return json.loads(proc.stdout)
