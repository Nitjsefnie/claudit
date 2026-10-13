"""Storage widths: SMALLINT dimensions, INTEGER keys, widest-first order.

Issue #959 task 7. Measured on live pg_stats, a BIGINT surrogate is
WIDER than the text it replaced for every low-cardinality dimension
(tool_name avg 7 B, effort 4 B, stop_reason 9 B), so the dimension ids
and their FK columns store SMALLINT, the file/project/session
surrogates INTEGER (2.1B ceiling), and records/tool_uses DECLARE their
columns widest-first — 8-byte, then 4-byte, then 2-byte, then variable
— so alignment padding cannot eat the saving.

Also pinned here: cli_version is a SMALLINT dimension (dim_cli_version,
NULL = absent), empty read_targets/write_targets arrays store as NULL,
and read_kind — two-valued in the parser by construction ('whole' /
'slice' / None) — stores as BOOLEAN, TRUE = a whole-file read.

Every shape assertion runs against the APPLIED schema in both states
(fresh database, pre-#959 database migrated by the chain) and the two
end states must agree; tests/test_schema_attnums.py pins the catalog
count invariant beside it.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pytest

# The fixture registers on import; the tests below take it as `fresh_db`.
from test_ingest import (  # pylint: disable=unused-import
    _fresh_db_fixture,  # noqa: F401
)

from backend import db, ingest, parse
from backend.r2 import R2Object
from tests import scratch_db

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "parser"

# The canonical LIVE column order (pg_attribute.attnum, dropped columns
# excluded) of records and tool_uses at rest: the 16-byte uuid split
# column leads (typalign 'c', so it needs no padding and its 16 bytes
# keep ts 8-aligned behind it), then 8-byte aligned (timestamptz), then
# fixed 4-byte integers — the PK parts and seq, the uuid's counter slot
# — then 2-byte dimension ids, then variable-length 4-aligned values
# (text, numeric, arrays), then 1-byte booleans last — a boolean needs
# no alignment and nothing follows it, so placing it earlier could only
# push later columns off their alignment. (records' mid-chain form
# additionally carries the reprice columns schema_records_price.sql
# moves onto the sidecar; those keep their declared positions and vanish
# from the live set.)
RECORDS_ORDER = (
    "uuid_col",                             # uuid (16, typalign c)
    "ts",                                   # timestamptz (8)
    "file_id", "line_num",                  # int4 (4) — PK parts
    "seq",                                  # int4 (4) — <uuid>:<n> counter
    "fresh_tokens", "cache_creation_tokens", "cache_read_tokens",
    "cache_write_tokens", "output_tokens", "eph5_tokens", "eph1h_tokens",
    "text_chars", "web_search_requests", "thinking_tokens",
    "turn_flags", "turn_tool_results",
    "model_id", "provider_id", "stop_reason_id", "effort_id",
    "cli_version_id",                       # int2 (2) — dimension ids
    "fallback",                             # TEXT — the uuid's other slots
    "request_id", "reply_latency_s",        # varlena / numeric (4, var)
    "generation_s", "output_tokens_per_s",   # varlena numeric
    "is_canonical", "is_replay",            # boolean (1), trailing
)
TOOL_USES_ORDER = (
    "ts",
    "file_id", "line_num", "idx", "lines_added", "lines_deleted",
    "dispatch_prompt_chars", "result_chars",
    "tool_name_id", "model_id", "error_kind_id", "agent_type_id",
    "agent_model_id",
    "tool_use_id", "error_text", "dispatch_name",
    "read_targets", "write_targets",        # TEXT[] (4, var)
    "read_kind", "is_error", "dispatch_brief_ref", "is_reread",
    "is_replay", "is_canonical",            # boolean (1), trailing
)

# Dimension lookups: id SMALLINT everywhere (headroom math in the task
# report — the fastest grower, dim_pricing_version, increments weekly
# against a 32,767 ceiling).
DIM_TABLES = (
    "dim_model", "dim_provider", "dim_tool_name", "dim_agent_type",
    "dim_agent_model", "dim_error_kind", "dim_stop_reason", "dim_effort",
    "dim_rate_fingerprint", "dim_pricing_version", "dim_cli_version",
)

# Every FK column referencing a dim table stores SMALLINT too.
SMALLINT_COLUMNS = {
    "records": ("model_id", "provider_id", "stop_reason_id", "effort_id",
                "cli_version_id"),
    "tool_uses": ("tool_name_id", "model_id", "error_kind_id",
                  "agent_type_id", "agent_model_id"),
    "records_price": ("pricing_version_id", "rate_fingerprint_id"),
    "usage_rollup": ("model_id", "provider_id"),
    "tool_rollup": ("model_id", "tool_name_id"),
    "tool_error_rollup": ("model_id", "tool_name_id", "error_kind_id"),
    "dispatch_rollup": ("agent_type_id", "agent_model_id"),
    "dispatch_brief_rollup": ("agent_type_id",),
    "ctx_cost_rollup": ("model_id",),
    "agent_rollup": ("model_id", "agent_type_id"),
    "latency_rollup": ("model_id",),
}

# The surrogate identity keys and their referencing grain columns store
# INTEGER (files 36k / projects 3k / sessions 9k rows today; 2.1B
# ceiling).
INTEGER_COLUMNS = {
    "files": ("file_id",),
    "projects": ("proj_id",),
    "sessions": ("ses_id", "proj_id"),
    "records": ("file_id", "line_num"),
    "tool_uses": ("file_id", "line_num", "idx"),
    "records_price": ("file_id", "line_num"),
    "usage_rollup": ("ses_id", "proj_id"),
    "tool_rollup": ("proj_id",),
    "tool_error_rollup": ("proj_id",),
    "dispatch_rollup": ("proj_id",),
    "dispatch_brief_rollup": ("proj_id",),
    "ctx_cost_rollup": ("proj_id",),
    "agent_rollup": ("proj_id",),
    "latency_rollup": ("proj_id",),
}


def _order(conn, table: str) -> tuple[str, ...]:
    """Live columns of `table` in attnum order."""
    return tuple(r[0] for r in conn.execute(
        "SELECT attname FROM pg_attribute "
        " WHERE attrelid = %s::regclass AND attnum > 0 AND NOT attisdropped"
        " ORDER BY attnum", (table,)
    ).fetchall())


def _types(conn, table: str) -> dict[str, str]:
    """Live columns of `table` as format_type() spells them."""
    return dict(conn.execute(
        "SELECT attname, format_type(atttypid, atttypmod) "
        "  FROM pg_attribute "
        " WHERE attrelid = %s::regclass AND attnum > 0 AND NOT attisdropped",
        (table,)
    ).fetchall())


def _assert_canonical(conn) -> None:
    """Types + order every deployment state must converge onto."""
    for table, columns in SMALLINT_COLUMNS.items():
        types = _types(conn, table)
        for column in columns:
            assert types.get(column) == "smallint", (table, column,
                                                     types.get(column))
    for table, columns in INTEGER_COLUMNS.items():
        types = _types(conn, table)
        for column in columns:
            assert types.get(column) == "integer", (table, column,
                                                    types.get(column))
    for table in DIM_TABLES:
        types = _types(conn, table)
        assert types.get("id") == "smallint", (table, types.get("id"))
    assert _types(conn, "tool_uses").get("read_kind") == "boolean"
    # The uuid split's own column types (issue #959 task 8): the native
    # uuid part, the INT4 counter, the verbatim TEXT fallback — and no
    # `uuid` column left to hold the old TEXT identity.
    split_types = _types(conn, "records")
    assert split_types.get("uuid_col") == "uuid", split_types
    assert split_types.get("seq") == "integer", split_types
    assert split_types.get("fallback") == "text", split_types
    assert "uuid" not in split_types, split_types
    assert _order(conn, "records") == RECORDS_ORDER, _order(conn, "records")
    assert _order(conn, "tool_uses") == TOOL_USES_ORDER, (
        _order(conn, "tool_uses"))


@pytest.mark.db
def test_fresh_database_carries_the_canonical_widths_and_order():
    """The CREATE TABLE declarations themselves (fresh apply)."""
    name = scratch_db.create_database("width_fresh")
    try:
        with psycopg.connect(f"postgresql:///{name}") as conn:
            _assert_canonical(conn)
    finally:
        scratch_db.drop_database(name)


@pytest.mark.db
def test_migrated_database_carries_the_canonical_widths_and_order(
        monkeypatch,
):
    """A pre-#959 database converges to the SAME types and order."""
    name = scratch_db.create_database("width_migrate", schema=scratch_db.OLD_SCHEMA)
    monkeypatch.setenv("CLAUDIT_DATABASE_URL_VIZ", f"postgresql:///{name}")
    db.reset_viz_pool()
    try:
        db.apply_schema()
        with psycopg.connect(f"postgresql:///{name}") as conn:
            _assert_canonical(conn)
    finally:
        db.reset_viz_pool()
        scratch_db.drop_database(name)


@pytest.mark.db
def test_reapply_keeps_the_order_and_destroys_no_rows(monkeypatch):
    """A canonical database is not dropped by its own signature guard.

    The width guard drops a table whose signature is not canonical; a
    re-apply must therefore find the canonical signature, take the
    no-op path, and leave stored rows and attnum order untouched.
    """
    name = scratch_db.create_database("width_reapply")
    now = datetime.now(timezone.utc)
    with psycopg.connect(f"postgresql:///{name}") as conn:
        conn.execute(
            "INSERT INTO projects (project_id, display_name, "
            "first_seen_at, last_seen_at) VALUES ('p', 'p', %s, %s)",
            (now, now))
        conn.execute(
            "INSERT INTO files (file_key, project_id, session_id, "
            "is_main, r2_etag, r2_size_bytes, r2_last_modified, "
            "parsed_at, parser_version) VALUES ('k/p/s/m.jsonl', 'p', "
            "'s', TRUE, 'e', 1, %s, %s, '0')", (now, now))
        conn.execute(
            "INSERT INTO dim_model (value) VALUES ('m') "
            "ON CONFLICT (value) DO UPDATE SET value = EXCLUDED.value")
        conn.execute(
            "INSERT INTO records (file_id, line_num, model_id, ts) "
            "SELECT file_id, 1, id, %s FROM files, dim_model "
            "WHERE files.file_key = 'k/p/s/m.jsonl' "
            "AND dim_model.value = 'm'", (now,))
        before = _order(conn, "records")
        conn.execute("DELETE FROM schema_stamp")
        conn.commit()

    monkeypatch.setenv("CLAUDIT_DATABASE_URL_VIZ", f"postgresql:///{name}")
    db.reset_viz_pool()
    try:
        db.apply_schema()
        db.apply_schema()
        with psycopg.connect(f"postgresql:///{name}") as conn:
            assert (conn.execute(
                "SELECT COUNT(*) FROM records").fetchone() or (-1,))[0] == 1, (
                "the width guard dropped a canonical table")
            assert _order(conn, "records") == before
            _assert_canonical(conn)
    finally:
        db.reset_viz_pool()
        scratch_db.drop_database(name)


def _persist_fixture(fixture: str, key: str) -> None:
    """Parse one parser fixture and push it through the real persist.

    The `fresh_db` fixture must already be active: persist writes to
    db.viz_conn()'s pool, which scratch_db pointed at that database.
    """
    out = parse.parse_file(key, (FIX / fixture).read_bytes())
    when = datetime.now(timezone.utc)
    proj = {"project_id": key.split("/")[1], "display_name": "p",
            "first_seen_at": when, "last_seen_at": when}
    obj = R2Object(key=key, etag="etag", size=1024, last_modified=when)
    ingest._persist(  # pylint: disable=protected-access
        obj, proj, out, "test-version")


@pytest.mark.db
def test_cli_version_round_trips_through_its_smallint_dim(fresh_db):
    """records.cli_version is a dim FK: the TEXT value reads back."""
    _persist_fixture("turn_flags.jsonl",
                     "claude/widths/sess-tf/sess-tf.jsonl")
    # A lane row carries no cli_version and must store NULL — no dim row
    # stands for "unknown": the Codex lane fixture has no version.
    _persist_fixture("codex_min.jsonl", "sessions/widths/wire/wire.jsonl")
    with db.viz_conn() as conn:
        rows = conn.execute(
            "SELECT dv.value FROM records r "
            "  JOIN dim_cli_version dv ON dv.id = r.cli_version_id "
            " ORDER BY r.line_num").fetchall()
        assert [r[0] for r in rows] == ["2.1.250", "2.1.251", "2.1.251"], rows
        nulls = conn.execute(
            "SELECT COUNT(*) FROM records WHERE cli_version_id IS NULL"
        ).fetchone()
        assert nulls is not None and nulls[0] >= 1, (
            "a lane row must store NULL")
        assert (conn.execute(
            "SELECT COUNT(*) FROM dim_cli_version").fetchone() or (0,))[0] == 2


@pytest.mark.db
def test_empty_target_arrays_store_as_null_and_read_kind_as_boolean(
        fresh_db,
):
    """`[]` maps to NULL on insert; read_kind's two values map to BOOL.

    The fixture's five calls carry: two whole reads with targets, one
    slice with targets, one Edit that writes and reads nothing, and one
    whole read after it — every empty array must come back NULL and the
    kind as TRUE (whole) / FALSE (slice) / NULL (not a read).
    """
    key = "claude/widths2/sess-reread/sess-reread.jsonl"
    _persist_fixture("reread.jsonl", key)
    with db.viz_conn() as conn:
        rows = conn.execute(
            "SELECT t.idx, t.read_kind, t.read_targets, t.write_targets "
            "  FROM tool_uses t JOIN files f ON f.file_id = t.file_id "
            " WHERE f.file_key = %s ORDER BY t.line_num, t.idx", (key,)
        ).fetchall()
    assert [r[1] for r in rows] == [True, True, False, None, True], rows
    # Whole and slice reads: targets kept; they write nothing -> NULL.
    assert rows[0][2] == ["/repo/n.md"] and rows[0][3] is None, rows[0]
    assert rows[2][2] == ["/repo/n.md"] and rows[2][3] is None, rows[2]
    # The Edit: reads nothing -> NULL array, writes the path.
    assert rows[3][2] is None and rows[3][3] == ["/repo/n.md"], rows[3]
