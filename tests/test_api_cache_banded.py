"""Issue #666: /api/cache flags a model priced through a banded provider row
on its per_model entry and the session total."""
from __future__ import annotations

from datetime import datetime, timezone

from test_api import _app_with_fresh_data_fixture  # noqa: F401

from backend import db, pricing
from tests import dims_seed

__all__ = ["_app_with_fresh_data_fixture"]

_RATES = {"fresh": 0.25, "create_5m": 0.25, "create_1h": 0.25,
          "read": 0.015, "output": 0.75}
_BAND = {"fresh": [0.05, 0.9], "create_5m": [0.05, 0.9],
         "create_1h": [0.05, 0.9], "read": [0.005, 0.09],
         "output": [0.25, 2.9]}


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


def test_cache_flags_a_banded_provider_row(app_with_fresh_data, monkeypatch):
    """A banded pair's per_model entry and the session total carry
    banded_rate True; an unbanded pair's entry is False."""
    banded = ("synthetic/banded-9", "BandHost")
    plain = ("synthetic/plain-9", "PlainHost")
    monkeypatch.setattr(pricing, "PROVIDER_RATES",
                        {**pricing.PROVIDER_RATES, banded: _RATES,
                         plain: _RATES})
    monkeypatch.setattr(pricing, "PROVIDER_DATED_RATES",
                        {**pricing.PROVIDER_DATED_RATES, banded: [], plain: []})
    monkeypatch.setattr(pricing, "PROVIDER_BANDS",
                        {**pricing.PROVIDER_BANDS, banded: {0: _BAND}})
    _seed(*banded)
    _seed(*plain, nth=2)
    body = app_with_fresh_data.get("/api/cache?range=3650d").json()
    flags = {r["model"]: r["banded_rate"] for r in body["per_model"]}
    assert flags[banded[0]] is True and flags[plain[0]] is False
    assert body["session_total"]["banded_rate"] is True
