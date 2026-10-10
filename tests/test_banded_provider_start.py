"""Issue #666: the band flag must not apply to a record priced by the
models table. A record whose `ts` precedes its provider row's start is
priced by the models table (mirroring resolve/_provider_key), so the row's
band does not apply — the fold helper re-derives the row from the
resolution and must carry the same start guard. Lives in its own module to
keep test_provider_pricing.py under the test-size ceiling.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backend import pricing
from backend.api_common import _row_is_banded

UTC = timezone.utc

_MODEL = "acme/started-9"
_HOST = "HostCo"
_START = datetime(2031, 1, 1, tzinfo=UTC)
_MODEL_RATES = {"fresh": 9.0, "create_5m": 9.0, "create_1h": 9.0,
                "read": 0.9, "output": 90.0}
_PROVIDER_RATES = {"fresh": 0.25, "create_5m": 0.25, "create_1h": 0.25,
                   "read": 0.015, "output": 0.75}
_BAND = {"fresh": [0.05, 0.9], "create_5m": [0.05, 0.9],
         "create_1h": [0.05, 0.9], "read": [0.005, 0.09],
         "output": [0.25, 2.9]}


def _banded(model, ts, provider):
    """The band flag the fold computes for a record (issue #666)."""
    return _row_is_banded(pricing.resolve(model, ts, provider), ts, provider)


def _install(monkeypatch):
    """A banded provider row that BEGINS at a time, whose model also names
    a models-table key — the overlap the start guard exists for."""
    pair = (_MODEL, _HOST)
    monkeypatch.setattr(pricing, "MODEL_RATES",
                        {**pricing.MODEL_RATES, _MODEL: _MODEL_RATES})
    monkeypatch.setattr(pricing, "PROVIDER_RATES",
                        {**pricing.PROVIDER_RATES, pair: _PROVIDER_RATES})
    monkeypatch.setattr(pricing, "PROVIDER_DATED_RATES",
                        {**pricing.PROVIDER_DATED_RATES, pair: []})
    monkeypatch.setattr(pricing, "PROVIDER_STARTS",
                        {**pricing.PROVIDER_STARTS, pair: _START})
    monkeypatch.setattr(pricing, "PROVIDER_BANDS",
                        {**pricing.PROVIDER_BANDS, pair: {0: _BAND}})
    monkeypatch.setattr(pricing, "_MATCH_KEY_CACHE", {})


def test_a_record_before_its_provider_rows_start_has_no_band(monkeypatch):
    _install(monkeypatch)
    before = _START - timedelta(seconds=1)
    res = pricing.resolve(_MODEL, before, _HOST)
    # Priced by the models table (the provider row is not active yet).
    assert res.kind == "exact" and res.key == _MODEL
    assert res.rates["fresh"] == _MODEL_RATES["fresh"]
    assert _banded(_MODEL, before, _HOST) is False


def test_a_record_at_its_provider_rows_start_takes_the_band(monkeypatch):
    _install(monkeypatch)
    res = pricing.resolve(_MODEL, _START, _HOST)
    assert res.rates["fresh"] == _PROVIDER_RATES["fresh"]
    assert _banded(_MODEL, _START, _HOST) is True
