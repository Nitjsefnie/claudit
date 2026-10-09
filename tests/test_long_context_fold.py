"""The Codex long-context meter survives every re-derivation.

A Codex record above the 272k threshold is stored at 2x input side /
1.5x output, whatever plan served the rollout (issue #194). Any endpoint
that rebuilds per-component cost from the stored tokens must apply the
same multipliers or its breakdown stops summing to the stored cost_usd
(SV-DATED-RATES). records.long_context persists the decision; these
tests pin the flag end to end: parse → ingest → /api/cache (the fold)
and → /api/dashboard (the hourly grain the browser breakdown prices
from).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import api, constants, db, ingest, ingest_reprice, pricing
from backend import rate_fingerprint
from tests import scratch_db


# 300k prompt / 2k output, no rate_limits (the API shape): the record
# lands over the threshold with the meter armed — which is every plan's
# shape now (issue #194).
_USAGE = {
    "input_tokens": 300_000, "cached_input_tokens": 290_000,
    "cache_write_input_tokens": 0, "output_tokens": 2_000,
    "reasoning_output_tokens": 1_000, "total_tokens": 302_000,
}
# The rollout's request timestamp; sol's pre-Aug21 dated window prices it.
_BLOB_TS = datetime(2026, 6, 14, 12, 3, tzinfo=timezone.utc)
_BLOB = b"".join(
    json.dumps(line).encode() + b"\n"
    for line in [
        {"timestamp": "2026-06-14T12:00:01.000Z", "type": "session_meta",
         "payload": {"session_id": "00000000-0000-4000-8000-0000000000f9"}},
        {"timestamp": "2026-06-14T12:00:02.000Z", "type": "turn_context",
         "payload": {"model": "gpt-5.6-sol"}},
        {"timestamp": "2026-06-14T12:00:03.000Z", "type": "event_msg",
         "payload": {"type": "token_count",
                     "info": {"total_token_usage": _USAGE,
                              "last_token_usage": _USAGE}}},
    ]
)


@pytest.fixture(name="fresh_db")
def _fresh_db_fixture(monkeypatch):
    """Per-test schema reset on a separate DB (same shape as
    test_multi_bucket's fixture, kept local so this module stands alone)."""
    yield from scratch_db.scratch_viz_database(monkeypatch, "long_context")


@pytest.fixture(name="codex_app")
def _codex_app_fixture(fresh_db, tmp_path, monkeypatch):
    mirror = tmp_path / "codex" / "sessions" / "payg" / "s1"
    mirror.mkdir(parents=True)
    (mirror / "wire.jsonl").write_bytes(_BLOB)
    monkeypatch.setenv("R2_ENDPOINT", f"file://{tmp_path}/")
    monkeypatch.setenv("R2_BUCKET", "codex")

    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None, result["error"]
    assert result["inserted"] == 1

    a = FastAPI()
    a.include_router(api.router)
    return TestClient(a)


def test_ingest_persists_the_long_context_flag(codex_app):
    with db.viz_conn() as c:
        row = c.execute(
            "SELECT long_context, long_context_input_mult, "
            "long_context_output_mult FROM records").fetchone()
        rollup_row = c.execute(
            "SELECT long_context FROM usage_rollup").fetchone()
    assert row is not None and rollup_row is not None
    assert row[0] is True
    assert (row[1], row[2]) == pricing.long_context_factors("gpt-5.6-sol")
    assert rollup_row[0] is True, (
        "the rollup grain must carry the flag or the hourly breakdown "
        "cannot price it"
    )


def test_cache_cost_buckets_sum_to_the_stored_cost(codex_app):
    """/api/cache re-derives per-component cost from summed tokens; for a
    long-context record that needs the 2x/1.5x multipliers, or the buckets
    fall short of the stored cost_total they decompose. The record's
    300k input splits 10k fresh + 290k cache-read (Codex's cached_input
    is a subset), and the meter multiplies the WHOLE input side."""
    body = codex_app.get("/api/cache?range=3650d").json()
    m = body["per_model"][0]
    assert m["model"] == "gpt-5.6-sol"
    assert body["session_total"]["cost_total"] > 0
    # Three valued buckets + the total, each rounded to 4 decimals:
    # they can disagree by up to 6 * 5e-5 = 3e-4 whatever the rates are.
    assert m["cost_total"] == pytest.approx(
        sum(m["cost_buckets"].values()), abs=3e-4
    )
    # The record's ts (2026-06-14) sits inside sol's pre-Aug21 dated
    # window; the fold resolves the same rates for the epoch, so the
    # expected figures read them at that ts too.
    rates = pricing.rate_for("gpt-5.6-sol", _BLOB_TS)  # sv-test-data: allow (derived: expected priced from the same window rates at the record's own ts)
    assert m["cost_buckets"]["fresh"] == pytest.approx(
        10_000 * rates["fresh"] * pricing.LONG_CONTEXT_INPUT_MULT / 1_000_000,
        abs=1e-4,
    )
    assert m["cost_buckets"]["read"] == pytest.approx(
        290_000 * rates["read"] * pricing.LONG_CONTEXT_INPUT_MULT / 1_000_000,
        abs=1e-4,
    )
    assert m["cost_buckets"]["output"] == pytest.approx(
        2_000 * rates["output"] * pricing.LONG_CONTEXT_OUTPUT_MULT / 1_000_000,
        abs=1e-4,
    )


def test_cache_fold_places_search_only_cost_in_each_rate_epoch(
        codex_app, monkeypatch):
    model, host = "gpt-5.6-sol", "SearchHost"
    cutover = datetime(2026, 7, 1, tzinfo=timezone.utc)
    tokens = {"fresh": 0.0, "create_5m": 0.0, "create_1h": 0.0,
              "read": 0.0, "output": 0.0}
    before = {**tokens, "web_search": 0.0137}
    after = {**tokens, "web_search": 0.045}
    monkeypatch.setattr(pricing, "PROVIDER_RATES", {
        ("gpt-5-6-sol", host): after})
    monkeypatch.setattr(pricing, "PROVIDER_DATED_RATES", {
        ("gpt-5-6-sol", host): [(cutover, before)]})
    monkeypatch.setattr(pricing, "PROVIDER_STARTS", {})
    monkeypatch.setattr(pricing, "PROVIDER_SCHEDULES", {})
    monkeypatch.setattr(pricing, "RATE_EPOCHS", [cutover])

    with db.viz_conn() as c:
        file_key = c.execute(
            "SELECT file_key FROM records LIMIT 1").fetchone()[0]
        c.execute("DELETE FROM records WHERE file_key = %s", (file_key,))
        c.execute("UPDATE usage_rollup SET provider = %s WHERE model = %s",
                  (host, model))
        for line_num, ts, searches, rate in (
                (1, cutover - timedelta(seconds=1), 2, 0.0137),
                (2, cutover, 3, 0.045)):
            c.execute(
                "INSERT INTO records (file_key, line_num, ts, model, "
                "fresh_tokens, cache_creation_tokens, cache_read_tokens, "
                "output_tokens, eph5_tokens, eph1h_tokens, cost_usd, "
                "web_search_requests, provider, pricing_version) "
                "VALUES (%s, %s, %s, %s, 0, 0, 0, 0, 0, 0, %s, %s, %s, %s)",
                (file_key, line_num, ts, model, round(searches * rate, 6),
                 searches, host, constants.PRICING_VERSION),
            )
        c.commit()

    body = codex_app.get("/api/cache?range=3650d").json()
    model_row = next(row for row in body["per_model"] if row["model"] == model)
    assert model_row["cost_total"] == pytest.approx(0.1624)
    assert model_row["cost_buckets"]["web_search"] == pytest.approx(0.1624)
    assert sum(model_row["cost_buckets"].values()) == pytest.approx(0.1624)


def test_mixed_stored_factor_pairs_decompose_stored_total(
        codex_app, monkeypatch):
    """A committed partial reprice leaves old and new meter pairs together.

    The two synthetic records share model, provider, rate epoch, and flag.
    Repricing one row from (5, 5) to (6, 5), then aborting before the next
    batch, must leave enough per-record provenance for the real /api/cache
    SQL aggregation to split their stored total of 62 without drift.
    """
    model = "synthetic-meter-reprice-878"
    rates = {"fresh": 2.0, "create_5m": 2.5, "create_1h": 4.0,
             "read": 0.2, "output": 4.0}
    old_meter = {"threshold": 100_000, "input_mult": 5.0,
                 "output_mult": 5.0}
    monkeypatch.setattr(pricing, "MODEL_RATES", {model: rates})
    monkeypatch.setattr(pricing, "DATED_RATES", {})
    monkeypatch.setattr(pricing, "LONG_CONTEXT_MODELS", frozenset({model}))
    monkeypatch.setattr(pricing, "LONG_CONTEXT_METERS", {model: old_meter})
    rate_fingerprint.clear_fingerprint_cache()
    old_fingerprint = rate_fingerprint.pair_fingerprint(model, None)

    with db.viz_conn() as c:
        file_key, line_num = c.execute(
            "SELECT file_key, line_num FROM records ORDER BY file_key, line_num"
        ).fetchone()
        c.execute(
            "UPDATE records SET model = %s, ts = %s, fresh_tokens = %s, "
            "cache_creation_tokens = 0, cache_read_tokens = 0, "
            "output_tokens = %s, eph5_tokens = 0, eph1h_tokens = 0, "
            "cost_usd = %s, long_context = TRUE, "
            "provider = NULL, pricing_version = '0', rate_fingerprint = %s, "
            "long_context_input_mult = 5.0, long_context_output_mult = 5.0 "
            "WHERE file_key = %s AND line_num = %s",
            (model, _BLOB_TS, 1_000_000, 1_000_000, 30.0,
             old_fingerprint, file_key, line_num),
        )
        c.execute(
            "INSERT INTO records (file_key, line_num, ts, model, fresh_tokens, "
            "output_tokens, cost_usd, long_context, provider, pricing_version, "
            "rate_fingerprint, long_context_input_mult, long_context_output_mult) "
            "VALUES (%s, %s, %s, %s, 1000000, 1000000, 30.0, TRUE, NULL, '0', "
            "%s, 5.0, 5.0)",
            (file_key, line_num + 1, _BLOB_TS, model, old_fingerprint),
        )
        c.commit()

    monkeypatch.setattr(pricing, "LONG_CONTEXT_METERS", {
        model: {"threshold": 100_000, "input_mult": 6.0,
               "output_mult": 5.0}})
    rate_fingerprint.clear_fingerprint_cache()
    current_fingerprint = rate_fingerprint.pair_fingerprint(model, None)
    assert current_fingerprint != old_fingerprint, (
        "a meter-factor change must prevent the fingerprint clean-restamp")
    future_version = str(int(constants.PRICING_VERSION) + 100)
    monkeypatch.setattr(constants, "PRICING_VERSION", future_version)
    monkeypatch.setattr(ingest_reprice, "REPRICE_BATCH", 1)
    stop_checks = iter((False, True))
    with pytest.raises(ingest_reprice.IngestAborted):
        ingest_reprice.reprice_stale(should_stop=lambda: next(stop_checks))

    with db.viz_conn() as c:
        rows = c.execute(
            "SELECT cost_usd, long_context_input_mult, "
            "long_context_output_mult, rate_fingerprint, pricing_version "
            "FROM records "
            "ORDER BY file_key, line_num").fetchall()

    body = codex_app.get("/api/cache?range=3650d").json()
    model_row = next(row for row in body["per_model"] if row["model"] == model)
    assert model_row["cost_total"] == 62.0
    assert sum(model_row["cost_buckets"].values()) == pytest.approx(62.0)
    assert [(float(cost), in_mult, out_mult) for cost, in_mult, out_mult,
            _fingerprint, _version in rows] == [(32.0, 6.0, 5.0), (30.0, 5.0, 5.0)]
    assert rows[0][3:] == (current_fingerprint, constants.PRICING_VERSION)
    assert rows[1][3:] == (old_fingerprint, "0")


def test_dashboard_hourly_rows_carry_the_flag(codex_app):
    """The hourly grain carries long_context so the browser breakdown
    (computeTokenBreakdown) can price each row by its own meter."""
    body = codex_app.get("/api/dashboard?range=3650d").json()
    assert body["hourly"], "the range covers the record"
    assert all(e["long_context"] is True for e in body["hourly"])
    assert body["hourly"][0]["input_tokens"] == 10_000
    assert body["hourly"][0]["cache_read_tokens"] == 290_000
