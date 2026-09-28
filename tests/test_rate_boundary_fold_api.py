"""Per-pair cache folding and the global fallback for missing rollup pairs."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import api, api_cache, db, pricing
from backend.cache import response_cache
from tests import scratch_db

UTC = timezone.utc


def _rates(fresh: float) -> dict[str, float]:
    return {
        "fresh": fresh,
        "create_5m": fresh,
        "create_1h": fresh,
        "read": fresh / 10,
        "output": fresh * 5,
    }


@pytest.fixture(name="fresh_db")
def _fresh_db_fixture(monkeypatch):
    yield from scratch_db.scratch_viz_database(monkeypatch, "rate_boundary_fold")


@pytest.fixture(name="api_client")
def _api_client_fixture(fresh_db):
    response_cache.clear()
    application = FastAPI()
    application.include_router(api.router)
    with TestClient(application) as client:
        yield client
    response_cache.clear()


def _seed_records(records, rollup_pairs):
    """Write synthetic records and a deliberately independent rollup set."""
    assert records
    assert rollup_pairs
    project_id = "rate-boundary-project"
    file_key = "synthetic/rate-boundary.jsonl"
    now = datetime(2026, 1, 1, tzinfo=UTC)
    with db.viz_conn() as conn:
        conn.execute(
            "INSERT INTO projects (project_id, display_name, first_seen_at, "
            "last_seen_at) VALUES (%s, %s, %s, %s)",
            (project_id, project_id, now, now),
        )
        conn.execute(
            "INSERT INTO files (file_key, project_id, session_id, is_main, "
            "r2_etag, r2_size_bytes, r2_last_modified, parsed_at, "
            "parser_version) VALUES (%s, %s, %s, TRUE, %s, 0, %s, %s, %s)",
            (file_key, project_id, "synthetic-session", "synthetic-etag",
             now, now, "synthetic"),
        )
        for line_num, (model, provider, ts, fresh) in enumerate(records, 1):
            cost = pricing.compute_cost(
                model, fresh=fresh, output=0, eph5=0, eph1h=0,
                unsplit_create=0, read=0, ts=ts, provider=provider,
            )
            conn.execute(
                "INSERT INTO records (file_key, line_num, ts, model, "
                "fresh_tokens, cost_usd, is_canonical, long_context, provider) "
                "VALUES (%s, %s, %s, %s, %s, %s, TRUE, FALSE, %s)",
                (file_key, line_num, ts, model, fresh, cost, provider),
            )
        for index, (model, provider, ts) in enumerate(rollup_pairs):
            hour = ts.replace(minute=0, second=0, microsecond=0)
            conn.execute(
                "INSERT INTO usage_rollup (session_id, project_id, hour, "
                "model, first_ts, last_ts, provider) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (f"synthetic-rollup-{index}", project_id, hour, model,
                 ts, ts, provider),
            )


def _capture_per_model_rows(monkeypatch):
    captured = {}
    original = api_cache._cache_queries

    def capture(conn, phases, canon_source, canon_args):
        result = original(conn, phases, canon_source, canon_args)
        captured["rows"] = result[0]
        captured["pair_bounds"] = result[4] if len(result) > 4 else {}
        return result

    monkeypatch.setattr(api_cache, "_cache_queries", capture)
    return captured


def test_cache_fold_ignores_other_pairs_boundaries_and_keeps_own_cutovers(
        api_client, monkeypatch):
    stable_model, stable_host = "acme/fold-stable-302", "StableHost"
    noisy_model, noisy_host = "acme/fold-noisy-302", "NoiseHost"
    changing_model, changing_host = "acme/fold-changing-302", "ChangingHost"
    noisy_bounds = [datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=i)
                    for i in range(50)]
    changing_cut = datetime(2026, 3, 1, tzinfo=UTC)
    noise_rates = [_rates(100 + i) for i in range(len(noisy_bounds) + 1)]
    changing_before, changing_after = _rates(2), _rates(7)
    stable_key = (stable_model, stable_host)
    noisy_key = (noisy_model, noisy_host)
    changing_key = (changing_model, changing_host)

    monkeypatch.setattr(pricing, "MODEL_RATES", {})
    monkeypatch.setattr(pricing, "DATED_RATES", {})
    monkeypatch.setattr(pricing, "PROVIDER_RATES", {
        stable_key: _rates(3), noisy_key: noise_rates[-1],
        changing_key: changing_after,
    })
    monkeypatch.setattr(pricing, "PROVIDER_DATED_RATES", {
        noisy_key: list(zip(noisy_bounds, noise_rates[:-1], strict=True)),
        changing_key: [(changing_cut, changing_before)],
    })
    monkeypatch.setattr(pricing, "PROVIDER_STARTS", {})
    monkeypatch.setattr(pricing, "PROVIDER_SCHEDULES", {})
    monkeypatch.setattr(pricing, "_TIER_FALLBACKS", ())
    monkeypatch.setattr(pricing, "DEFAULT_RATES", _rates(89))
    monkeypatch.setattr(pricing, "RATE_EPOCHS", sorted(noisy_bounds + [changing_cut]))

    stable_records = [
        (stable_model, stable_host, noisy_bounds[0] - timedelta(days=1),
         1_000_000),
        (stable_model, stable_host, noisy_bounds[-1] + timedelta(days=1),
         1_000_000),
    ]
    changing_records = [
        (changing_model, changing_host, changing_cut - timedelta(days=1),
         1_000_000),
        (changing_model, changing_host, changing_cut, 1_000_000),
    ]
    rollup_pairs = [
        (stable_model, stable_host, stable_records[0][2]),
        (noisy_model, noisy_host, noisy_bounds[0]),
        (changing_model, changing_host, changing_cut),
    ]
    _seed_records(stable_records + changing_records, rollup_pairs)
    captured = _capture_per_model_rows(monkeypatch)

    response = api_client.get("/api/cache?range=all")
    assert response.status_code == 200
    body = response.json()
    assert captured["rows"], "seeded canonical records must make fold rows"
    assert len(noisy_bounds) == 50
    assert captured["pair_bounds"][stable_key] == []
    assert len(captured["pair_bounds"][noisy_key]) == 50

    stable_groups = [row for row in captured["rows"]
                     if row[0] == stable_model and row[1] == stable_host]
    changing_groups = [row for row in captured["rows"]
                       if row[0] == changing_model and row[1] == changing_host]
    assert len(stable_groups) == 1
    assert stable_groups[0][2] == 0
    assert stable_groups[0][4] == 2
    assert {row[2] for row in changing_groups} == {0, 1}
    assert len(changing_groups) == 2

    per_model = {entry["model"]: entry for entry in body["per_model"]}
    assert set(per_model) == {stable_model, changing_model}
    assert per_model[stable_model]["cost_total"] == pytest.approx(6.0)
    assert per_model[stable_model]["cost_buckets"]["fresh"] == pytest.approx(6.0)
    assert per_model[changing_model]["cost_total"] == pytest.approx(9.0)
    assert per_model[changing_model]["cost_buckets"]["fresh"] == pytest.approx(9.0)
    for entry in per_model.values():
        assert abs(sum(entry["cost_buckets"].values())
                   - entry["cost_total"]) <= 3e-4


def test_cache_missing_rollup_pair_uses_global_epoch_fallback(
        api_client, monkeypatch):
    present_model, present_host = "acme/fold-present-302", "PresentHost"
    missing_model, missing_host = "acme/fold-missing-302", "MissingHost"
    unrelated_cut = datetime(2026, 3, 1, tzinfo=UTC)
    missing_cut = datetime(2026, 4, 1, tzinfo=UTC)
    present_key = (present_model, present_host)
    missing_key = (missing_model, missing_host)
    before, after = _rates(5), _rates(7)

    monkeypatch.setattr(pricing, "MODEL_RATES", {})
    monkeypatch.setattr(pricing, "DATED_RATES", {})
    monkeypatch.setattr(pricing, "PROVIDER_RATES", {
        present_key: _rates(11), missing_key: after,
    })
    monkeypatch.setattr(pricing, "PROVIDER_DATED_RATES", {
        present_key: [(unrelated_cut, _rates(9))],
        missing_key: [(missing_cut, before)],
    })
    monkeypatch.setattr(pricing, "PROVIDER_STARTS", {})
    monkeypatch.setattr(pricing, "PROVIDER_SCHEDULES", {})
    monkeypatch.setattr(pricing, "_TIER_FALLBACKS", ())
    monkeypatch.setattr(pricing, "DEFAULT_RATES", _rates(89))
    monkeypatch.setattr(pricing, "RATE_EPOCHS", [unrelated_cut, missing_cut])

    missing_records = [
        (missing_model, missing_host, missing_cut - timedelta(days=1),
         1_000_000),
        (missing_model, missing_host, missing_cut, 1_000_000),
    ]
    # The missing pair's records are deliberately absent from this rollup
    # list, as after ingest writes records but before its rollup rebuild.
    _seed_records(missing_records, [
        (present_model, present_host, unrelated_cut),
    ])
    captured = _capture_per_model_rows(monkeypatch)

    response = api_client.get("/api/cache?range=all")
    assert response.status_code == 200
    body = response.json()
    assert captured["rows"], "the unrolled records must still be selected"
    assert present_key in captured["pair_bounds"]
    assert missing_key not in captured["pair_bounds"]
    missing_groups = [row for row in captured["rows"] if row[0] == missing_model]
    assert len(missing_groups) == 2
    assert [row[2] for row in missing_groups] == [1, 2]

    missing_entry = next(entry for entry in body["per_model"]
                         if entry["model"] == missing_model)
    assert missing_entry["cost_total"] == pytest.approx(12.0)
    assert missing_entry["cost_buckets"]["fresh"] == pytest.approx(12.0)
    assert abs(sum(missing_entry["cost_buckets"].values())
               - missing_entry["cost_total"]) <= 3e-4
