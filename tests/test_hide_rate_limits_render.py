from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_effective_policy_resolves_the_legend_with_positive_hits() -> None:
    """The real helper removes the key and swatch while retaining EMA entries."""
    script = """
const assert = require('node:assert/strict');
global.window = {};
require('./src/feature-flags.js');
require('./src/burn-rate-legend.js');
const series = { sample: {color:'blue', label:'sample'} };
const resolve = () => window.burnRateLegend(series, window.featureOn('rate_limits'), 6.4, 600);
const on = resolve();
assert.equal(on.legendPlaced.length, 2);
assert.equal(on.legendPlaced[1].key, '__ratelimit');
assert.equal(on.legendPlaced[1].label, 'Rate limit hit');
window.FEATURES = {rate_limits:false};
const off = resolve();
assert.deepEqual(off.legendPlaced, on.legendPlaced.slice(0, 1));
assert.equal(off.legendPlaced.some(e => e.key === '__ratelimit'), false);
"""
    result = subprocess.run(["node", "-e", script], cwd=ROOT,
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_all_render_uses_receive_effective_policy() -> None:
    """Raw arrays are normalized once; marks/hover/description use the bound hits."""
    view = (ROOT / "src" / "dashboard-view.jsx").read_text()
    burn = (ROOT / "src" / "burn-rate-panel.jsx").read_text()
    shape = (ROOT / "src" / "dashboard-shape.js").read_text()
    assert "payloadHideRateLimits || !window.featureOn('rate_limits')" in view
    assert "const limitHits = hideRateLimits ? [] : rawLimitHits;" in view
    assert '{!hideRateLimits && <Stat label="rate-limit hits"' in view
    assert "hideRateLimits={hideRateLimits}" in view
    assert "hideRateLimits = hideRateLimits || !window.featureOn('rate_limits');" in burn
    assert "hideRateLimits ? [] : (rawLimitHits || [])" in burn
    assert burn.count("rawLimitHits") == 2
    assert "{limitHits.map(" in burn
    assert "for (const lh of limitHits)" in burn
    assert "hideRateLimits ? ''" in burn
    assert "series, !hideRateLimits, legendAdvUsed" in burn
    assert "window.featureOn('rate_limits') ? tx.meta : []" in shape
    assert "window.featureOn('rate_limits') && !b.hide_rate_limits" in shape
