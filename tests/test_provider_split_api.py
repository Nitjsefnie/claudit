"""The provider dimension end to end: ingest stores records.provider, the
rollup keeps it in its grain, and /api/dashboard and /api/cache expose
cost and tokens per (model, provider) beside the per-model views, whose
totals do not move.
"""
import json
import os
import shutil
import tempfile
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import api, db, ingest, pricing
from tests import scratch_db
from tests.conftest import seam_now

_REPO_ROOT = Path(__file__).resolve().parent.parent
V41 = "deepseek/deepseek-v4.1-flash"

# (provider or None, model, fresh, cache_read, output). Two hosts and a
# provider-less record of the same model, plus a second model with none.
_REQUESTS = [
    ("Novita", V41, 1000, 2000, 300),
    ("Morph", V41, 4000, 0, 100),
    (None, V41, 1000, 2000, 300),
    (None, "glm-5.3-flash", 5000, 1000, 700),
]


def _lines(start: datetime) -> str:
    out = [{"type": "user", "timestamp": start.isoformat(), "uuid": "u0",
            "message": {"role": "user", "content": "hi"}}]
    for i, (provider, model, fresh, read, output) in enumerate(_REQUESTS, 1):
        msg = {"id": f"gen-{i}", "type": "message", "role": "assistant",
               "model": model, "content": [{"type": "text", "text": "ok"}],
               "stop_reason": "end_turn",
               "usage": {"input_tokens": fresh, "cache_read_input_tokens": read,
                         "cache_creation_input_tokens": 0,
                         "output_tokens": output}}
        if provider:
            msg["provider"] = provider
        out.append({"type": "assistant", "uuid": f"a{i}",
                    "requestId": f"req-{i}", "message": msg,
                    "timestamp": (start + timedelta(seconds=i)).isoformat()})
    return "\n".join(json.dumps(o) for o in out) + "\n"


def _stored_cost(provider, model, fresh, read, output, ts):
    return round(pricing.compute_cost(
        model, fresh=fresh, output=output, eph5=0, eph1h=0,
        unsplit_create=0, read=read, ts=ts,
        res=pricing.resolve(model, ts, provider)), 6)


@pytest.fixture(scope="module", name="client")
def _client_fixture():
    mp = pytest.MonkeyPatch()
    test_db = scratch_db.create_database("provider_split")
    tmp = tempfile.mkdtemp(prefix="sv-provider-")
    # The records' ts is entry-sensitive under the perturbation: which
    # rate entry is in force there moves with the bot's hourly stamps,
    # so the instant comes from the seam, never the bare clock.
    start = seam_now() - timedelta(hours=2)
    sess = Path(tmp) / "r2" / "claude" / "projOR" / "sess-or"
    sess.mkdir(parents=True)
    (sess / "sess-or.jsonl").write_text(_lines(start), encoding="utf-8")
    try:
        mp.setenv("DATABASE_URL_VIZ", f"postgresql:///{test_db}")
        mp.setenv("R2_ENDPOINT", f"file://{tmp}/r2/")
        db.reset_viz_pool()
        ingest.run_ingest(trigger="manual")
        a = FastAPI()
        a.include_router(api.router)
        yield TestClient(a), start
    finally:
        db.reset_viz_pool()
        shutil.rmtree(tmp)
        scratch_db.drop_database(test_db)
        mp.undo()


def _expected(start):
    return [(p, m, _stored_cost(p, m, f, r, o, start + timedelta(seconds=i)),
             f + r + o)
            for i, (p, m, f, r, o) in enumerate(_REQUESTS, 1)]


def test_ingest_stores_the_provider_and_prices_by_it(client):
    _, start = client
    with closing(psycopg.connect(os.environ["DATABASE_URL_VIZ"])) as conn:
        rows = conn.execute(
            "SELECT provider, model, cost_usd FROM records ORDER BY line_num"
        ).fetchall()
    assert [(p, m, float(c)) for p, m, c in rows] == \
        [(p, m, pytest.approx(c)) for p, m, c, _ in _expected(start)]


def test_the_rollup_keeps_provider_in_its_grain(client):
    with closing(psycopg.connect(os.environ["DATABASE_URL_VIZ"])) as conn:
        got = conn.execute(
            "SELECT model, provider, SUM(requests) FROM usage_rollup "
            "GROUP BY 1, 2 ORDER BY 1, 2"
        ).fetchall()
    # '' is the rollup's spelling of a NULL provider: it is part of the
    # primary key, which admits no NULL.
    assert sorted(got) == sorted([(V41, "", 1), (V41, "Morph", 1),
                                  (V41, "Novita", 1), ("glm-5.3-flash", "", 1)])


@pytest.mark.parametrize("rng", ["3650d", "24h"])  # rollup and live paths
def test_dashboard_splits_cost_and_tokens_by_model_and_provider(client, rng):
    c, start = client
    body = c.get(f"/api/dashboard?range={rng}&fresh=1").json()
    want = {}
    for p, m, cost, tokens in _expected(start):
        cur = want.setdefault((m, p), [0.0, 0])
        cur[0] += cost
        cur[1] += tokens
    got = {(r["model"], r["provider"]): [r["cost_usd"], r["total_tokens"]]
           for r in body["cost_by_model_provider"]}
    assert set(got) == set(want)
    for key, (cost, tokens) in want.items():
        assert got[key][0] == pytest.approx(cost)
        assert got[key][1] == tokens

    # The per-model view and the hourly total are unchanged by the split.
    by_model = {r["model"]: r["cost_usd"] for r in body["cost_by_model"]}
    assert by_model[V41] == pytest.approx(
        sum(v[0] for (m, _), v in want.items() if m == V41))
    total = sum(h["cost_usd"] for h in body["hourly"])
    assert total == pytest.approx(sum(v[0] for v in want.values()))
    assert {h["provider"] for h in body["hourly"]} == {"Novita", "Morph", None}


def test_cache_splits_per_model_by_provider_and_keeps_the_totals(client):
    c, start = client
    body = c.get("/api/cache?range=30d").json()
    split = {(e["model"], e["provider"]): e for e in body["per_model_provider"]}
    for p, m, cost, _ in _expected(start):
        # The fold rounds each total to 4 places; `cost` reaches the same
        # value through a DIFFERENT pipeline (the DB's numeric SUM,
        # converted and folded, against this side's own float), so the
        # two roundings of one 6-place value can straddle a 4-decimal
        # midpoint by one unit in the 4th place: |a - b| <= 2 * 5e-5
        # = 1e-4 strict, and 1.5e-4 adds float margin.
        assert split[(m, p)]["cost_total"] == pytest.approx(
            round(cost, 4), abs=1.5e-4)
        # Five buckets + the total: six values rounded on the same
        # 4-decimal grid, each within half a unit (5e-5) of its true
        # value, so the two sides sit at most 6 * 5e-5 = 3e-4 apart
        # whatever the rates are.
        assert abs(sum(split[(m, p)]["cost_buckets"].values())
                   - split[(m, p)]["cost_total"]) <= 3e-4
    per_model = {e["model"]: e for e in body["per_model"]}
    assert "provider" not in per_model[V41]
    assert per_model[V41]["turns"] == 3
    # A per-model total is the fold's own accumulation — a naive `+=`
    # chain over its rows' stored costs in the SQL's cost-DESC order —
    # while this side re-derives with builtin sum() (compensated since
    # CPython 3.12) over the same values in request order. Two
    # independent summation paths: at a 4-decimal midpoint they round to
    # OPPOSITE sides (the proven case: exact sum 0.014850 -> compensated
    # 0.0149, naive 0.0148), so a straddle bound, not exactness:
    # |round_a - round_b| <= 2 * 5e-5 = 1e-4 strict, + float margin.
    v41_total = sum(cost for _, m, cost, _ in _expected(start) if m == V41)
    assert per_model[V41]["cost_total"] == pytest.approx(
        round(v41_total, 4), abs=1.5e-4)
    # session_total sums the per-model entries' ALREADY-ROUNDED totals
    # and rounds again (backend/api_cache._session_total). Both folds
    # are two-step, and the per-model totals themselves straddle by up
    # to 1e-4 per model (above): with M models the strict bound is
    # M * 1e-4 (first step) + 1e-4 (the final two-sided rounding)
    # = (M + 1) * 1e-4, plus half a unit of margin.
    by_model = {}
    for _, m, cost, _ in _expected(start):
        by_model[m] = by_model.get(m, 0.0) + cost
    session_tol = (len(by_model) + 1) * 1e-4 + 0.5e-4
    assert body["session_total"]["cost_total"] == pytest.approx(
        round(sum(round(v, 4) for v in by_model.values()), 4),
        abs=session_tol)
