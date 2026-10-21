"""Replay a captured OpenRouter catalog through the vendor pass."""
from __future__ import annotations

import json

import pytest

from tests.vendor_rate_refresh_helpers import ROOT, _catalog, _doc, _run


@pytest.mark.parametrize("model_id,key", [
    ("minimax/minimax-m2.1", "minimax-m2-1"),
    ("minimax/minimax-m2.5", "minimax-m2-5"),
    ("minimax/minimax-m2.7", "minimax-m2-7"),
])
def test_vendor_snapshot_selects_first_party_quantization(model_id, key):
    snapshot = json.loads((ROOT / "fixtures" / "vendor_catalog" /
                           "minimax-endpoints.json").read_text(encoding="utf-8"))
    doc, out = _run(_doc(prefixes=["minimax"]), _catalog(model_id),
                    {model_id: snapshot[model_id]})
    assert not out.refusals, "\n".join(out.refusals)
    assert doc["openrouter"]["models"][key] == {
        "id": model_id, "vendor_host": "Minimax", "context_length": 204800}
    assert len(out.notices) == 1
    assert "rule-resolved (unique quantization)" in out.notices[0]
    assert "took the fp8 endpoint 'minimax/fp8' over service tiers" in out.notices[0]
