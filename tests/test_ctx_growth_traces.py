"""The context-growth traces panel endpoint and the traces move (issue
#644): the per-file traces left the /api/dashboard payload — they were
82% of its gzip — and the panel fetches
/api/context-growth/traces itself. Also the /api/models read-path pins
that landed with the same fan-out work: the response cache (it re-ran a
~1s GROUP BY on every open) and the is_canonical filter
(SV-CANONICAL-FLAG).
"""
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
from tests import mini_mirror, scratch_db

_REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module", name="client")
def _client_fixture():
    """Fresh DB + mini R2 + ingest, yielding a TestClient on the api
    router (auth bypassed), the same shape tests/test_api.py uses."""
    mp = pytest.MonkeyPatch()
    test_db = scratch_db.create_database("ctx_growth_traces")
    mp.setenv("DATABASE_URL_VIZ", f"postgresql:///{test_db}")
    src = _REPO_ROOT / "fixtures/r2_mini"
    tmp = tempfile.mkdtemp(prefix="sv-ctx-traces-")
    shutil.copytree(src, Path(tmp) / "r2")
    mp.setenv("R2_ENDPOINT", f"file://{tmp}/r2/")
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
    """GET /api/models re-ran a full GROUP BY over records on every page
    open (measured ~1s on allmeter at range=all scale) — the one fan-out
    request that was neither cached nor warmed. The cached endpoint
    answers the second call from the cache, not the database (issue
    #644): the same dict object comes back, no second query runs."""
    first = api.list_models()
    second = api.list_models()
    assert second is first


def test_models_excludes_non_canonical_rows(client):
    """A read over records filters is_canonical (SV-CANONICAL-FLAG).
    list_models counted every row including replayed copies, so a model
    seen only by a losing duplicate was offered in the dropdown."""
    with closing(psycopg.connect(os.environ["DATABASE_URL_VIZ"])) as conn, \
            conn.cursor() as cur:
        cur.execute(
            "INSERT INTO records (file_key, line_num, uuid, ts, model, "
            "output_tokens, cost_usd, is_canonical) "
            "SELECT file_key, 9001, 'uuid-644-noncanon', now(), "
            "'zzz-only-on-dupes', 1, 0.0, FALSE FROM files LIMIT 1"
        )
        conn.commit()

        names = [m["model"] for m in
                 client.get("/api/models").json()["models"]]
        assert "zzz-only-on-dupes" not in names

        cur.execute(
            "UPDATE records SET is_canonical = TRUE "
            "WHERE uuid = 'uuid-644-noncanon'"
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
    """/api/context-growth/traces returns the per-file traces the
    dashboard payload used to carry, projected the same way ({model,
    turns}, turns flattened to ctx ints): the panel's reading is
    unchanged. Every session whose ctx_at_end is set must find a trace
    ending on that value — the fold and the panel now read one source."""
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
    with closing(psycopg.connect(os.environ["DATABASE_URL_VIZ"])) as conn, \
            conn.cursor() as cur:
        cur.execute(
            "SELECT file_key FROM files "
            "WHERE jsonb_array_length(ctx_turns) > 0 LIMIT 1"
        )
        row = cur.fetchone()
        assert row is not None, "fixture has no traced file"
        (file_key,) = row
        cur.execute(
            "INSERT INTO records (file_key, line_num, uuid, ts, model, "
            "output_tokens, cost_usd, is_canonical) "
            "SELECT %s, 9100 + n, 'uuid-644-cte-' || n, now(), "
            "'zzz-dupe-only-model', 1, 0.0, FALSE "
            "FROM generate_series(1, 3) AS n",
            (file_key,),
        )
        conn.commit()
        cache.response_cache.clear()

        traces = client.get(
            "/api/context-growth/traces?range=all").json()["traces"]
        models = {t["model"] for t in traces
                  if t["model"] == "zzz-dupe-only-model"}
        assert not models, \
            "a model existing only on non-canonical rows won the fold"
