"""Per-pair cache folding and the global fallback for missing rollup pairs."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import api, api_cache, db, pricing
from backend.cache import response_cache
from tests import scratch_db

UTC = timezone.utc


@dataclass(frozen=True)
class _NoisyEpochCase:
    stable_pair: tuple[str, str]
    noisy_pair: tuple[str, str]
    changing_pair: tuple[str, str]
    noisy_bounds: list[datetime]
    records: list[tuple[str, str, datetime, int]]
    rollup_pairs: list[tuple[str, str, datetime]]


@dataclass(frozen=True)
class _MissingPairCase:
    present_pair: tuple[str, str]
    missing_pair: tuple[str, str]
    records: list[tuple[str, str, datetime, int]]
    rollup_pairs: list[tuple[str, str, datetime]]


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
    original = api_cache._cache_queries  # pylint: disable=protected-access

    def capture(conn, phases, canon_source, canon_args):
        result = original(conn, phases, canon_source, canon_args)
        captured["rows"] = result[0]
        captured["pair_bounds"] = result[4] if len(result) > 4 else {}
        return result

    monkeypatch.setattr(api_cache, "_cache_queries", capture)
    return captured


def _install_noisy_epoch_case(monkeypatch) -> _NoisyEpochCase:
    pairs = {
        "stable": ("acme/fold-stable-302", "StableHost"),
        "noisy": ("acme/fold-noisy-302", "NoiseHost"),
        "changing": ("acme/fold-changing-302", "ChangingHost"),
    }
    noisy_bounds = [datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=i)
                    for i in range(50)]
    changing_cut = datetime(2026, 3, 1, tzinfo=UTC)
    noise_rates = [_rates(100 + i) for i in range(len(noisy_bounds) + 1)]
    changing_rates = {"before": _rates(2), "after": _rates(7)}

    monkeypatch.setattr(pricing, "MODEL_RATES", {})
    monkeypatch.setattr(pricing, "DATED_RATES", {})
    monkeypatch.setattr(pricing, "PROVIDER_RATES", {
        pairs["stable"]: _rates(3), pairs["noisy"]: noise_rates[-1],
        pairs["changing"]: changing_rates["after"],
    })
    monkeypatch.setattr(pricing, "PROVIDER_DATED_RATES", {
        pairs["noisy"]: list(zip(noisy_bounds, noise_rates[:-1], strict=True)),
        pairs["changing"]: [(changing_cut, changing_rates["before"])],
    })
    monkeypatch.setattr(pricing, "PROVIDER_STARTS", {})
    monkeypatch.setattr(pricing, "PROVIDER_SCHEDULES", {})
    monkeypatch.setattr(pricing, "_TIER_FALLBACKS", ())
    monkeypatch.setattr(pricing, "DEFAULT_RATES", _rates(89))
    monkeypatch.setattr(pricing, "RATE_EPOCHS", sorted(noisy_bounds + [changing_cut]))

    records = [
        (pairs["stable"][0], pairs["stable"][1],
         noisy_bounds[0] - timedelta(days=1),
         1_000_000),
        (pairs["stable"][0], pairs["stable"][1],
         noisy_bounds[-1] + timedelta(days=1),
         1_000_000),
        (pairs["changing"][0], pairs["changing"][1],
         changing_cut - timedelta(days=1),
         1_000_000),
        (pairs["changing"][0], pairs["changing"][1], changing_cut, 1_000_000),
    ]
    rollup_pairs = [
        (*pairs["stable"], records[0][2]),
        (*pairs["noisy"], noisy_bounds[0]),
        (*pairs["changing"], changing_cut),
    ]
    return _NoisyEpochCase(
        pairs["stable"], pairs["noisy"], pairs["changing"], noisy_bounds,
        records, rollup_pairs)


def test_cache_fold_ignores_other_pairs_boundaries_and_keeps_own_cutovers(
        api_client, monkeypatch):
    case = _install_noisy_epoch_case(monkeypatch)
    _seed_records(case.records, case.rollup_pairs)
    captured = _capture_per_model_rows(monkeypatch)

    response = api_client.get("/api/cache?range=all")
    assert response.status_code == 200
    body = response.json()
    assert captured["rows"], "seeded canonical records must make fold rows"
    assert len(case.noisy_bounds) == 50
    assert captured["pair_bounds"][case.stable_pair] == []
    assert len(captured["pair_bounds"][case.noisy_pair]) == 50

    stable_groups = [row for row in captured["rows"]
                     if row[:2] == case.stable_pair]
    changing_groups = [row for row in captured["rows"]
                       if row[:2] == case.changing_pair]
    assert len(stable_groups) == 1
    assert stable_groups[0][2] == 0
    assert stable_groups[0][4] == 2
    assert {row[2] for row in changing_groups} == {0, 1}
    assert len(changing_groups) == 2

    per_model = {entry["model"]: entry for entry in body["per_model"]}
    assert set(per_model) == {case.stable_pair[0], case.changing_pair[0]}
    assert per_model[case.stable_pair[0]]["cost_total"] == pytest.approx(6.0)
    assert per_model[case.stable_pair[0]]["cost_buckets"]["fresh"] == \
        pytest.approx(6.0)
    assert per_model[case.changing_pair[0]]["cost_total"] == pytest.approx(9.0)
    assert per_model[case.changing_pair[0]]["cost_buckets"]["fresh"] == \
        pytest.approx(9.0)
    for entry in per_model.values():
        assert abs(sum(entry["cost_buckets"].values())
                   - entry["cost_total"]) <= 3e-4


def _install_missing_pair_case(monkeypatch) -> _MissingPairCase:
    pairs = {
        "present": ("acme/fold-present-302", "PresentHost"),
        "missing": ("acme/fold-missing-302", "MissingHost"),
    }
    unrelated_cut = datetime(2026, 3, 1, tzinfo=UTC)
    missing_cut = datetime(2026, 4, 1, tzinfo=UTC)
    target_rates = {"before": _rates(5), "after": _rates(7)}

    monkeypatch.setattr(pricing, "MODEL_RATES", {})
    monkeypatch.setattr(pricing, "DATED_RATES", {})
    monkeypatch.setattr(pricing, "PROVIDER_RATES", {
        pairs["present"]: _rates(11), pairs["missing"]: target_rates["after"],
    })
    monkeypatch.setattr(pricing, "PROVIDER_DATED_RATES", {
        pairs["present"]: [(unrelated_cut, _rates(9))],
        pairs["missing"]: [(missing_cut, target_rates["before"])],
    })
    monkeypatch.setattr(pricing, "PROVIDER_STARTS", {})
    monkeypatch.setattr(pricing, "PROVIDER_SCHEDULES", {})
    monkeypatch.setattr(pricing, "_TIER_FALLBACKS", ())
    monkeypatch.setattr(pricing, "DEFAULT_RATES", _rates(89))
    monkeypatch.setattr(pricing, "RATE_EPOCHS", [unrelated_cut, missing_cut])

    records = [
        (pairs["missing"][0], pairs["missing"][1],
         missing_cut - timedelta(days=1),
         1_000_000),
        (pairs["missing"][0], pairs["missing"][1], missing_cut, 1_000_000),
    ]
    rollup_pairs = [(*pairs["present"], unrelated_cut)]
    return _MissingPairCase(pairs["present"], pairs["missing"], records,
                            rollup_pairs)


def test_cache_missing_rollup_pair_uses_global_epoch_fallback(
        api_client, monkeypatch):
    case = _install_missing_pair_case(monkeypatch)
    # The missing pair's records are deliberately absent from this rollup
    # list, as after ingest writes records but before its rollup rebuild.
    _seed_records(case.records, case.rollup_pairs)
    captured = _capture_per_model_rows(monkeypatch)

    response = api_client.get("/api/cache?range=all")
    assert response.status_code == 200
    body = response.json()
    assert captured["rows"], "the unrolled records must still be selected"
    assert case.present_pair in captured["pair_bounds"]
    assert case.missing_pair not in captured["pair_bounds"]
    missing_groups = [row for row in captured["rows"]
                      if row[0] == case.missing_pair[0]]
    assert len(missing_groups) == 2
    assert {row[2] for row in missing_groups} == {1, 2}

    missing_entry = next(entry for entry in body["per_model"]
                         if entry["model"] == case.missing_pair[0])
    assert missing_entry["cost_total"] == pytest.approx(12.0)
    assert missing_entry["cost_buckets"]["fresh"] == pytest.approx(12.0)
    assert abs(sum(missing_entry["cost_buckets"].values())
               - missing_entry["cost_total"]) <= 3e-4
