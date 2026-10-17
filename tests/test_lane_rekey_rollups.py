"""A lane re-key that deletes the source project leaves the rollups right.

The rekey UPDATEs a stale hash project's files onto the folded marker
project and DELETEs the hash project's row once no file references it.
That DELETE cascades: sessions and every
proj_id-keyed rollup row go with it. The moved files' contributions must
reappear under the TARGET project, which only happens if the run's
rebuild sees the move — ingest promotes the scope to full when a rekey
moved files, and it has to: the files' etags and parser version did not
change, so no file is reparsed and an incremental run captures no dirty
hour at all, while the source project's rows have already died with the
cascade.

So this pins the end state of a marker-arrival run: no orphan rows, no
source project, and the target's rollups equal both what the source
carried before the move and what the records themselves say.
"""
from __future__ import annotations

import json
import lzma
from datetime import datetime, timezone

import pytest

# The fixtures register on import; pylint only sees names nobody calls.
from test_ingest import (  # pylint: disable=unused-import
    _FIX_ROOT, _fresh_db_fixture, _scalar,
)

from backend import db, ingest, key_layout, lane_projects
from tests import scratch_db

_MARKER = "project.json"
_HASH = "aaaa1111"
_PATH = "/home/me/alpha"

# Every rollup keyed by proj_id (the FK list, plus sessions' own).
_ROLLUPS = ("usage_rollup", "tool_rollup", "tool_error_rollup",
            "dispatch_rollup", "dispatch_brief_rollup", "ctx_cost_rollup",
            "agent_rollup", "latency_rollup")


def _lane(bucket, proj: str, marker: str | None = None) -> None:
    lane = bucket / "sessions" / proj / "s-1"
    lane.mkdir(parents=True)
    (lane / "wire.jsonl.xz").write_bytes(
        lzma.compress((_FIX_ROOT / "parser" / "codex_min.jsonl").read_bytes()))
    if marker is not None:
        (bucket / "sessions" / proj / _MARKER).write_text(marker)


def _usage_totals(conn, project_id: str) -> tuple:
    row = conn.execute(
        "SELECT COALESCE(SUM(u.requests), 0), "
        "       COALESCE(SUM(u.fresh_tokens), 0), "
        "       COALESCE(SUM(u.output_tokens), 0), "
        "       COALESCE(SUM(u.cost_usd), 0) "
        "  FROM usage_rollup u "
        "  JOIN projects p ON p.proj_id = u.proj_id "
        " WHERE p.project_id = %s", (project_id,)).fetchone()
    assert row is not None
    return tuple(float(v) for v in row)


def _record_totals(conn, project_id: str) -> tuple:
    """What the raw facts say for one project id, in usage_rollup's
    terms: canonical rows with a timestamp (the rebuild's own filter)."""
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(r.fresh_tokens), 0), "
        "       COALESCE(SUM(r.output_tokens), 0), "
        "       COALESCE(SUM(rp.cost_usd), 0) "
        "  FROM records r "
        "  JOIN files f ON f.file_id = r.file_id "
        "  JOIN records_price rp ON rp.file_id = r.file_id "
        "                       AND rp.line_num = r.line_num "
        " WHERE f.project_id = %s AND r.is_canonical AND r.ts IS NOT NULL",
        (project_id,)).fetchone()
    assert row is not None
    return tuple(float(v) for v in row)


def _rollup_rows(conn, project_id: str) -> dict[str, int]:
    counts = {}
    for table in _ROLLUPS:
        counts[table] = _scalar(
            conn,
            f"SELECT COUNT(*) FROM {table} t "  # noqa: S608
            "JOIN projects p ON p.proj_id = t.proj_id "
            "WHERE p.project_id = %s",
            (project_id,),
        )
    return counts


def _orphans(conn) -> dict[str, int]:
    """Rows pointing at no project — what a missed cascade would leave."""
    found = {}
    for table in _ROLLUPS + ("sessions",):
        found[table] = _scalar(
            conn,
            f"SELECT COUNT(*) FROM {table} t "  # noqa: S608
            "LEFT JOIN projects p ON p.proj_id = t.proj_id "
            "WHERE p.proj_id IS NULL",
        )
    return found


@pytest.mark.db
def test_a_rekey_that_deletes_the_source_project_rebuilds_the_target(
        fresh_db, tmp_path, monkeypatch):
    bucket = tmp_path / "r2" / "claude"
    _lane(bucket, _HASH)  # no marker yet: the run stores it hash-keyed
    monkeypatch.setenv("CLAUDIT_R2_ENDPOINT", f"file://{tmp_path}/r2/")
    slug = key_layout.canonical_project_id(key_layout.project_slug(_PATH))

    first = ingest.run_ingest(trigger="manual")
    assert first["error"] is None
    with db.viz_conn() as conn:
        assert _scalar(conn, "SELECT COUNT(*) FROM files "
                             "WHERE project_id = %s", (_HASH,)) == 1
        source_usage = _usage_totals(conn, _HASH)
        source_rows = _rollup_rows(conn, _HASH)
        assert source_usage[0] > 0, "the source project must carry usage"
        assert source_rows["usage_rollup"] > 0
        assert all(count == 0 for count in _orphans(conn).values())
        # The rekey's own projects insert is an allocation site: this
        # run must spend exactly the one value its target row takes.
        proj_seq_before = scratch_db.seq_used(conn, "projects_proj_id_seq")

    # The marker arrives: the stored mapping is stale, so this run rekeys
    # the files onto the slug and drops the hash project's row.
    (bucket / "sessions" / _HASH / _MARKER).write_text(
        json.dumps({"path": _PATH}))
    second = ingest.run_ingest(trigger="manual")
    assert second["error"] is None
    assert second["reparsed"] == 0, (
        "no reparse: an incremental rebuild would capture no dirty hour, "
        "so the promote-to-full on a rekey is what restores the target's "
        "rows")

    with db.viz_conn() as conn:
        assert _scalar(conn, "SELECT COUNT(*) FROM projects "
                             "WHERE project_id = %s", (_HASH,)) == 0
        assert _scalar(conn, "SELECT COUNT(*) FROM projects "
                             "WHERE project_id = %s", (slug,)) == 1
        assert scratch_db.seq_used(conn, "projects_proj_id_seq") - proj_seq_before == 1, (
            "the rekey inserted one target row and must have spent "
            "exactly one proj_id")
        assert _scalar(conn, "SELECT COUNT(*) FROM files "
                             "WHERE project_id = %s", (slug,)) == 1
        # Nothing survived the cascade under an id that no longer exists.
        assert all(count == 0 for count in _orphans(conn).values()), (
            _orphans(conn))
        # The target carries exactly what the source carried before the
        # move, row for row, and the records agree with it.
        target_rows = _rollup_rows(conn, slug)
        assert target_rows == source_rows, (source_rows, target_rows)
        assert _usage_totals(conn, slug) == source_usage
        assert _usage_totals(conn, slug) == _record_totals(conn, slug)


@pytest.mark.db
def test_rekey_onto_a_preexisting_target_spends_no_project_id(fresh_db):
    """The rekey's projects insert is an allocation site: a target the
    store already holds costs no proj_id. The end-to-end run above
    cannot reach this case — there its target is new — so the conflict
    path gets its own assertion."""
    when = datetime(2026, 9, 22, tzinfo=timezone.utc)
    slug = key_layout.canonical_project_id(key_layout.project_slug(_PATH))
    with db.viz_conn() as conn:
        scratch_db.seed_projects_with_file(
            conn, (_HASH, slug),
            f"claude/sessions/{_HASH}/s-1/wire.jsonl", "s-1", when)
        conn.commit()
        before = scratch_db.seq_used(conn, "projects_proj_id_seq")

    moved = lane_projects.rekey_stale_lane_projects(
        {_HASH: _PATH}, {_HASH: _HASH})
    assert moved == 1

    with db.viz_conn() as conn:
        assert scratch_db.seq_used(conn, "projects_proj_id_seq") == before, (
            "re-keying onto a stored target burned a proj_id")
        assert _scalar(conn, "SELECT COUNT(*) FROM files "
                             "WHERE project_id = %s", (slug,)) == 1
        assert _scalar(conn, "SELECT COUNT(*) FROM projects "
                             "WHERE project_id = %s", (_HASH,)) == 0
