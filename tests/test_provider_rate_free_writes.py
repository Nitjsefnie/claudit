"""Listed-pricing normalization governs even stored explicit free-write rows."""
from __future__ import annotations

import json

import pytest

from backend.pricing_document import expand_pricing_doc
from tests.test_provider_rate_backfill import (
    MODEL, RATE_A, RATE_B, _entry, _run,
)


@pytest.mark.parametrize("field", ["create_5m", "create_1h"])
def test_backfill_replaces_explicit_free_writes_with_normalized_log_states(tmp_path, capsys, field):
    history = [_entry(None, {**RATE_A, field: 0}),
               _entry("2031-01-01T00:10:00Z", RATE_B)]
    states = [("2031-01-01T00:00:00Z", RATE_A),
              ("2031-01-01T00:20:00Z", RATE_B)]
    rc, out, err, path, _, _ = _run(tmp_path, capsys, {"Wafer": history}, states)
    assert rc == 0, err
    saved = expand_pricing_doc(json.loads(path.read_text()))
    assert saved["providers"][MODEL]["Wafer"] == [_entry(None, RATE_A)]
    assert "rewritten Wafer" in out
