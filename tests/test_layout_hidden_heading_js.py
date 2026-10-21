"""A visually hidden page heading participates in accessibility, not flex gaps."""
from __future__ import annotations

import shutil

import pytest

from tests.test_panel_layout_rules_js import _node

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")

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


def test_gap_collector_keeps_flow_sections_beside_hidden_heading():
    out = _node(_DOM + """
      const {boxes} = mod.COLLECT_PROBE(mod.COLLECT_SELECTOR);
      console.log(JSON.stringify(boxes.map(b => b.name)));
    """)
    assert out == ["dash-summary", "dash-grid"]


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
