"""Fetch per-file context traces separately from the dashboard response.
Cache the models endpoint so repeated opens avoid full GROUP BY work, and
filter it by canonical records (SV-CANONICAL-FLAG)."""
from contextlib import closing
from pathlib import Path
import os
import shutil
import tempfile

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import api, cache, db, ingest
from tests import dims_seed, mini_mirror, scratch_db

_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module", name="client")
def _client_fixture():
    mp = pytest.MonkeyPatch()
    test_db = scratch_db.create_database("ctx_growth_traces")
    mp.setenv("CLAUDIT_DATABASE_URL_VIZ", f"postgresql:///{test_db}")
    src = _REPO_ROOT / "fixtures/r2_mini"
    tmp = tempfile.mkdtemp(prefix="sv-ctx-traces-")
    shutil.copytree(src, Path(tmp) / "r2")
    mp.setenv("CLAUDIT_R2_ENDPOINT", f"file://{tmp}/r2/")
    db.reset_viz_pool()
    ingest.run_ingest(trigger="manual")
    a = FastAPI()
    a.include_router(api.router)
    yield TestClient(a)
    db.reset_viz_pool()
    shutil.rmtree(tmp)
    scratch_db.drop_database(test_db)
    mp.undo()


def test_models_response_is_cached(client):
    """A second /api/models call returns the cached dict without a second query."""
    first = api.list_models()
    second = api.list_models()
    assert second is first


def test_models_excludes_non_canonical_rows(client):
    """A read over records filters is_canonical (SV-CANONICAL-FLAG).
    list_models counted every row including replayed copies, so a model
    seen only by a losing duplicate was offered in the dropdown."""
    with closing(psycopg.connect(os.environ["CLAUDIT_DATABASE_URL_VIZ"])) as conn, \
            conn.cursor() as cur:
        mid = dims_seed.dim_id(cur, "dim_model", "zzz-only-on-dupes")
        cur.execute(
            "INSERT INTO records (file_id, line_num, fallback, ts, model_id, "
            "output_tokens, is_canonical) "
            "SELECT file_id, 9001, 'uuid-644-noncanon', now(), %s, "
            "1, FALSE FROM files LIMIT 1",
            (mid,),
        )
        cur.execute(
            "INSERT INTO records_price (file_id, line_num, cost_usd) "
            "SELECT file_id, 9001, 0.0 FROM files LIMIT 1")
        conn.commit()

        names = [m["model"] for m in
                 client.get("/api/models").json()["models"]]
        assert "zzz-only-on-dupes" not in names

        cur.execute(
            "UPDATE records SET is_canonical = TRUE "
            "WHERE fallback = 'uuid-644-noncanon'"
        )
        conn.commit()
        # Out-of-band row mutation: production invalidates the response
        # cache at ingest; the test does the same by hand.
        cache.response_cache.clear()
        names = [m["model"] for m in
                 client.get("/api/models").json()["models"]]
        assert "zzz-only-on-dupes" in names


def test_dashboard_carries_no_ctx_traces(client):
    """The dashboard response must not carry the per-file traces any
    more — the sessions' ctx_at_end fold still runs server-side from
    the same rows."""
    body = client.get("/api/dashboard?range=all").json()
    assert "ctx_traces" not in body
    # The fold that outlived the move: sessions still carry ctx_at_end.
    sessions = body["sessions"]
    assert sessions, "fixture produced no sessions"
    assert any(s["ctx_at_end"] is not None for s in sessions), \
        "ctx_at_end fold lost by the traces move"


def test_context_growth_traces_serves_the_panel_rows(client):
    """Project per-file traces as {model, turns}, flattening turns to ctx ints.
    Every session with ctx_at_end must have a trace ending at that value,
    because the fold and panel read one source."""
    traces = client.get(
        "/api/context-growth/traces?range=all").json()["traces"]
    assert traces, "fixture produced no traces"
    for t in traces:
        assert set(t) == {"model", "turns"}
        assert all(isinstance(v, int) for v in t["turns"])

    body = client.get("/api/dashboard?range=all").json()
    endings = {s["ctx_at_end"] for s in body["sessions"]
               if s["ctx_at_end"] is not None}
    last_turns = {t["turns"][-1] for t in traces if t["turns"]}
    assert endings <= last_turns, \
        "a session's ctx_at_end has no matching trace"


def test_context_growth_traces_bad_range_400(client):
    assert client.get(
        "/api/context-growth/traces?range=banana").status_code == 400


def test_traces_route_is_mounted_under_api_router(client):
    """The endpoint rides api.router's sub-inclusion (the split-module
    pattern), not a separate app mount: the auth middleware and the
    guest ?project= gate are app-level, but the route must exist for
    every deploy that only includes api.router (the panel-layout
    harness among them)."""
    r = client.get("/api/context-growth/traces?range=all")
    assert r.status_code == 200
    assert mini_mirror.project_ids(), "fixture sanity"


def test_traces_model_fold_ignores_non_canonical_rows(client):
    """The file_models CTE scans records for the per-file dominant model;
    a read over records filters is_canonical (SV-CANONICAL-FLAG). A
    model present ONLY on losing duplicate rows must not win the fold
    even when it outnumbers the canonical rows."""
    with closing(psycopg.connect(os.environ["CLAUDIT_DATABASE_URL_VIZ"])) as conn, \
            conn.cursor() as cur:
        cur.execute(
            "SELECT file_key FROM files "
            "WHERE cardinality(ctx_input) > 0 LIMIT 1"
        )
        row = cur.fetchone()
        assert row is not None, "fixture has no traced file"
        (file_key,) = row
        fid = dims_seed.file_id(cur, file_key)
        mid = dims_seed.dim_id(cur, "dim_model", "zzz-dupe-only-model")
        cur.execute(
            "INSERT INTO records (file_id, line_num, fallback, ts, model_id, "
            "output_tokens, is_canonical) "
            "SELECT %s, 9100 + n, 'uuid-644-cte-' || n, now(), "
            "%s, 1, FALSE "
            "FROM generate_series(1, 3) AS n",
            (fid, mid),
        )
        cur.execute(
            "INSERT INTO records_price (file_id, line_num, cost_usd) "
            "SELECT %s, 9100 + n, 0.0 FROM generate_series(1, 3) AS n",
            (fid,))
        conn.commit()
        cache.response_cache.clear()

        traces = client.get(
            "/api/context-growth/traces?range=all").json()["traces"]
        models = {t["model"] for t in traces
                  if t["model"] == "zzz-dupe-only-model"}
        assert not models, \
            "a model existing only on non-canonical rows won the fold"
