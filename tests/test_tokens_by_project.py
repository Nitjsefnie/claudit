"""/api/dashboard tokens_by_project — the Tokens by Project panel.

The billed partition per project (fresh + cache_creation + cache_read +
output; thinking_tokens is a SUBSET of output and is never added,
SV-SUBSET-TOKENS), folded like cost_by_project: top 10 plus one
"Other (N projects)" remainder.

Split out of tests/test_api.py (the module tripped pylint's 1000-line
gate). Fixture construction is tests/test_api.py's, kept local so the
module stands alone (the tests/test_multi_bucket.py convention). The
endpoint requests carry fresh=1 so no other module's response-cache
entry can serve this module a payload computed against another DB.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import closing
from pathlib import Path

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import api, api_dashboard, cache, db, ingest
from tests import scratch_db

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _build_api_client(mp, label: str):
    """Fresh DB + mini R2 + ingest, yielding a TestClient on the api router.

    Auth is bypassed by mounting only the router into a clean app.
    """
    test_db = scratch_db.create_database(label)
    mp.setenv("DATABASE_URL_VIZ", f"postgresql:///{test_db}")
    src = _REPO_ROOT / "fixtures/r2_mini"
    tmp = tempfile.mkdtemp(prefix="sv-tokens-proj-")
    shutil.copytree(src, Path(tmp) / "r2")
    mp.setenv("R2_ENDPOINT", f"file://{tmp}/r2/")

    # Response cache is process-global and keyed by endpoint+params only:
    # an entry another test module populated would serve foreign data.
    cache.response_cache.clear()
    db.reset_viz_pool()

    ingest.run_ingest(trigger="manual")

    a = FastAPI()
    a.include_router(api.router)

    yield TestClient(a)

    db.reset_viz_pool()
    shutil.rmtree(tmp)
    scratch_db.drop_database(test_db)


@pytest.fixture(scope="module", name="app_with_data")
def _app_with_data_fixture():
    mp = pytest.MonkeyPatch()          # monkeypatch itself is function-scoped
    try:
        yield from _build_api_client(mp, "tokens")
    finally:
        mp.undo()


@pytest.fixture(name="app_with_fresh_data")
def _app_with_fresh_data_fixture():
    """Function-scoped variant for tests that mutate rows, so they cannot
    contaminate the shared module-scoped client."""
    mp = pytest.MonkeyPatch()
    try:
        yield from _build_api_client(mp, "tokens_mut")
    finally:
        mp.undo()


def test_dashboard_tokens_by_project_shape(app_with_data):
    """tokens_by_project is cost_by_project's fold measured in the billed
    token partition Tokens by Model sums — fresh + cache_creation +
    cache_read + output, sorted desc, zero-token rows out, top 10 plus
    at most one "Other (N projects)". thinking_tokens is a SUBSET of
    output and must NOT be added (SV-SUBSET-TOKENS): the exact-sum
    comparison against the stored records catches that."""
    body = app_with_data.get("/api/dashboard?range=3650d&fresh=1").json()
    tbp = body["tokens_by_project"]
    assert {"projA", "projB"} <= {r["project"] for r in tbp}
    tokens = [r["total_tokens"] for r in tbp]
    assert all(t > 0 for t in tokens)
    assert tokens == sorted(tokens, reverse=True)
    named = [r for r in tbp if not r["project"].startswith("Other (")]
    assert len(named) <= 10
    others = [r for r in tbp if r["project"].startswith("Other (")]
    assert len(others) <= 1

    # EXACT sums over the deduped records, computed with the same
    # partition the panel claims to chart.
    with db.viz_conn() as c:
        expected = dict(c.execute(
            "SELECT f.project_id, "
            "SUM(d.fresh_tokens + d.output_tokens + d.cache_creation_tokens "
            "+ d.cache_read_tokens) "
            "FROM records d JOIN files f ON f.file_key = d.file_key "
            "WHERE d.is_canonical GROUP BY 1"
        ).fetchall())
    by_proj = {r["project"]: r["total_tokens"] for r in tbp
               if not r["project"].startswith("Other (")}
    for pid, total in expected.items():
        assert by_proj[pid] == int(total), (
            f"{pid}: the panel's sum drifted from the billed partition")


def test_dashboard_tokens_by_project_shows_a_free_lane(app_with_fresh_data):
    """A project whose usage cost $0 but moved tokens appears in
    tokens_by_project (the llama free-lane case): the tokens panel
    measures usage, not money. Cost by Project keeps its rule — the
    zero-cost project stays out of THAT list."""
    with closing(psycopg.connect(os.environ["DATABASE_URL_VIZ"])) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO projects (project_id, display_name, "
            "first_seen_at, last_seen_at) "
            "VALUES ('projFree', 'projFree', now(), now())"
        )
        cur.execute(
            "INSERT INTO usage_rollup (session_id, project_id, hour, model, "
            "is_main, first_ts, last_ts, requests, fresh_tokens, cost_usd) "
            "VALUES ('sess-free', 'projFree', date_trunc('hour', now()), "
            "'m', TRUE, now(), now(), 2, 1234, 0)"
        )
        conn.commit()
    body = app_with_fresh_data.get("/api/dashboard?range=3650d&fresh=1").json()
    rows = {r["project"]: r for r in body["tokens_by_project"]}
    assert rows["projFree"]["total_tokens"] == 1234
    assert "projFree" not in {r["project"] for r in body["cost_by_project"]}


def test_fold_tokens_by_project_top10_and_remainder():
    """The fold is cost_by_project's: top 10 by tokens, the tail as ONE
    "Other (N projects)" row carrying the summed remainder. The query
    hands the fold DESC-sorted rows (the slice is the top-10), so the
    fixture rows are sorted too."""
    rows = [(f"p{i}", 12 - i) for i in range(12)]  # p0..p11 → tokens 12..1
    folded = api_dashboard._fold_tokens_by_project(  # pylint: disable=protected-access
        rows)
    assert len(folded) == 11
    assert [r["project"] for r in folded[:10]] == [
        f"p{i}" for i in range(10)]
    other = folded[10]
    assert other["project"] == "Other (2 projects)"
    assert other["total_tokens"] == 1 + 2
