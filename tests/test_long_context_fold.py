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
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import api, db, ingest, pricing
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
        row = c.execute("SELECT long_context FROM records").fetchone()
        rollup_row = c.execute(
            "SELECT long_context FROM usage_rollup").fetchone()
    assert row is not None and rollup_row is not None
    assert row[0] is True
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
    assert m["cost_total"] == pytest.approx(
        sum(m["cost_buckets"].values()), abs=1e-4
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


def test_dashboard_hourly_rows_carry_the_flag(codex_app):
    """The hourly grain carries long_context so the browser breakdown
    (computeTokenBreakdown) can price each row by its own meter."""
    body = codex_app.get("/api/dashboard?range=3650d").json()
    assert body["hourly"], "the range covers the record"
    assert all(e["long_context"] is True for e in body["hourly"])
    assert body["hourly"][0]["input_tokens"] == 10_000
    assert body["hourly"][0]["cache_read_tokens"] == 290_000
