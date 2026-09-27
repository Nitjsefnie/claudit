"""The project_aliases fold pass: unit, end-to-end and picker pins.

One behaviour per test. The unit tests seed a scratch DB the way ingest
leaves state (one `projects` row per id, one `files` row per file); the
end-to-end tests run a real ingest over the mini mirror; the last test
pins /api/projects. The baselined test modules (tests/test_ingest.py,
tests/test_api.py) carry a recorded size that is never raised, so this
feature's tests live together here (SV-CI-RATCHETS).
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

from backend import api, constants, db, ingest, project_aliases
from tests import scratch_db

_REPO_ROOT = Path(__file__).resolve().parent.parent
_FIX_ROOT = _REPO_ROOT / "fixtures"


@pytest.fixture(name="fresh_db")
def _fresh_db_fixture(monkeypatch):
    yield from scratch_db.scratch_viz_database(monkeypatch, "aliases")


@pytest.fixture(name="mini_r2_env")
def _mini_r2_env_fixture(monkeypatch):
    src = _REPO_ROOT / "fixtures/r2_mini"
    tmp = tempfile.mkdtemp(prefix="sv-aliases-")
    shutil.copytree(src, Path(tmp) / "r2")
    monkeypatch.setenv("R2_ENDPOINT", f"file://{tmp}/r2/")
    yield Path(tmp) / "r2" / "claude"
    shutil.rmtree(tmp)


def _scalar(cur, sql: str, params=None):
    """First column of the first row; the query must yield one."""
    row = (cur.execute(sql, params) if params is not None
           else cur.execute(sql)).fetchone()
    assert row is not None, f"expected a row: {sql[:80]}"
    return row[0]


def _seed(cur: psycopg.Cursor, pid: str, keys: list[str]) -> None:
    """One project row + one file row per key, the way ingest leaves it."""
    cur.execute(
        "INSERT INTO projects (project_id, display_name, first_seen_at, "
        "last_seen_at) VALUES (%s, %s, now(), now())",
        (pid, pid))
    for k in keys:
        cur.execute(
            "INSERT INTO files (file_key, project_id, session_id, is_main, "
            "r2_etag, r2_size_bytes, r2_last_modified, parsed_at, "
            "parser_version) "
            "VALUES (%s, %s, 's', TRUE, 'e', 1, now(), now(), 't')",
            (k, pid))


def _alias(pattern: str, target: str) -> None:
    """Add one alias row, the way an operator would."""
    with db.viz_conn() as c:
        c.execute(
            "INSERT INTO project_aliases (pattern, project_id, note) "
            "VALUES (%s, %s, 'test')",
            (pattern, target))
        c.commit()


def _pairs() -> dict[str, str]:
    with db.viz_conn() as c:
        return project_aliases.folded_pairs(c)


def _owners() -> dict[str, int]:
    """File count per stored project id — who owns what after the pass."""
    with db.viz_conn() as c:
        return dict(c.execute(
            "SELECT project_id, COUNT(*) FROM files GROUP BY 1 ORDER BY 1"
        ).fetchall())


def _project_ids() -> set[str]:
    with db.viz_conn() as c:
        return {r[0] for r in c.execute(
            "SELECT project_id FROM projects").fetchall()}


def test_first_match_is_the_lexicographically_smallest_matching_pattern(
        fresh_db):
    """Two patterns match one id: the FIRST in `pattern` order names the
    target. PK(pattern) fixes the order, so which row wins never depends
    on insertion order."""
    with db.viz_conn() as c, c.cursor() as cur:
        _seed(cur, "-tmp-x-1", ["claude/-tmp-x-1/s1/f.jsonl"])
        c.commit()
    _alias("-tmp-x-1%", "zzz-late")
    _alias("-tmp-x-%", "parent-first")

    assert _pairs() == {"-tmp-x-1": "parent-first"}
    assert project_aliases.rekey_folded_projects() == 1
    assert _owners() == {"parent-first": 1}
    assert _project_ids() == {"parent-first"}, (
        "the losing pattern's target never receives rows, so no project "
        "row is materialised for it")


def test_alias_match_is_case_sensitive(fresh_db):
    """LIKE, not ILIKE: '-TMP-%' does not match '-tmp-x'. POSIX project
    slugs are case-sensitive — only the Windows ones are case-folded, by
    key_layout.canonical_project_id — so a case-blind match would merge
    two distinct projects."""
    with db.viz_conn() as c, c.cursor() as cur:
        _seed(cur, "-tmp-x", ["claude/-tmp-x/s1/f.jsonl"])
        _seed(cur, "-TMP-x", ["claude/-TMP-x/s1/f.jsonl"])
        c.commit()
    _alias("-TMP-%", "parent")

    assert _pairs() == {"-TMP-x": "parent"}
    assert project_aliases.rekey_folded_projects() == 1
    assert _owners() == {"-tmp-x": 1, "parent": 1}, (
        "the lowercase id matches nothing and must stay put")


def test_fold_converges_across_passes_without_chaining_within_a_pass(
        fresh_db):
    """'keep-b' is both a target (of '-tmp-a-%') and itself aliased (by
    'keep%'): only the row stored under 'keep-b' BEFORE pass one moves
    on to 'final'. The rows folded onto it stop there for that pass; on
    pass two, that target advances with its rows. Each pass resolves
    ids once against its pre-fold id set, and repeated passes converge
    without losing or duplicating files."""
    with db.viz_conn() as c, c.cursor() as cur:
        _seed(cur, "-tmp-a-1", ["claude/-tmp-a-1/s1/f.jsonl",
                                "claude/-tmp-a-1/s2/f.jsonl"])
        _seed(cur, "keep-b", ["claude/keep-b/s3/f.jsonl"])
        c.commit()
    _alias("-tmp-a-%", "keep-b")
    _alias("keep%", "final")

    assert _pairs() == {"-tmp-a-1": "keep-b", "keep-b": "final"}
    assert project_aliases.rekey_folded_projects() == 2
    assert _owners() == {"final": 1, "keep-b": 2}, (
        "the folded rows stop on 'keep-b'; only its own rows move on")
    assert _project_ids() == {"final", "keep-b"}, (
        "'-tmp-a-1' is emptied and dropped; 'keep-b' now holds the "
        "folded rows, so its own project row survives")

    pass_two = project_aliases.rekey_folded_projects()
    owners_after_two = _owners()
    assert pass_two == 1, "the single matched source project id advances"
    assert owners_after_two == {"final": 3}, (
        "the two files folded onto keep-b advance with keep-b's own file")
    assert sum(owners_after_two.values()) == 3, "no files are lost or added"

    pass_three = project_aliases.rekey_folded_projects()
    assert pass_three == 0, "no stored id matches after convergence"
    assert _owners() == owners_after_two


def test_project_whose_id_is_its_own_target_is_skipped(fresh_db):
    """A project whose own id equals its matched target is neither moved
    nor deleted. Load-bearing because the pass deletes emptied source
    project rows and files FK-cascade on project delete."""
    with db.viz_conn() as c, c.cursor() as cur:
        _seed(cur, "-tmp-x-main", ["claude/-tmp-x-main/s1/f.jsonl"])
        _seed(cur, "-tmp-x-1", ["claude/-tmp-x-1/s1/f.jsonl",
                                "claude/-tmp-x-1/s2/f.jsonl"])
        c.commit()
    _alias("-tmp-x-%", "-tmp-x-main")

    assert _pairs() == {"-tmp-x-1": "-tmp-x-main"}
    assert project_aliases.rekey_folded_projects() == 1
    assert _owners() == {"-tmp-x-main": 3}
    assert _project_ids() == {"-tmp-x-main"}


def test_fold_with_no_remaining_match_moves_zero(fresh_db):
    """A pass with no stored id matching an alias has no work to do."""
    with db.viz_conn() as c, c.cursor() as cur:
        _seed(cur, "-tmp-x-1", ["claude/-tmp-x-1/s1/f.jsonl",
                                "claude/-tmp-x-1/s2/f.jsonl"])
        _seed(cur, "keep", ["claude/keep/s3/f.jsonl"])
        c.commit()
    _alias("-tmp-x-%", "keep")

    assert project_aliases.rekey_folded_projects() == 1
    after_first = _owners()
    assert after_first == {"keep": 3}
    assert project_aliases.rekey_folded_projects() == 0
    assert _owners() == after_first


def test_rekey_is_a_noop_with_an_empty_alias_table(fresh_db):
    """The pass runs on every ingest, so the empty-alias case is the
    common one — and this codebase also ships to deploys that alias
    nothing."""
    with db.viz_conn() as c, c.cursor() as cur:
        _seed(cur, "-tmp-x-1", ["claude/-tmp-x-1/s1/f.jsonl"])
        c.commit()

    assert project_aliases.rekey_folded_projects() == 0
    assert _owners() == {"-tmp-x-1": 1}


# ---------------------------------------------------------------------------
# End-to-end: the fold runs as a phase of the derived-state rebuild, so
# stored identity is folded before any rollup reads it.
# ---------------------------------------------------------------------------


def test_ingest_folds_an_aliased_project_end_to_end(fresh_db, mini_r2_env):
    """A file persisted under its raw key-layout id plus an alias row:
    the SAME run's rebuild folds it — every file lands on the target id,
    the emptied source project row is gone, and usage_rollup (rebuilt
    after the fold) carries the target id only."""
    _alias("projA%", "projB")
    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    with db.viz_conn() as c:
        src_rows = _scalar(c, "SELECT COUNT(*) FROM projects "
                              "WHERE project_id = 'projA'")
        tgt_rows = _scalar(c, "SELECT COUNT(*) FROM projects "
                              "WHERE project_id = 'projB'")
        rolled = dict(c.execute(
            "SELECT project_id, COUNT(*) FROM usage_rollup GROUP BY 1"
        ).fetchall())
    assert _owners() == {"projB": 5}, (
        "every file in the mirror lands on the target id")
    assert src_rows == 0, "the emptied source project row is gone"
    assert tgt_rows == 1, "the target project row exists"
    assert set(rolled) == {"projB"} and sum(rolled.values()) > 0, (
        "usage_rollup was rebuilt after the fold and carries the target id")


def test_alias_added_after_first_ingest_folds_without_a_reparse(
        fresh_db, mini_r2_env):
    """An alias row added AFTER the files landed takes effect on the next
    ingest with no new files: no reparse, no R2 fetch, parser_version
    untouched — the pass re-keys stored identity only."""
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        stored = {r[0] for r in c.execute(
            "SELECT DISTINCT parser_version FROM files").fetchall()}
    assert stored == {constants.PARSER_VERSION}

    _alias("projA%", "projB")
    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    assert result["reparsed"] == 0
    assert result["inserted"] == 0
    with db.viz_conn() as c:
        stored = {r[0] for r in c.execute(
            "SELECT DISTINCT parser_version FROM files").fetchall()}
    assert stored == {constants.PARSER_VERSION}, (
        "folding is not a reparse: the stored parser_version is unchanged")
    assert _owners() == {"projB": 5}


def test_alias_row_deleted_already_folded_rows_keep_the_target(
        fresh_db, mini_r2_env):
    """Deleting the alias stops folding NEW files only: already-folded
    rows keep the target id — the raw id is not retained, so an unfold
    is not possible."""
    _alias("projA%", "projB")
    assert ingest.run_ingest(trigger="manual")["error"] is None
    with db.viz_conn() as c:
        c.execute("DELETE FROM project_aliases")
        c.commit()
    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    assert _owners() == {"projB": 5}


# ---------------------------------------------------------------------------
# /api/projects: the endpoint already hides zero-usage projects (the
# all-time-token inner JOIN); this pins it.
# ---------------------------------------------------------------------------


@pytest.fixture(name="projects_client")
def _projects_client_fixture(fresh_db, mini_r2_env):
    """A TestClient on the api router over a fresh ingest of the mini
    mirror; auth is bypassed by mounting only the router."""
    ingest.run_ingest(trigger="manual")
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


def test_projects_hides_a_project_whose_files_carry_zero_usage_records(
        projects_client):
    """The all-time-token inner JOIN drops a project whose files parsed to
    ZERO usage records — the state a folded-away worktree's source
    project would otherwise present in the picker (issue #272). A project
    with usage stays listed. Uses range=3650d: a cache key no other
    /api/projects test claims."""
    with closing(psycopg.connect(os.environ["DATABASE_URL_VIZ"])) as conn, \
            conn.cursor() as cur:
        cur.execute(
            "INSERT INTO projects (project_id, display_name, first_seen_at, "
            "last_seen_at) VALUES ('projNoRecords', 'projNoRecords', "
            "now(), now())")
        cur.execute(
            "INSERT INTO files (file_key, project_id, session_id, is_main, "
            "r2_etag, r2_size_bytes, r2_last_modified, parsed_at, "
            "parser_version) VALUES "
            "('claude/projNoRecords/sess-E/sess-E.jsonl', 'projNoRecords', "
            "'sess-E', TRUE, 'e', 1, now(), now(), 't')")
        conn.commit()

    r = projects_client.get("/api/projects?range=3650d")
    assert r.status_code == 200
    by_id = {p["project_id"] for p in r.json()["projects"]}
    assert "projA" in by_id, "a project with usage stays listed"
    assert "projNoRecords" not in by_id, (
        "a project with files but zero usage records must be absent — "
        "its picker row would carry no tokens and no cost")
