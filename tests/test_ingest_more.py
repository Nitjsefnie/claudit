"""Tests moved from test_ingest.py to keep test modules under 700 lines."""
from __future__ import annotations

import json
import lzma
import os
import shutil
from contextlib import closing

import psycopg

from backend import cache, constants, db, ingest, lane_projects

# Imported fixtures are discovered from their pytest fixture markers.
from tests.test_ingest import (  # pylint: disable=unused-import
    _FIX_ROOT,
    _fresh_db_fixture,
    _last_run_finished_at,
    _mini_r2_env_fixture,
    _plant,
    _scalar,
    _suppress,
)


def test_purge_suppressed_is_a_noop_with_an_empty_table(fresh_db, mini_r2_env):
    """It runs on every ingest, so the empty case is the common one —
    and this codebase also ships to a deploy that suppresses nothing."""
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        before = _scalar(c, "SELECT COUNT(*) FROM records")
    assert ingest.purge_suppressed() == 0
    with db.viz_conn() as c:
        assert _scalar(c, "SELECT COUNT(*) FROM records") == before


def test_purge_suppressed_is_idempotent(fresh_db, mini_r2_env):
    """Nothing left to delete on the second pass — it runs every ingest."""
    ingest.run_ingest(trigger="manual")
    _suppress("claude-opus-%")
    assert ingest.purge_suppressed() > 0
    assert ingest.purge_suppressed() == 0


def test_parser_version_ignores_the_environment(fresh_db, mini_r2_env,
                                                monkeypatch):
    """PARSER_VERSION is code-owned: setting the old env var must NOT
    trigger a reparse.

    It used to be read from .env, so a parser change shipped without an
    operator editing that file left every stored row on the previous
    semantics with nothing to detect the drift.
    """
    ingest.run_ingest(trigger="manual")
    monkeypatch.setenv("PARSER_VERSION", "999")  # sv-test-data: allow (negative test: the env value is ignored input, not test data)
    result = ingest.run_ingest(trigger="manual")
    assert result["reparsed"] == 0

    with db.viz_conn() as c:
        stored = {r[0] for r in c.execute(
            "SELECT DISTINCT parser_version FROM files").fetchall()}
    assert stored == {constants.PARSER_VERSION}


def test_tool_failure_columns_persist(fresh_db, mini_r2_env):
    """error_kind/error_text survive the ingest INSERT, and only
    errored rows carry them."""
    _plant(mini_r2_env, "projK", "sess-K", "error_kinds.jsonl")
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        rows = c.execute(
            "SELECT idx, is_error, error_kind, error_text FROM tool_uses "
            "WHERE file_key LIKE '%sess-K.jsonl' ORDER BY idx"
        ).fetchall()
    assert [r[2] for r in rows] == ["failed", "rejected", "tool_error", None]
    assert "PreToolUse hook" in rows[0][3]
    assert rows[3][1] is False and rows[3][3] is None


def test_agent_dispatch_columns_persist(fresh_db, mini_r2_env):
    """agent_type/agent_model survive the ingest INSERT and stay NULL
    for tools that dispatch nothing."""
    _plant(mini_r2_env, "projD", "sess-D", "agent_dispatch.jsonl")
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        rows = c.execute(
            "SELECT idx, tool_name, agent_type, agent_model FROM tool_uses "
            "WHERE file_key LIKE '%sess-D.jsonl' ORDER BY idx"
        ).fetchall()
    assert rows[0][1:] == ("Agent", "Explore", "haiku")
    assert rows[1][2] is None and rows[1][3] is None
    assert rows[2][1] == "Bash" and rows[2][2] is None


def test_tool_error_rollup_totals_match_raw(fresh_db, mini_r2_env):
    """SV-ROLLUP: the pre-aggregate must equal the raw aggregate it
    stands in for, per kind."""
    _plant(mini_r2_env, "projK", "sess-K", "error_kinds.jsonl")
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        rolled = dict(c.execute(
            "SELECT error_kind, SUM(n) FROM tool_error_rollup GROUP BY 1"
        ).fetchall())
        raw = dict(c.execute(
            "SELECT error_kind, COUNT(*) FROM tool_uses "
            "WHERE error_kind IS NOT NULL AND ts IS NOT NULL GROUP BY 1"
        ).fetchall())
    assert rolled == raw
    # The literal is the PLANTED fixture's own contribution, read from the
    # raw table scoped to its session: the mirror itself now carries lane
    # wires (issue #503), one of which settles a failed call of its own,
    # so the global tally is the planted three plus that one.
    assert dict(c.execute(
        "SELECT error_kind, COUNT(*) FROM tool_uses "
        "WHERE error_kind IS NOT NULL AND ts IS NOT NULL "
        "AND file_key LIKE '%sess-K%' GROUP BY 1"
    ).fetchall()) == {"failed": 1, "rejected": 1, "tool_error": 1}


def test_dispatch_rollup_totals_match_raw(fresh_db, mini_r2_env):
    """Every dispatch is counted, including one that named no model."""
    _plant(mini_r2_env, "projD", "sess-D", "agent_dispatch.jsonl")
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        rolled = c.execute(
            "SELECT agent_type, agent_model, SUM(n) FROM dispatch_rollup "
            "GROUP BY 1, 2 ORDER BY 1"
        ).fetchall()
        raw_total = _scalar(c, "SELECT COUNT(*) FROM tool_uses "
                               "WHERE agent_type IS NOT NULL")
    assert rolled == [("Explore", "haiku", 1)]
    assert sum(r[2] for r in rolled) == raw_total


def test_dispatch_brief_columns_persist(fresh_db, mini_r2_env):
    """The briefing-shape columns survive the ingest INSERT, and stay
    NULL for a dispatch with no prompt and for non-dispatch tools."""
    _plant(mini_r2_env, "projB", "sess-B", "dispatch_brief_shape.jsonl")
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        rows = c.execute(
            "SELECT idx, tool_name, dispatch_brief_ref, "
            "dispatch_prompt_chars FROM tool_uses "
            "WHERE file_key LIKE '%sess-B.jsonl' ORDER BY idx"
        ).fetchall()
    assert rows[0][2] is True
    assert rows[1][2] is False and rows[1][3] == 188
    assert rows[2][2] is False, "a path past the scan window is not a ref"
    assert rows[3][2] is None and rows[3][3] is None
    assert rows[4][1] == "Bash" and rows[4][2] is None


def test_dispatch_brief_rollup_totals_match_raw(fresh_db, mini_r2_env):
    """SV-ROLLUP: the pre-aggregate equals the raw aggregate it stands
    in for, on both the count and the summed prompt length."""
    _plant(mini_r2_env, "projB", "sess-B", "dispatch_brief_shape.jsonl")
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        rolled = c.execute(
            "SELECT brief_ref, SUM(n), SUM(prompt_chars) "
            "FROM dispatch_brief_rollup GROUP BY 1 ORDER BY 1"
        ).fetchall()
        raw = c.execute(
            "SELECT dispatch_brief_ref, COUNT(*), "
            "COALESCE(SUM(dispatch_prompt_chars), 0) FROM tool_uses "
            "WHERE dispatch_brief_ref IS NOT NULL AND ts IS NOT NULL "
            "GROUP BY 1 ORDER BY 1"
        ).fetchall()
    assert rolled == raw
    # Two inline dispatches, one that delegates to a written brief.
    assert dict((r[0], r[1]) for r in rolled) == {False: 2, True: 1}


def test_lane_layout_ingests_with_marker_display_name(
        fresh_db, tmp_path, monkeypatch):
    """A lane bucket's sessions/ tree: wire.jsonl[.xz] under
    sessions/<project>/<session>/, a project.json marker carrying the
    project's directory, and a subagent wire under subagents/. Main and
    sidecar land under the SAME project, keyed by the Claude slug of the
    marker's path — the id a Claude-layout bucket derives for the same
    directory — the path becomes projects.display_name, and is_main
    splits main from subagent."""
    proj, sess = "8805b8ac99ad", "01a0-uuid"
    slug = "-home-me-lanework"
    bucket = tmp_path / "r2" / "claude"
    lane = bucket / "sessions" / proj / sess
    (lane / "subagents" / "019f-child").mkdir(parents=True)
    (lane / "wire.jsonl.xz").write_bytes(
        lzma.compress((_FIX_ROOT / "parser" / "codex_min.jsonl").read_bytes()))
    shutil.copy(
        _FIX_ROOT / "parser" / "kimi_code_min.jsonl",
        lane / "subagents" / "019f-child" / "wire.jsonl",
    )
    (bucket / "sessions" / proj / "project.json").write_text(
        json.dumps({"path": "/home/me/lanework"}))
    monkeypatch.setenv("R2_ENDPOINT", f"file://{tmp_path}/r2/")

    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    assert result["inserted"] == 2
    assert result["r2_listed"] == 2, "the marker is not a transcript"
    with db.viz_conn() as c:
        rows = c.execute(
            "SELECT file_key, project_id, session_id, is_main FROM files "
            "ORDER BY is_main DESC"
        ).fetchall()
        display = _scalar(
            c, "SELECT display_name FROM projects WHERE project_id = %s",
            (slug,))
        hash_projects = _scalar(
            c, "SELECT COUNT(*) FROM projects WHERE project_id = %s",
            (proj,))
        n_records = _scalar(c, "SELECT COUNT(*) FROM records")
    assert [(r[2], r[3]) for r in rows] == [(sess, True), (sess, False)]
    assert all(r[0].startswith(f"claude/sessions/{proj}/{sess}/") for r in rows)
    assert all(r[1] == slug for r in rows), (
        "a marker path read this run keys the project by its slug")
    assert display == "/home/me/lanework"
    assert hash_projects == 0, (
        "no project row may be keyed by the bare hash when a marker was read")
    assert n_records > 0, "both wire files parsed into records"


def test_lane_layout_without_project_marker_displays_the_project_id(
        fresh_db, tmp_path, monkeypatch):
    """Same lane tree with the sessions/<project>/project.json marker
    ABSENT: ingest still succeeds and the project's display_name falls
    back to the project id, exactly as for a malformed marker."""
    proj, sess = "8805b8ac99ad", "01a0-uuid"
    bucket = tmp_path / "r2" / "claude"
    lane = bucket / "sessions" / proj / sess
    lane.mkdir(parents=True)
    (lane / "wire.jsonl.xz").write_bytes(
        lzma.compress((_FIX_ROOT / "parser" / "codex_min.jsonl").read_bytes()))
    monkeypatch.setenv("R2_ENDPOINT", f"file://{tmp_path}/r2/")

    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    assert result["inserted"] == 1
    assert result["r2_listed"] == 1
    with db.viz_conn() as c:
        display = _scalar(
            c, "SELECT display_name FROM projects WHERE project_id = %s",
            (proj,))
        n_records = _scalar(c, "SELECT COUNT(*) FROM records")
    assert display == proj, "no marker: the project id is the display name"
    assert n_records > 0, "the wire file parsed into records"


def test_transient_marker_failure_keeps_the_stored_display_name(
        fresh_db, tmp_path, monkeypatch):
    """A run that reparses a lane project's files while its project.json
    marker fetch fails (marker gone, GET error) must keep the stored
    display_name AND the slug-keyed project id. Flipping an existing
    slug-keyed project back to its hash for that run splits one project
    into two ids until the next reparse."""
    proj, sess = "8805b8ac99ad", "01a0-uuid"
    slug = "-home-me-lanework"
    bucket = tmp_path / "r2" / "claude"
    lane = bucket / "sessions" / proj / sess
    lane.mkdir(parents=True)
    (lane / "wire.jsonl.xz").write_bytes(
        lzma.compress((_FIX_ROOT / "parser" / "codex_min.jsonl").read_bytes()))
    (bucket / "sessions" / proj / "project.json").write_text(
        json.dumps({"path": "/home/me/lanework"}))
    monkeypatch.setenv("R2_ENDPOINT", f"file://{tmp_path}/r2/")

    ingest.run_ingest(trigger="manual")

    # The marker vanishes and the wire's etag changes: the next run
    # reparses the file having read no marker at all.
    (bucket / "sessions" / proj / "project.json").unlink()
    (lane / "wire.jsonl.xz").touch()
    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    assert result["reparsed"] == 1

    with db.viz_conn() as c:
        display = _scalar(
            c, "SELECT display_name FROM projects WHERE project_id = %s",
            (slug,))
        hash_files = _scalar(
            c, "SELECT COUNT(*) FROM files WHERE project_id = %s", (proj,))
    assert display == "/home/me/lanework", (
        "a run that read no marker must preserve the stored display name"
    )
    assert hash_files == 0, (
        "no file may flip back to the hash id: the stored hash→slug "
        "mapping keeps one directory ONE project across a marker miss"
    )


def test_marker_ok_run_rekeys_a_hash_stalled_by_a_failed_marker(
        fresh_db, tmp_path, monkeypatch):
    """Run 1's marker GET fails (a per-object failure, not fatal): the
    files persist under the hash. Run 2 reads the marker fine but NO
    etag changed, so nothing reparses — the stored rows are re-keyed to
    the slug directly, the orphan hash project row is dropped, and one
    directory is ONE project again. Without the rekey the files sit
    hash-keyed until each etag changes, and the first reparse creates
    TWO project rows for one directory."""
    proj = "8805b8ac99ad"
    slug = "-home-me-lanework"
    bucket = tmp_path / "r2" / "claude"
    lane = bucket / "sessions" / proj / "01a0-uuid"
    lane.mkdir(parents=True)
    (lane / "wire.jsonl.xz").write_bytes(
        lzma.compress((_FIX_ROOT / "parser" / "codex_min.jsonl").read_bytes()))
    (bucket / "sessions" / proj / "project.json").write_text(
        json.dumps({"path": "/home/me/lanework"}))
    monkeypatch.setenv("R2_ENDPOINT", f"file://{tmp_path}/r2/")

    # Run 1: the marker GET fails after retries - a per-object failure.
    real_fetch = ingest._fetch_with_retry  # pylint: disable=protected-access

    def failing_fetch(key: str) -> bytes:
        if key.endswith("project.json"):
            raise RuntimeError("marker GET dropped")
        return real_fetch(key)

    monkeypatch.setattr(ingest, "_fetch_with_retry", failing_fetch)
    r1 = ingest.run_ingest(trigger="manual")
    # Restore the fetch (calling undo() here would also wipe this
    # test's R2_ENDPOINT); teardown handles the rest.
    monkeypatch.setattr(ingest, "_fetch_with_retry", real_fetch)
    assert r1["error"] is not None
    assert r1["failed"] == 1
    with db.viz_conn() as c:
        pids = [r[0] for r in c.execute(
            "SELECT DISTINCT project_id FROM files")]
    assert pids == [proj], "a marker-failed run keeps the hash"

    # Run 2: the marker reads fine and NOTHING was touched - no etag
    # change, no reparse.
    r2res = ingest.run_ingest(trigger="manual")
    assert r2res["error"] is None
    assert r2res["reparsed"] == 0
    with db.viz_conn() as c:
        pids = [r[0] for r in c.execute(
            "SELECT DISTINCT project_id FROM files")]
        display = _scalar(
            c, "SELECT display_name FROM projects WHERE project_id = %s",
            (slug,))
        hash_rows = _scalar(
            c, "SELECT COUNT(*) FROM projects WHERE project_id = %s",
            (proj,))
    assert pids == [slug], (
        "a marker-OK run re-keys the stalled files onto the slug even "
        "with nothing reparsed")
    assert display == "/home/me/lanework"
    assert hash_rows == 0, "the orphan hash project row must be gone"


def test_stored_lane_mapping_prefers_slug_then_the_larger_side(
        fresh_db, monkeypatch):
    """If a crash ever left one hash's files split across two ids, the
    stored hash->project mapping must resolve deterministically: prefer
    the slug form, else the id holding more files, ties by id. The
    hash-keyed side's rows are inserted FIRST (and hold more files), so
    the old setdefault would have picked it on insertion order."""
    with closing(psycopg.connect(os.environ["DATABASE_URL_VIZ"])) as conn, \
            conn.cursor() as cur:
        for pid in ("8805b8ac99ad", "not-a-slug", "zzz-hash", "aaa-hash"):
            cur.execute(
                "INSERT INTO projects (project_id, display_name, "
                "first_seen_at, last_seen_at) VALUES (%s, %s, now(), now())",
                (pid, pid))
        for pid, keys in (
            ("8805b8ac99ad", ("claude/sessions/hx/s1/wire.jsonl",)),
            ("not-a-slug", ("claude/sessions/hx/s2/wire.jsonl",
                            "claude/sessions/hx/s3/wire.jsonl")),
            ("zzz-hash", ("claude/sessions/hy/s9/wire.jsonl",)),
            ("aaa-hash", ("claude/sessions/hy/sa/wire.jsonl",
                          "claude/sessions/hy/sb/wire.jsonl")),
        ):
            for k in keys:
                cur.execute(
                    "INSERT INTO files (file_key, project_id, session_id, "
                    "is_main, r2_etag, r2_size_bytes, r2_last_modified, "
                    "parsed_at, parser_version) "
                    "VALUES (%s, %s, 's', TRUE, 'e', 1, now(), now(), 't')",
                    (k, pid))
        conn.commit()

    mapping = lane_projects.stored_lane_ids()
    # hx: the slug form wins over the larger hash-keyed side. hy: both
    # sides are non-slug, so the id holding more files wins; a count tie
    # would fall to the lexicographically smaller id.
    assert mapping["hx"] == "not-a-slug"
    assert mapping["hy"] == "aaa-hash"


def test_finished_at_lands_after_the_derived_state_rebuild(
        fresh_db, mini_r2_env, monkeypatch):
    """A reader that waits on ingest_runs.finished_at and then reads a
    rollup must not land on the PREVIOUS run's aggregates — the rebuild
    has to run while finished_at is still NULL."""
    real = ingest._rebuild_derived_state  # pylint: disable=protected-access
    seen = {}

    def probe():
        seen["finished_at"] = _last_run_finished_at()
        return real()

    monkeypatch.setattr(ingest, "_rebuild_derived_state", probe)
    summary = ingest.run_ingest_locked("manual")

    assert summary["error"] is None
    assert seen["finished_at"] is None, (
        "the rebuild ran on a run already marked finished")
    assert _last_run_finished_at() is not None, (
        "run_ingest_locked must return with finished_at set")


def test_a_failed_rebuild_still_closes_the_run(fresh_db, mini_r2_env,
                                               monkeypatch):
    """A derived-state rebuild that raises is more severe than any
    per-object failure summary, so it becomes the run's error — and the
    run still gets its finished_at. Being fatal, it also gates the
    ingest_done broadcast and the response-cache invalidation, exactly
    like a walk-level fatal does."""
    def boom():
        raise RuntimeError("rollup rebuild exploded")

    monkeypatch.setattr(ingest, "_rebuild_derived_state", boom)
    broadcasts = []
    monkeypatch.setattr(ingest.events, "broadcast_threadsafe",
                        lambda *args, **kwargs: broadcasts.append(args))
    cache.response_cache.put("rebuild-fatal-key", {"v": "old"})

    summary = ingest.run_ingest_locked("manual")

    assert summary["error"] == "RuntimeError: details are in the server log"
    assert _last_run_finished_at() is not None, (
        "a failed rebuild must still close the run")
    assert not broadcasts, "a fatal run must not broadcast ingest_done"
    assert cache.response_cache.get_entry("rebuild-fatal-key") == (
        {"v": "old"}, False), "a fatal run must not mark responses stale"


def test_state_reset_forces_full_rebuild(fresh_db, mini_r2_env):
    """Clearing the state row repairs derived data without reparsing files."""
    _plant(mini_r2_env, "projK", "sess-K", "error_kinds.jsonl")
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        c.execute("DELETE FROM tool_error_rollup")
        c.execute("DELETE FROM ingest_derived_state")
        c.commit()
    result = ingest.run_ingest(trigger="manual")
    assert result["reparsed"] == 0
    with db.viz_conn() as c:
        assert _scalar(c, "SELECT COUNT(*) FROM tool_error_rollup") > 0
