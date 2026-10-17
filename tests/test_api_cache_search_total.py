"""The API session cache total must include nonzero web-search charges."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from test_api import _app_with_fresh_data_fixture  # noqa: F401
from backend import db, pricing
from tests import dims_seed

__all__ = ["_app_with_fresh_data_fixture"]

_SEARCH_MODEL = "synthetic/session-search-9"
_SEARCH_HOST = "SearchHost"
_SEARCH_PAIR = (_SEARCH_MODEL, _SEARCH_HOST)
_SEARCH_REQUESTS = 3
_SEARCH_RATE = 0.0137
_FRESH_TOKENS = 1000
_FRESH_RATE = 2.0
_EXPECTED_SEARCH_COST = 0.0411
_EXPECTED_TOTAL_COST = 0.0431


def test_cache_session_search_total_sums_real_search_requests(
        app_with_fresh_data, monkeypatch):
    rates = {
        "fresh": _FRESH_RATE,
        "create_5m": 2.5,
        "create_1h": 4.0,
        "read": 0.2,
        "output": 10.0,
        "web_search": _SEARCH_RATE,
    }
    monkeypatch.setattr(pricing, "PROVIDER_RATES", {
        **pricing.PROVIDER_RATES, _SEARCH_PAIR: rates,
    })
    monkeypatch.setattr(pricing, "PROVIDER_DATED_RATES", {
        **pricing.PROVIDER_DATED_RATES, _SEARCH_PAIR: [],
    })

    # 1000 * $2/M = $0.002; three searches at $0.0137 = $0.0411.
    with db.viz_conn() as conn:
        row = conn.execute(
            "SELECT f.file_key, r.line_num FROM records r "
            "JOIN files f ON f.file_id = r.file_id "
            "WHERE r.is_canonical IS TRUE "
            "ORDER BY f.file_key, r.line_num LIMIT 1"
        ).fetchone()
        assert row is not None, "mini mirror has no canonical record to seed"
        file_key, line_num = row
        mid = dims_seed.dim_id(conn, "dim_model", _SEARCH_MODEL)
        provid = dims_seed.dim_id(conn, "dim_provider", _SEARCH_HOST)
        conn.execute(
            "UPDATE records r SET ts = %s, model_id = %s, provider_id = %s, "
            "fresh_tokens = %s, cache_creation_tokens = 0, "
            "cache_read_tokens = 0, output_tokens = 0, eph5_tokens = 0, "
            "eph1h_tokens = 0, web_search_requests = %s "
            "FROM files f "
            "WHERE r.file_id = f.file_id AND f.file_key = %s "
            "  AND r.line_num = %s",
            (datetime(2026, 6, 1, tzinfo=timezone.utc), mid, provid,
             _FRESH_TOKENS, _SEARCH_REQUESTS, file_key, line_num),
        )
        # The stored total is a reprice-written column, so it lands on
        # the records_price sidecar the pair already carries.
        conn.execute(
            "UPDATE records_price rp SET cost_usd = %s, long_context = FALSE, "
            "long_context_input_mult = NULL, long_context_output_mult = NULL "
            "FROM files f "
            "WHERE rp.file_id = f.file_id AND f.file_key = %s "
            "  AND rp.line_num = %s",
            (_EXPECTED_TOTAL_COST, file_key, line_num),
        )
        conn.commit()

    body = app_with_fresh_data.get("/api/cache?range=3650d").json()
    model_row = next(row for row in body["per_model"]
                     if row["model"] == _SEARCH_MODEL)
    # cost_buckets is stripped from per_model; the session
    # total still carries the decomposition, and only this model bills a
    # search rate, so its web_search bucket is the whole total.
    assert "cost_buckets" not in model_row
    session_search = body["session_total"]["cost_buckets"]["web_search"]
    assert session_search == pytest.approx(_EXPECTED_SEARCH_COST)
