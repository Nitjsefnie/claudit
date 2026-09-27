"""The project_aliases fold pass: resolution rules and stored-state re-key.

One behaviour per test, against a scratch DB seeded the way ingest
leaves state: one `projects` row per id, one `files` row per file. The
rules pinned here are the table's comment block in schema.sql and the
fold pass in backend/project_aliases.py (issue #272).
"""
from __future__ import annotations

import psycopg
import pytest

from backend import db, project_aliases
from tests import scratch_db


@pytest.fixture(name="fresh_db")
def _fresh_db_fixture(monkeypatch):
    yield from scratch_db.scratch_viz_database(monkeypatch, "aliases")


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
    _alias("-tmp-x-9%", "zzz-late")
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


def test_fold_does_not_chain_through_a_matching_target(fresh_db):
    """'keep-b' is both a target (of '-tmp-a-%') and itself aliased (by
    'keep%'): only the rows stored under 'keep-b' BEFORE the pass move
    on to 'final'. The rows folded onto it stop there — each stored
    project id is resolved exactly once, against the pre-fold id set."""
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


def test_fold_is_idempotent_second_pass_moves_zero(fresh_db):
    """The pass runs on every ingest, so the steady state is the common
    one: the already-folded ids resolve to no move."""
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
