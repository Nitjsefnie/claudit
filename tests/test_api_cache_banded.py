"""The API session cache total must flag a model priced through a banded
provider row (issue #666): the entry's five rates are the time-weighted
mean of a range the host oscillates inside, not a listed price
(SV-RATE-REFRESH, #640's ruling). Lives here, not in test_api.py, to keep
that module under the test-size ceiling.
"""
from __future__ import annotations

from datetime import datetime, timezone

from test_api import _app_with_fresh_data_fixture  # noqa: F401

from backend import db, pricing

__all__ = ["_app_with_fresh_data_fixture"]

_BANDED_RATES = {"fresh": 0.25, "create_5m": 0.25, "create_1h": 0.25,
                 "read": 0.015, "output": 0.75}
_BANDED_BAND = {"fresh": [0.05, 0.9], "create_5m": [0.05, 0.9],
                "create_1h": [0.05, 0.9], "read": [0.005, 0.09],
                "output": [0.25, 2.9]}


def _seed_cache_record(model: str, host: str) -> None:
    """Point one canonical record at (model, host) so /api/cache folds it."""
    with db.viz_conn() as conn:
        row = conn.execute(
            "SELECT file_key, line_num FROM records "
            "WHERE is_canonical IS TRUE ORDER BY file_key, line_num LIMIT 1"
        ).fetchone()
        assert row is not None, "mini mirror has no canonical record to seed"
        file_key, line_num = row
        conn.execute(
            "UPDATE records SET ts = %s, model = %s, provider = %s, "
            "fresh_tokens = 1000, cache_creation_tokens = 0, "
            "cache_read_tokens = 0, output_tokens = 0, eph5_tokens = 0, "
            "eph1h_tokens = 0, cost_usd = 0.00025, long_context = FALSE, "
            "long_context_input_mult = NULL, "
            "long_context_output_mult = NULL, web_search_requests = 0 "
            "WHERE file_key = %s AND line_num = %s",
            (datetime(2026, 6, 1, tzinfo=timezone.utc), model, host,
             file_key, line_num),
        )
        conn.commit()


def _monkeypatch_provider_pair(monkeypatch, pair, bands):
    monkeypatch.setattr(pricing, "PROVIDER_RATES",
                        {**pricing.PROVIDER_RATES, pair: _BANDED_RATES})
    monkeypatch.setattr(pricing, "PROVIDER_DATED_RATES",
                        {**pricing.PROVIDER_DATED_RATES, pair: []})
    monkeypatch.setattr(pricing, "PROVIDER_BANDS", bands)


def test_cache_session_total_banded_rate_true_when_a_model_priced_through_a_band(
    app_with_fresh_data, monkeypatch
):
    pair = ("synthetic/banded-9", "BandHost")
    _monkeypatch_provider_pair(
        monkeypatch, pair, {**pricing.PROVIDER_BANDS, pair: {0: _BANDED_BAND}})
    _seed_cache_record(*pair)

    body = app_with_fresh_data.get("/api/cache?range=3650d").json()
    row = next(r for r in body["per_model"] if r["model"] == pair[0])
    assert row["banded_rate"] is True
    assert body["session_total"]["banded_rate"] is True


def test_cache_session_total_banded_rate_false_for_an_unbanded_pair(
    app_with_fresh_data, monkeypatch
):
    pair = ("synthetic/unbanded-9", "PlainHost")
    _monkeypatch_provider_pair(monkeypatch, pair, dict(pricing.PROVIDER_BANDS))
    _seed_cache_record(*pair)

    body = app_with_fresh_data.get("/api/cache?range=3650d").json()
    row = next(r for r in body["per_model"] if r["model"] == pair[0])
    assert row["banded_rate"] is False
    assert body["session_total"]["banded_rate"] is False
