"""Per-row measure columns narrow to INT4; every rollup total stays BIGINT.

Ruling R7: only per-row measures — the nine records token
columns, tool_uses' churn and result_chars — narrow; corpus evidence
says no per-row value reaches 2^31-1. Aggregates are sums over the whole
corpus and exceed int4 by construction, so every rollup TOTAL column
stays BIGINT. Also require no index on the turn_flags mask and guarded
convergence of a database already carrying
the surrogate-id shape (the path schema_dims.sql's legacy drop skips).
"""
from __future__ import annotations

import psycopg
import pytest

from backend import db
from tests import scratch_db

RECORD_INT4 = (
    "fresh_tokens",
    "cache_creation_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "output_tokens",
    "eph5_tokens",
    "eph1h_tokens",
    "thinking_tokens",
    "text_chars",
)
TOOL_INT4 = ("lines_added", "lines_deleted", "result_chars")

# Aggregate TOTALS (SV-ROLLUP): sums over records, kept BIGINT (R7).
ROLLUP_BIGINT = {
    "usage_rollup": (
        "requests", "fresh_tokens", "output_tokens",
        "cache_creation_tokens", "cache_read_tokens", "eph5_tokens",
        "eph1h_tokens", "thinking_tokens",
    ),
    "tool_rollup": ("n_total", "n_rated", "n_error",
                    "lines_added", "lines_deleted"),
    "tool_error_rollup": ("n",),
    "dispatch_rollup": ("n",),
    "dispatch_brief_rollup": ("n", "prompt_chars"),
    "ctx_cost_rollup": ("requests", "total_tokens"),
    "agent_rollup": ("requests", "output_tokens", "total_tokens"),
    "latency_rollup": ("n",),
}


def _column_types(conn, table: str) -> dict[str, str]:
    rows = conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s", (table,)
    ).fetchall()
    return dict(rows)


def _index_names(conn) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"
    ).fetchall()}


@pytest.mark.db
def test_record_measures_are_integer():
    name = scratch_db.create_database("int4_records")
    try:
        with psycopg.connect(f"postgresql:///{name}") as conn:
            types = _column_types(conn, "records")
            for column in RECORD_INT4:
                assert types.get(column) == "integer", (column,
                                                        types.get(column))
    finally:
        scratch_db.drop_database(name)


@pytest.mark.db
def test_tool_churn_is_integer():
    name = scratch_db.create_database("int4_tools")
    try:
        with psycopg.connect(f"postgresql:///{name}") as conn:
            types = _column_types(conn, "tool_uses")
            for column in TOOL_INT4:
                assert types.get(column) == "integer", (column,
                                                        types.get(column))
    finally:
        scratch_db.drop_database(name)


@pytest.mark.db
def test_rollup_totals_stay_bigint():
    """Narrowing is per-row only: aggregates keep their width (R7)."""
    name = scratch_db.create_database("rollup_bigint")
    try:
        with psycopg.connect(f"postgresql:///{name}") as conn:
            for table, columns in ROLLUP_BIGINT.items():
                types = _column_types(conn, table)
                for column in columns:
                    assert types.get(column) == "bigint", (
                        table, column, types.get(column))
    finally:
        scratch_db.drop_database(name)


@pytest.mark.db
def test_no_index_on_the_turn_flags_mask():
    """The token mask has no replacement GIN index."""
    name = scratch_db.create_database("no_tf_index")
    try:
        with psycopg.connect(f"postgresql:///{name}") as conn:
            assert not any("turn_flags" in name_
                           for name_ in _index_names(conn))
            rows = conn.execute(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE schemaname = 'public' AND indexdef ILIKE "
                "'%turn_flags%'").fetchall()
            assert rows == [], rows
    finally:
        scratch_db.drop_database(name)


@pytest.mark.db
def test_schema_retypes_a_surrogate_shape_database(monkeypatch):
    """A database already carrying the surrogate shape converges.

    schema_dims.sql's drop guard keys on the legacy text shape
    (records.file_key); a database migrated by Task 1 keeps its rows, so
    the type compaction runs as guarded in-place ALTERs instead — a
    no-op once the columns already carry the narrow types.
    """
    name = scratch_db.create_database("type_converge")
    monkeypatch.setenv("CLAUDIT_DATABASE_URL_VIZ", f"postgresql:///{name}")
    db.reset_viz_pool()
    try:
        with psycopg.connect(f"postgresql:///{name}") as conn:
            conn.execute(
                "ALTER TABLE records ALTER COLUMN turn_flags DROP DEFAULT, "
                "ALTER COLUMN turn_flags TYPE text[] USING '{}'::text[], "
                "ALTER COLUMN turn_flags SET DEFAULT '{}', "
                "ALTER COLUMN fresh_tokens TYPE bigint, "
                "ALTER COLUMN text_chars TYPE bigint")
            conn.execute(
                "ALTER TABLE tool_uses ALTER COLUMN lines_added TYPE bigint")
            conn.execute("DELETE FROM schema_stamp")
            conn.commit()

        db.apply_schema()

        with psycopg.connect(f"postgresql:///{name}") as conn:
            types = _column_types(conn, "records")
            assert types["turn_flags"] == "integer"
            assert types["fresh_tokens"] == "integer"
            assert types["text_chars"] == "integer"
            assert _column_types(conn, "tool_uses")["lines_added"] == "integer"
            default = conn.execute(
                "SELECT column_default FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'records' "
                "AND column_name = 'turn_flags'").fetchone()
            assert default is not None and default[0] == "0", default
            # Re-running the whole schema stays a no-op (SV-SCHEMA-AUTOAPPLY).
            db.apply_schema()
            again = _column_types(conn, "records")["turn_flags"]
            assert again == "integer"
    finally:
        db.reset_viz_pool()
        scratch_db.drop_database(name)
