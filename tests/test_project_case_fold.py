"""Project identity across Windows path case: the ingest-level shape.

key_layout's unit tests pin the fold itself; these tests pin what the
run does with it — two Claude-layout slugs of one Windows directory
becoming ONE project with a readable display_name, a pre-53 mixed-case
project being re-keyed with no orphan row left behind, and a lane
marker naming a Windows path meeting a Claude-layout key of the same
directory on one id.
"""
import json
import lzma
import os
from contextlib import closing
from pathlib import Path

import psycopg
import pytest

from backend import db, ingest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_FIX_ROOT = _REPO_ROOT / "fixtures"


def _scalar(cur, sql: str, params=None):
    """First column of the first row; the query must yield one."""
    row = (cur.execute(sql, params) if params is not None
           else cur.execute(sql)).fetchone()
    assert row is not None, f"expected a row: {sql[:80]}"
    return row[0]


def _plant(mirror: Path, project: str, session: str, fixture: str) -> None:
    """Copy a parser fixture into the mirror as a Claude-layout session."""
    dest = mirror / project / session
    dest.mkdir(parents=True, exist_ok=True)
    (dest / f"{session}.jsonl").write_bytes(
        (_FIX_ROOT / "parser" / fixture).read_bytes())


@pytest.fixture(name="fresh_db")
def _fresh_db_fixture(monkeypatch):
    """Per-test schema reset on a separate DB (as in test_ingest.py)."""
    test_db = "claudit_test"
    os.system(f"dropdb --if-exists {test_db} 2>/dev/null")
    os.system(f"createdb {test_db} 2>/dev/null")
    os.system(f"psql {test_db} -f {_REPO_ROOT / 'backend/schema.sql'} >/dev/null")
    monkeypatch.setenv("DATABASE_URL_VIZ", f"postgresql:///{test_db}")
    db.reset_viz_pool()
    yield
    db.reset_viz_pool()
    os.system(f"dropdb --if-exists {test_db} 2>/dev/null")


def test_windows_case_variants_are_one_project(fresh_db, tmp_path,
                                               monkeypatch):
    """Two Claude-layout slugs of one Windows directory — cased as the
    shell happened to case each session — must end as ONE project row
    holding both files, displayed by an original-case slug (the side
    holding the most files; ties by first seen), never the folded id."""
    bucket = tmp_path / "r2" / "claude"
    _plant(bucket, "C--Users-M-kvalita", "s-up-a", "error_kinds.jsonl")
    _plant(bucket, "C--Users-M-kvalita", "s-up-b", "error_kinds.jsonl")
    _plant(bucket, "c--users-m-kvalita", "s-low", "error_kinds.jsonl")
    monkeypatch.setenv("R2_ENDPOINT", f"file://{tmp_path}/r2/")

    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    assert result["inserted"] == 3
    with db.viz_conn() as c:
        pids = [r[0] for r in c.execute(
            "SELECT DISTINCT project_id FROM files ORDER BY 1")]
        projects = c.execute(
            "SELECT project_id, display_name FROM projects").fetchall()
    assert pids == ["c--users-m-kvalita"]
    assert projects == [
        ("c--users-m-kvalita", "C--Users-M-kvalita"),
    ], "one project, displayed by the original-case slug with the most files"


def test_pre53_mixed_case_project_rekeys_without_orphan(fresh_db, tmp_path,
                                                        monkeypatch):
    """A DB written by parser version 52 holds the project under the
    mixed-case slug. The migration run reparses every file, re-keys the
    stored rows onto the folded id, and leaves no orphan project row;
    the display_name stays the cased form that was stored."""
    bucket = tmp_path / "r2" / "claude"
    _plant(bucket, "C--Users-M-kvalita", "s-up-a", "error_kinds.jsonl")
    _plant(bucket, "C--Users-M-kvalita", "s-up-b", "error_kinds.jsonl")
    _plant(bucket, "c--users-m-kvalita", "s-low", "error_kinds.jsonl")
    monkeypatch.setenv("R2_ENDPOINT", f"file://{tmp_path}/r2/")

    with closing(psycopg.connect(os.environ["DATABASE_URL_VIZ"])) as conn, \
            conn.cursor() as cur:
        cur.execute(
            "INSERT INTO projects (project_id, display_name, "
            "first_seen_at, last_seen_at) "
            "VALUES ('C--Users-M-kvalita', 'C--Users-M-kvalita', "
            "now(), now())")
        cur.execute(
            "INSERT INTO files (file_key, project_id, session_id, is_main, "
            "r2_etag, r2_size_bytes, r2_last_modified, parsed_at, "
            "parser_version) VALUES "
            "('claude/C--Users-M-kvalita/s-up-a/s-up-a.jsonl', "
            "'C--Users-M-kvalita', 's-up-a', TRUE, 'old-etag', 1, now(), "
            "now(), '52')")
        conn.commit()

    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    assert result["reparsed"] == 1, "the seeded pre-53 file reparses"
    assert result["inserted"] == 2, "the two unseen files insert"
    with db.viz_conn() as c:
        pids = [r[0] for r in c.execute(
            "SELECT DISTINCT project_id FROM files")]
        projects = c.execute(
            "SELECT project_id, display_name FROM projects").fetchall()
    assert pids == ["c--users-m-kvalita"], (
        "every file, stored and new, is re-keyed onto the folded id")
    assert projects == [
        ("c--users-m-kvalita", "C--Users-M-kvalita"),
    ], "the mixed-case project row is gone and the cased display survives"


def test_lane_marker_windows_path_meets_the_claude_project(
        fresh_db, tmp_path, monkeypatch):
    """A lane marker naming 'C:\\Users\\Z\\Repo' and a Claude-layout key
    of the same directory land under ONE project, displayed by the
    marker path."""
    bucket = tmp_path / "r2" / "claude"
    proj, sess = "8805b8ac99ad", "01a0-uuid"
    lane = bucket / "sessions" / proj / sess
    lane.mkdir(parents=True)
    (lane / "wire.jsonl.xz").write_bytes(lzma.compress(
        (_FIX_ROOT / "parser" / "codex_min.jsonl").read_bytes()))
    (bucket / "sessions" / proj / "project.json").write_text(
        json.dumps({"path": "C:\\Users\\Z\\Repo"}))
    _plant(bucket, "c--users-z-repo", "sess", "error_kinds.jsonl")
    monkeypatch.setenv("R2_ENDPOINT", f"file://{tmp_path}/r2/")

    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    assert result["inserted"] == 2
    with db.viz_conn() as c:
        pids = [r[0] for r in c.execute(
            "SELECT DISTINCT project_id FROM files")]
        display = _scalar(
            c, "SELECT display_name FROM projects WHERE project_id = %s",
            ("c--users-z-repo",))
    assert pids == ["c--users-z-repo"]
    assert display == "C:\\Users\\Z\\Repo"


def test_posix_case_variants_stay_two_projects(fresh_db, tmp_path,
                                               monkeypatch):
    """Linux/macOS paths are case-sensitive: two slugs differing only in
    case are TWO projects with their own files."""
    bucket = tmp_path / "r2" / "claude"
    _plant(bucket, "-root-Claudit", "s1", "error_kinds.jsonl")
    _plant(bucket, "-root-claudit", "s2", "error_kinds.jsonl")
    monkeypatch.setenv("R2_ENDPOINT", f"file://{tmp_path}/r2/")

    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    with db.viz_conn() as c:
        projects = c.execute(
            "SELECT project_id, display_name FROM projects").fetchall()
    # Sorted in Python, never ORDER BY: the two rows differ only in
    # case, and a database collation may rank them either way — the
    # test is about WHICH rows exist, not their stored order.
    assert sorted(projects) == [
        ("-root-Claudit", "-root-Claudit"),
        ("-root-claudit", "-root-claudit"),
    ], "no folding on POSIX slugs"


def test_display_name_keeps_the_most_filed_original_case():
    """With no marker path, the project's display_name is the original
    (pre-fold) slug that holds the most files; ties by first seen. The
    flag stays False — slug-derived displays never clobber a stored one
    directly; the upsert's repair branch upgrades a stored display that
    is the bare folded id."""
    seen: dict = {}
    track = ingest._track_project  # pylint: disable=protected-access
    ts = "2026-09-23T00:00:00Z"
    track(seen, "c--users-m-kvalita", ts, None, "c--users-m-kvalita")
    track(seen, "c--users-m-kvalita", ts, None, "C--Users-M-kvalita")
    track(seen, "c--users-m-kvalita", ts, None, "C--Users-M-kvalita")
    proj = seen["c--users-m-kvalita"]
    assert proj["display_name"] == "C--Users-M-kvalita"
    assert proj["display_name_set"] is False


def test_display_name_stays_the_id_when_only_it_is_known():
    """A project whose every walked file carries the folded form itself
    has no better-cased display to offer — and must not claim one."""
    seen: dict = {}
    track = ingest._track_project  # pylint: disable=protected-access
    ts = "2026-09-23T00:00:00Z"
    track(seen, "c--users-m-kvalita", ts, None, "c--users-m-kvalita")
    track(seen, "c--users-m-kvalita", ts, None, "c--users-m-kvalita")
    proj = seen["c--users-m-kvalita"]
    assert proj["display_name"] == "c--users-m-kvalita"
    assert proj["display_name_set"] is False, (
        "never overwrite a stored display with the bare folded id")
