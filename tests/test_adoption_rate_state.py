"""Model adoption moves price provenance atomically and respects rollbacks."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from test_canonical_model_attribution import (  # pylint: disable=unused-import
    _fresh_db_fixture, _seed_two_identities,
)
from backend import constants, db, dims, ingest_rollup_state, ingest_vacuum, pricing
from backend.ingest_scope import Scope
from tests import dims_seed


def _seed(pricing_version):
    _seed_two_identities()
    with db.viz_conn() as c:
        provider = dims_seed.dim_id(c, "dim_provider", "adoption-host")
        version = dims_seed.dim_id(c, "dim_pricing_version", pricing_version)
        c.execute(
            "UPDATE records SET fresh_tokens = 300000, output_tokens = 1000, "
            "web_search_requests = 2, provider_id = %s, "
            "ts = '2026-01-01T00:00Z' WHERE is_replay", (provider,))
        c.execute(
            "INSERT INTO records_price (file_id, line_num, cost_usd, "
            "long_context, long_context_input_mult, long_context_output_mult, "
            "pricing_version_id) SELECT file_id, line_num, 77, TRUE, 1, 1, %s "
            "FROM records", (version,))
        c.commit()


def _snapshot():
    with db.viz_conn() as c:
        return c.execute(
            "SELECT f.file_key, dm.value, r.provider_id, rp.* FROM records r "
            "JOIN files f USING (file_id) JOIN dim_model dm ON dm.id = r.model_id "
            "JOIN records_price rp USING (file_id, line_num) "
            "ORDER BY f.file_key, r.line_num").fetchall()


@pytest.mark.parametrize("spelling", ["plain", "signed-whitespace"])
def test_newer_price_defers_model_adoption(fresh_db, spelling):
    newer = str(int(constants.PRICING_VERSION) + 1)
    if spelling == "signed-whitespace":
        newer = f" +{newer} "
    _seed(newer)
    before = _snapshot()
    assert ingest_rollup_state.adopt_original_models() == 0
    assert _snapshot() == before, "keep the newer model and its entire price together"
    # The forward binary can finish the deferred adoption.
    with db.viz_conn() as c:
        current = dims.dim_id(c, "dim_pricing_version", constants.PRICING_VERSION)
        c.execute("UPDATE records_price SET pricing_version_id = %s", (current,))
        c.commit()
    assert ingest_rollup_state.adopt_original_models() == 2


@pytest.mark.parametrize("metered", [False, True])
def test_adoption_prices_own_provider_timestamp_tokens_and_meter(
        fresh_db, monkeypatch, metered):
    _seed(constants.PRICING_VERSION)
    winner = "gpt-5.6-sol"
    key = "gpt-5-6-sol"
    monkeypatch.setattr(pricing, "LONG_CONTEXT_MODELS", frozenset({key}))
    monkeypatch.setattr(pricing, "LONG_CONTEXT_METERS", {
        key: {"threshold": 1 if metered else 400000,
              "input_mult": 3.0, "output_mult": 2.0}})
    monkeypatch.setattr(pricing, "VENDOR_BARE", {})
    pair = (key, "adoption-host")
    rates = {"fresh": 2.0, "output": 9.0, "read": 0.1,
             "create_5m": 3.0, "create_1h": 4.0, "web_search": 0.25}
    monkeypatch.setattr(pricing, "PROVIDER_RATES", {pair: {**rates, "fresh": 99.0}})
    monkeypatch.setattr(pricing, "PROVIDER_STARTS", {})
    monkeypatch.setattr(pricing, "PROVIDER_SCHEDULES", {})
    monkeypatch.setattr(pricing, "PROVIDER_DATED_RATES", {
        pair: [(datetime(2027, 1, 1, tzinfo=timezone.utc), rates)]})
    ingest_vacuum.begin_run()
    scope = Scope(False, "test", 2000)
    scope.affected_uuids = {"u1"}
    before = _snapshot()
    assert ingest_rollup_state.adopt_original_models(scope) == 1
    after = _snapshot()
    assert after[1:] == before[1:], "only the scoped replay changes"
    fork = after[0]
    assert fork[0:3] == ("fork1", winner, before[0][2])
    # 300k fresh + 1k output at the dated provider price, plus two searches.
    assert fork[5:9] == (Decimal("2.318000" if metered else "1.109000"),
                         metered, 3.0 if metered else None,
                         2.0 if metered else None)
    assert ingest_vacuum.take_phase_rows() == {"reprice": 1}
    assert not scope.dirty_hours and not scope.full


def test_failed_adoption_pricing_rolls_back_model_and_file_metadata(fresh_db, monkeypatch):
    _seed(constants.PRICING_VERSION)
    before = _snapshot()
    with db.viz_conn() as c:
        files_before = c.execute("SELECT file_id, models FROM files ORDER BY file_id").fetchall()

    def fail_pricing(*args, **kwargs):
        raise RuntimeError("injected price failure")

    monkeypatch.setattr(pricing, "compute_cost", fail_pricing)
    ingest_vacuum.begin_run()
    with pytest.raises(RuntimeError, match="injected price failure"):
        ingest_rollup_state.adopt_original_models()
    assert _snapshot() == before
    with db.viz_conn() as c:
        assert c.execute("SELECT file_id, models FROM files ORDER BY file_id").fetchall() == files_before
    assert not ingest_vacuum.take_phase_rows()
