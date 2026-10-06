"""The API layer passes an unattributed model through unmasked (issue #653).

parse_lanes.refuse_unattributed keeps a model-less lane file out of the
database, and records.model is NOT NULL — so the hole the API fold used
to mask as `unknown` is reachable only through an out-of-band row, which
this module seeds directly.
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import api, db, ingest
from tests import scratch_db

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _scalar(cur, sql: str):
    """First column of the first row; the query must yield one."""
    row = cur.execute(sql).fetchone()
    assert row is not None, f"expected a row: {sql[:80]}"
    return row[0]


@pytest.fixture(name="client")
def _client_fixture(monkeypatch):
    """Fresh DB + mini R2 + ingest, yielding a TestClient on the router.

    Auth is bypassed by mounting only the router into a clean app (the
    shape tests/test_api.py's _build_api_client uses)."""
    test_db = scratch_db.create_database("model_demask")
    monkeypatch.setenv("DATABASE_URL_VIZ", f"postgresql:///{test_db}")
    src = _REPO_ROOT / "fixtures/r2_mini"
    tmp = tempfile.mkdtemp(prefix="sv-demask-")
    shutil.copytree(src, Path(tmp) / "r2")
    monkeypatch.setenv("R2_ENDPOINT", f"file://{tmp}/r2/")

    db.reset_viz_pool()
    ingest.run_ingest(trigger="manual")

    a = FastAPI()
    a.include_router(api.router)
    yield TestClient(a)

    db.reset_viz_pool()
    shutil.rmtree(tmp)
    scratch_db.drop_database(test_db)


def test_a_null_model_row_is_not_masked_as_unknown(client):
    """The de-masking control for the issue-653 API sites: ingest refuses
    a file whose rows name no model, and records.model is NOT NULL — so
    the hole the fold used to mask is the EMPTY-STRING model, seeded here
    directly through SQL. The fold and the hourly panel pass it through
    as the empty string (never the `unknown` string), and pricing falls
    back to the default rates instead of renaming the hole."""
    with db.viz_conn() as c:
        c.execute(
            """
            INSERT INTO records (file_key, line_num, uuid, ts, model,
                                 fresh_tokens, output_tokens, cost_usd)
            VALUES ('claude/projA/sess-A/sess-A.jsonl', 9999,
                    'empty-model-control-uuid', now(), '', 100, 5, 0.0)
            """
        )

    # bucket_s < 3600 takes the live pass over records, where the seeded
    # row actually sits (usage_rollup is derived and still excludes it).
    r = client.get("/api/dashboard?range=24h")
    assert r.status_code == 200
    hourly = r.json()["hourly"]
    models = [entry["model"] for entry in hourly]
    assert "" in models, models
    assert "unknown" not in models, models


def test_rollup_grains_do_not_mask_an_empty_model(client):
    """The rollup grains shared the read path's masking (issue #686): a
    stored empty-string model was grouped as `unknown` in usage_rollup
    and latency_rollup, so the >=1h views disagreed with the live pass.
    The seed sits on an existing file key, so the second ingest does not
    reparse it; DELETE FROM ingest_derived_state forces the full derived
    rebuild that carries the seeded row into both rollups."""
    with db.viz_conn() as c:
        c.execute(
            """
            INSERT INTO records (file_key, line_num, uuid, ts, model,
                                 fresh_tokens, output_tokens, cost_usd,
                                 reply_latency_s)
            VALUES ('claude/projA/sess-A/sess-A.jsonl', 9998,
                    'empty-model-rollup-uuid', now(), '', 100, 5, 0.0, 0.5)
            """
        )
        c.execute("DELETE FROM ingest_derived_state")

    ingest.run_ingest(trigger="manual")

    with db.viz_conn() as c:
        usage_empty = _scalar(c, "SELECT COUNT(*) FROM usage_rollup "
                                 "WHERE model = ''")
        usage_unknown = _scalar(c, "SELECT COUNT(*) FROM usage_rollup "
                                   "WHERE model = 'unknown'")
        latency_empty = _scalar(c, "SELECT COUNT(*) FROM latency_rollup "
                                   "WHERE model = ''")
        latency_unknown = _scalar(c, "SELECT COUNT(*) FROM latency_rollup "
                                     "WHERE model = 'unknown'")
    assert usage_empty >= 1, (usage_empty, usage_unknown)
    assert latency_empty >= 1, (latency_empty, latency_unknown)
    assert usage_unknown == 0
    assert latency_unknown == 0

    # The >=1h dashboard view reads usage_rollup; it must agree with the
    # live pass the 24h test above pins.
    r = client.get("/api/dashboard?range=7d")
    assert r.status_code == 200
    models = [entry["model"] for entry in r.json()["hourly"]]
    assert "" in models, models
    assert "unknown" not in models, models
