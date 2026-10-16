"""Explicit free cache-write history cannot be reconstructed from the token log."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from backend.pricing_document import expand_pricing_doc, serialize_pricing_doc
from tests.refresh_fixture_builders import _endpoint
from tests.test_provider_rate_backfill import (
    MODEL, RATE_A, RATE_B, RATE_C, BackfillFetches, _doc, _entry, _run, _series, backfill,
)


@pytest.mark.parametrize("field", ["create_5m", "create_1h"])
def test_backfill_keeps_a_sampled_row_with_explicit_free_writes(tmp_path, capsys, field):
    history = [_entry(None, {**RATE_A, field: 0}),
               _entry("2031-01-01T00:10:00Z", RATE_B)]
    states = [("2031-01-01T00:00:00Z", RATE_A),
              ("2031-01-01T00:20:00Z", RATE_B)]
    rc, out, err, path, _, original = _run(tmp_path, capsys, {"Wafer": history}, states)
    assert rc == 0, err
    assert path.read_text() == original
    assert "untouched Wafer" in out and "zero cache-write" in out


@pytest.mark.parametrize("field", ["create_5m", "create_1h"])
def test_hourly_samples_a_row_whose_free_writes_the_log_cannot_express(
        tmp_path, capsys, field):
    history = [_entry(None, {**RATE_A, field: 0}),
               _entry("2031-01-01T00:10:00Z", RATE_B)]
    path, constants = tmp_path / "pricing.json", tmp_path / "constants.py"
    path.write_text(serialize_pricing_doc(_doc({"Wafer": history})))
    constants.write_text('PRICING_VERSION = "31"\n')
    fetches = BackfillFetches([_endpoint("Wafer", RATE_C, tag="wafer/fp8")], {
        "data": {"series": [_series("Wafer", "wafer", [
            ("2031-01-01T00:00:00Z", RATE_A),
            ("2031-01-01T00:10:00Z", RATE_B),
            ("2031-01-01T00:20:00Z", RATE_C)])]}})
    rc = backfill.hourly.main(
        [], fetch=fetches.fetch_endpoints, fetch_models=fetches.fetch_models,
        fetch_log=fetches.fetch_log, now=datetime(2031, 1, 1, 0, 40, tzinfo=timezone.utc),
        pricing_path=path, constants_path=constants, vendor=None)
    out, err = capsys.readouterr()
    assert rc == 0, err
    saved = expand_pricing_doc(json.loads(path.read_text()))
    assert saved["providers"][MODEL]["Wafer"] == [
        *history, _entry("2031-01-01T00:40:00Z", RATE_C)]
    assert "sampled" in out and "zero cache-write" in out
