"""window.modelColors answers for EVERY model, not only the hardcoded ones.

The table in src/dashboard-charts.jsx names the models that had a hand-
picked colour; anything else (a new lane, a new generation) fell to the
'#888' every lookup site carries as its fallback, so two unknown models
were the same grey on every panel. The table is now a Proxy over the
hardcoded map: a listed key returns its hand-picked colour, any other
string returns a colour DERIVED from the key — deterministic, so a model
keeps its colour across panels and reloads, and spread by hue so two
unknown models are distinguishable.

Driven through node on the block extracted verbatim from the .jsx (same
approach as test_vbar_label_geometry.py); rendering is not exercised.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

CHARTS_JSX = Path(__file__).resolve().parent.parent / "src" / "dashboard-charts.jsx"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available"
)


def _node_probe(keys):
    script = f"""
      global.window = {{}};
      const fs = require('fs');
      const src = fs.readFileSync({str(CHARTS_JSX)!r}, 'utf8');
      const start = src.indexOf('// --- Model colours');
      const end = src.indexOf('// --- End model colours');
      if (start < 0 || end < 0 || end <= start) {{
        throw new Error('model colour block extraction failed');
      }}
      eval(src.slice(start, end) + '\\nwindow.__colors = MODEL_COLORS;');
      const c = window.__colors;
      const out = {{}};
      for (const k of {json.dumps(keys)}) out[k] = c[k];
      out.__symbol = c[Symbol.iterator] === undefined;
      out.__hardcoded = Object.keys(c);
      console.log(JSON.stringify(out));
    """
    res = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    return json.loads(res.stdout)


OKLCH = re.compile(r"^oklch\(0\.\d+ 0\.\d+ \d+(\.\d+)?\)$")


def test_hardcoded_models_keep_their_hand_picked_colour():
    out = _node_probe(["opus-5", "haiku-4-5", "<synthetic>"])
    assert out["opus-5"] == "oklch(0.72 0.18 350)"
    assert out["haiku-4-5"] == "oklch(0.78 0.14 175)"
    assert out["<synthetic>"] == "oklch(0.65 0.02 260)"


def test_unknown_models_get_a_derived_colour_not_grey():
    out = _node_probe(["bonsai-2-27b", "gpt-5-6-sol", "glm-5-3-flash", "unknown"])
    for k in ("bonsai-2-27b", "gpt-5-6-sol", "glm-5-3-flash", "unknown"):
        assert OKLCH.match(out[k]), (k, out[k])
    assert len({out[k] for k in ("bonsai-2-27b", "gpt-5-6-sol", "glm-5-3-flash")}) == 3


def test_derived_colour_is_stable_across_lookups():
    a = _node_probe(["bonsai-2-27b"])["bonsai-2-27b"]
    b = _node_probe(["bonsai-2-27b"])["bonsai-2-27b"]
    assert a == b


def test_derived_hue_stays_clear_of_the_hardcoded_hues():
    """Generated hues avoid a band around every hand-picked hue, so an
    unknown model cannot impersonate Opus or Sonnet at a glance."""
    out = _node_probe(["bonsai-2-27b", "gpt-5-6-sol", "kimi-k2", "mimo-v2-6-pro"])
    fixed = [350, 5, 25, 55, 90, 275, 245, 305, 175, 318, 330]
    for k, v in out.items():
        if k.startswith("__"):
            continue
        m = OKLCH.match(v)
        assert m is not None, (k, v)
        hue = float(m.group(0).split()[-1].rstrip(")"))
        gap = min(min(abs(hue - f), 360 - abs(hue - f)) for f in fixed)
        assert gap >= 8, (k, v, gap)


def test_non_string_lookups_do_not_throw():
    out = _node_probe([])
    assert out["__symbol"] is True
    assert "opus-5" in out["__hardcoded"]
