"""Cache responses expose estimate status without obsolete band flags."""
from __future__ import annotations

from datetime import datetime, timezone

from test_api import _app_with_fresh_data_fixture  # noqa: F401

from backend import db, pricing
from tests import dims_seed

__all__ = ["_app_with_fresh_data_fixture"]

_RATES = {"fresh": 0.25, "create_5m": 0.25, "create_1h": 0.25,
          "read": 0.015, "output": 0.75}


def _seed(model, host, nth=1):
    """Point the nth canonical record at (model, host) so the fold sees it."""
    with db.viz_conn() as conn:
        row = conn.execute(
            "SELECT f.file_key, r.line_num FROM records r "
            "JOIN files f ON f.file_id = r.file_id "
            "WHERE r.is_canonical IS TRUE "
            "ORDER BY f.file_key, r.line_num LIMIT 1 OFFSET %s",
            (nth - 1,),
        ).fetchone()
        assert row is not None, "mini mirror has no canonical record to seed"
        mid = dims_seed.dim_id(conn, "dim_model", model)
        provid = dims_seed.dim_id(conn, "dim_provider", host)
        conn.execute(
            "UPDATE records r SET ts = %s, model_id = %s, provider_id = %s, "
            "fresh_tokens = 1000, cache_creation_tokens = 0, "
            "cache_read_tokens = 0, output_tokens = 0, eph5_tokens = 0, "
            "eph1h_tokens = 0, web_search_requests = 0 "
            "FROM files f "
            "WHERE r.file_id = f.file_id AND f.file_key = %s "
            "  AND r.line_num = %s",
            (datetime(2026, 6, 1, tzinfo=timezone.utc), mid, provid, *row))
        # The reprice-written columns live on the records_price sidecar
        # (#959 R4); the fixture's ingest wrote both halves of the pair.
        conn.execute(
            "UPDATE records_price rp SET cost_usd = 0.00025, "
            "long_context = FALSE, long_context_input_mult = NULL, "
            "long_context_output_mult = NULL "
            "FROM files f "
            "WHERE rp.file_id = f.file_id AND f.file_key = %s "
            "  AND rp.line_num = %s",
            row)
        conn.commit()


def test_cache_has_no_band_flag(app_with_fresh_data, monkeypatch):
    pair = ("synthetic/priced-9", "PriceHost")
    monkeypatch.setitem(pricing.PROVIDER_RATES, pair, _RATES)
    monkeypatch.setitem(pricing.PROVIDER_DATED_RATES, pair, [])
    _seed(*pair)
    body = app_with_fresh_data.get("/api/cache?range=3650d").json()
    assert body["per_model"] and body["per_model_provider"]
    for entry in [*body["per_model"], *body["per_model_provider"], body["session_total"]]:
        assert "estimated_rate" in entry
        assert "banded_rate" not in entry
