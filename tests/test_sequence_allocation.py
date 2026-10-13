"""ID allocation must spend a sequence value only on a row it inserts.

PostgreSQL evaluates a DEFAULT nextval while forming the tuple — BEFORE
the unique index decides the row conflicts — so `INSERT ... ON CONFLICT`
attempted for a value that is already stored burns the id anyway, and
`DO UPDATE` additionally locks the row it did not need to touch. At the
#959 shape every id is a surrogate (dim ids, proj_id, file_id, ses_id),
so a per-attempt burn is what makes a SMALLINT id space (32767) run out
and what queues the four persist threads behind one hot row. Every
allocation site therefore reads the existing rows first, inserts only
what is missing with ON CONFLICT DO NOTHING, and reads the ids back.

Each test below drives ONE site twice over the SAME values and asserts
the sequence advanced by the number of DISTINCT values only.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

# The fixture registers on import; pylint only sees names nobody calls.
from test_ingest import (  # pylint: disable=unused-import
    _fresh_db_fixture, _scalar,
)

from backend import db, dims, ingest, ingest_reprice, parser_version
from backend import ingest_rollup_hourly, parse, project_aliases
from backend.r2 import R2Object
from tests import scratch_db
from tests.reprice_fixture_builders import seed_record

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "parser"

_DIM_TABLES = ("dim_model", "dim_provider", "dim_tool_name",
               "dim_agent_type", "dim_agent_model", "dim_error_kind",
               "dim_stop_reason", "dim_effort", "dim_rate_fingerprint",
               "dim_pricing_version", "dim_cli_version")
_IDENTITY_SEQUENCES = ("projects_proj_id_seq", "files_file_id_seq",
                       "sessions_ses_id_seq")
_FILE_KEY = "claude/seq-test/sess-a/sess-a.jsonl"


def _snapshot(conn, sequences) -> dict[str, int]:
    return {seq: scratch_db.seq_used(conn, seq) for seq in sequences}


def _growth(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    return {seq: after[seq] - before[seq] for seq in after}


@pytest.mark.db
def test_dim_ids_spends_one_value_per_distinct_value(fresh_db):
    """Every dim table, repeatedly resolved: two values then one more."""
    sequences = {table: f"{table}_id_seq" for table in _DIM_TABLES}
    with db.viz_conn() as conn:
        before = _snapshot(conn, sequences.values())
        first: dict[str, dict[str, int]] = {}
        for table in _DIM_TABLES:
            first[table] = dims.dim_ids(conn, table,
                                        ["alpha", "beta", "alpha", None])
            again = dims.dim_ids(conn, table, ["alpha", "beta"])
            assert again == first[table], table
            single = dims.dim_id(conn, table, "gamma")
            assert single == dims.dim_id(conn, table, "gamma"), table
        conn.commit()
        after = _snapshot(conn, sequences.values())

    for table in _DIM_TABLES:
        spent = _growth(before, after)[sequences[table]]
        assert spent == 3, f"{table} spent {spent} of 3 possible values"


@pytest.mark.db
def test_persisting_the_same_file_twice_spends_no_identity_value(
        fresh_db):
    """projects.proj_id, files.file_id and every dim id: one persist
    allocates them, the second identical persist allocates nothing."""
    key = "sessions/p/s/wire.jsonl"
    out = parse.parse_file(key, (FIX / "codex_min.jsonl").read_bytes())
    assert out["records"], "the fixture must carry usage records"
    when = datetime(2026, 9, 22, tzinfo=timezone.utc)
    proj = {"project_id": "p", "display_name": "p",
            "first_seen_at": when, "last_seen_at": when}
    obj = R2Object(key=key, etag="etag", size=1024, last_modified=when)
    sequences = [f"{table}_id_seq" for table in _DIM_TABLES]
    sequences += ["projects_proj_id_seq", "files_file_id_seq"]

    with db.viz_conn() as conn:
        before = _snapshot(conn, sequences)
    ingest._persist(  # pylint: disable=protected-access
        obj, proj, out, parser_version.PARSER_VERSION)
    with db.viz_conn() as conn:
        once = _snapshot(conn, sequences)
    ingest._persist(  # pylint: disable=protected-access
        obj, proj, out, parser_version.PARSER_VERSION)
    with db.viz_conn() as conn:
        twice = _snapshot(conn, sequences)

    assert once["projects_proj_id_seq"] - before["projects_proj_id_seq"] == 1
    assert once["files_file_id_seq"] - before["files_file_id_seq"] == 1
    spent_twice = _growth(once, twice)
    assert spent_twice == {seq: 0 for seq in sequences}, (
        f"a repeat persist burned ids: {spent_twice}")


@pytest.mark.db
def test_ensure_sessions_and_agent_dims_spend_once_per_distinct_value(
        fresh_db):
    """The rollup rebuild's own allocations: called twice over the same
    files, both sequences move only for rows they actually insert."""
    when = datetime(2026, 9, 22, tzinfo=timezone.utc)
    with db.viz_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO projects (project_id, display_name, "
            "first_seen_at, last_seen_at) VALUES ('p', 'p', %s, %s)",
            (when, when))
        for idx, (session, agent) in enumerate(
                (("s-1", "general-purpose"), ("s-2", "explore"))):
            cur.execute(
                "INSERT INTO files (file_key, project_id, session_id, "
                "is_main, r2_etag, r2_size_bytes, r2_last_modified, "
                "parsed_at, parser_version, agent_type) "
                "VALUES (%s, 'p', %s, TRUE, 'e', 1, %s, %s, '0', %s)",
                (f"claude/seq/sess-{idx}/wire.jsonl", session, when, when,
                 agent))
        conn.commit()

    sequences = ["sessions_ses_id_seq", "dim_agent_type_id_seq"]
    with db.viz_conn() as conn:
        before = _snapshot(conn, sequences)
    ingest_rollup_hourly.ensure_sessions()
    ingest_rollup_hourly.ensure_agent_type_dims()
    with db.viz_conn() as conn:
        once = _snapshot(conn, sequences)
    ingest_rollup_hourly.ensure_sessions()
    ingest_rollup_hourly.ensure_agent_type_dims()
    with db.viz_conn() as conn:
        twice = _snapshot(conn, sequences)

    assert (once["sessions_ses_id_seq"]
            - before["sessions_ses_id_seq"]) == 2
    assert (once["dim_agent_type_id_seq"]
            - before["dim_agent_type_id_seq"]) == 2
    assert _growth(once, twice) == {seq: 0 for seq in sequences}, (
        f"a repeat rebuild burned ids: {_growth(once, twice)}")


@pytest.mark.db
def test_reprice_spends_no_value_when_it_sees_the_same_stale_rows(
        fresh_db):
    """The reprice pass's dim allocations go through the same
    select-first path: re-stamping the same rows must not move a
    sequence."""
    sequences = ["dim_pricing_version_id_seq", "dim_rate_fingerprint_id_seq"]
    with db.viz_conn() as conn:
        seed_record(conn, _FILE_KEY, 1)
        conn.commit()
        before = _snapshot(conn, sequences)

    assert ingest_reprice.reprice_stale() == 1
    with db.viz_conn() as conn:
        once = _snapshot(conn, sequences)
    assert once["dim_pricing_version_id_seq"] > before[
        "dim_pricing_version_id_seq"], "the first run must stamp a version"

    # The same rows, stale again, under the same values: the stored cost
    # is wrong too, so this run REPRICES them (its return value counts
    # repriced rows; a restamp-only row would still allocate the ids).
    with db.viz_conn() as conn:
        conn.execute(
            "UPDATE records_price SET pricing_version_id = NULL, "
            "rate_fingerprint_id = NULL, cost_usd = 0")
        conn.commit()
    assert ingest_reprice.reprice_stale() == 1
    with db.viz_conn() as conn:
        twice = _snapshot(conn, sequences)

    assert _growth(once, twice) == {seq: 0 for seq in sequences}, (
        f"re-stamping the same rows burned ids: {_growth(once, twice)}")


@pytest.mark.db
def test_alias_fold_spend_no_value_when_the_target_project_exists(
        fresh_db):
    """The alias fold's target upsert is an allocation site too.

    `folded_pairs` returns every move whose source still has files, so
    this INSERT is attempted on every alias move — and the common case is
    a target `projects` row the store already holds, where the DEFAULT
    proj_id would fire before ON CONFLICT rejects the row.
    """
    when = datetime(2026, 9, 22, tzinfo=timezone.utc)
    with db.viz_conn() as conn:
        scratch_db.seed_projects_with_file(
            conn, ("alias-src", "alias-tgt"),
            "claude/alias-fold/s-1/wire.jsonl", "s-1", when,
            alias="alias-src")
        conn.commit()
        before = scratch_db.seq_used(conn, "projects_proj_id_seq")

    assert project_aliases.rekey_folded_projects() >= 1
    with db.viz_conn() as conn:
        once = scratch_db.seq_used(conn, "projects_proj_id_seq")
        # The move itself is the point of the pass, id spend or not.
        at_target = _scalar(
            conn, "SELECT COUNT(*) FROM files "
                  "WHERE project_id = 'alias-tgt'")
        source_left = _scalar(
            conn, "SELECT COUNT(*) FROM projects "
                  "WHERE project_id = 'alias-src'")
    assert at_target == 1 and source_left == 0, (at_target, source_left)
    assert once == before, (
        f"folding onto a stored target burned {once - before} project id(s)")

    # A repeat pass finds nothing to move and still spends nothing.
    project_aliases.rekey_folded_projects()
    with db.viz_conn() as conn:
        twice = scratch_db.seq_used(conn, "projects_proj_id_seq")
    assert twice == before, f"a repeat fold burned {twice - before} id(s)"
