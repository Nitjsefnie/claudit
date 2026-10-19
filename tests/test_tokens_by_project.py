"""Tokens by Project keeps free usage and folds the billed token partition into top-ten rows and a remainder."""
from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import api, api_dashboard, cache, db, ingest
from tests import scratch_db
from tests import dims_seed

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _build_api_client(mp, label: str):
    """Fresh DB + mini R2 + ingest, yielding a TestClient on the api router.

    Auth is bypassed by mounting only the router into a clean app.
    """
    test_db = scratch_db.create_database(label)
    mp.setenv("CLAUDIT_DATABASE_URL_VIZ", f"postgresql:///{test_db}")
    src = _REPO_ROOT / "fixtures/r2_mini"
    tmp = tempfile.mkdtemp(prefix="sv-tokens-proj-")
    shutil.copytree(src, Path(tmp) / "r2")
    mp.setenv("CLAUDIT_R2_ENDPOINT", f"file://{tmp}/r2/")

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


def test_dashboard_tokens_by_project_shows_a_free_lane(app_with_fresh_data):
    """A project whose usage cost $0 but moved tokens appears in
    tokens_by_project (the llama free-lane case): the tokens panel
    measures usage, not money. Cost by Project keeps its rule — the
    zero-cost project stays out of THAT list."""
    with closing(psycopg.connect(os.environ["CLAUDIT_DATABASE_URL_VIZ"])) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO projects (project_id, display_name, "
            "first_seen_at, last_seen_at) "
            "VALUES ('projFree', 'projFree', now(), now())"
        )
        dims_seed.seed_usage_rollup(
            cur, "projFree", "sess-free",
            datetime.now(timezone.utc).replace(
                minute=0, second=0, microsecond=0),
            requests=2, fresh_tokens=1234, cost_usd=0,
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
    rows = [(f"p{i}", 12 - i) for i in range(12)]
    folded = api_dashboard._fold_tokens_by_project(  # pylint: disable=protected-access
        rows)
    assert len(folded) == 11
    assert [r["project"] for r in folded[:10]] == [
        f"p{i}" for i in range(10)]
    other = folded[10]
    assert other["project"] == "Other (2 projects)"
    assert other["total_tokens"] == 1 + 2
