"""The picker reserves measured controls before choosing chip page boundaries."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from tests.node_run import run_node

ROOT = Path(__file__).resolve().parents[1]
SPACE = ROOT / 'src' / 'picker-space.js'


@pytest.mark.skipif(shutil.which('node') is None, reason='node not available')
def test_measured_controls_fit_across_widths_and_counter_sizes():
    script = f"""
      global.window = {{}};
      const fs = require('node:fs');
      if (fs.existsSync({str(SPACE)!r})) require({str(SPACE)!r});
      const plan = window.pickerSpace?.plan;
      if (!plan) console.log('null');
      else {{
        const failures = [];
        for (const core of [119.078125, 133.953125, 240]) {{
          for (const jump of [0, 39, 260]) {{
            for (let width = 1; width <= 1500; width++) {{
              const natural = {{all: 46.453125, core, jump, jumpMin: 36,
                chip: 14, gap: 6, padding: 22}};
              const p = plan(width, natural);
              const used = 2*p.padding + 2*p.gap + p.scale *
                (natural.all + core + natural.chip + (jump ? p.jump + 6 : 0));
              if (!(p.scale > 0 && p.scale <= 1 && p.padding >= 0
                && p.jump >= 0 && p.jump <= jump && used <= width + 1e-8)) {{
                failures.push({{width, natural, p, used}});
              }}
            }}
          }}
        }}
        const wide = plan(1440, {{all: 46, core: 134, jump: 260,
          jumpMin: 36, chip: 14, gap: 6, padding: 22}});
        console.log(JSON.stringify({{failures, wide}}));
      }}
    """
    proc = run_node(
        ["node"], input=script, capture_output=True, text=True,
        check=False, timeout=30)
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result is not None, 'measured picker-space module is missing'
    assert result['failures'] == []
    assert result['wide'] == {'scale': 1, 'padding': 22, 'gap': 6, 'jump': 260}
