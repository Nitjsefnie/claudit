"""Per-row measures narrow to INT4; rollup totals stay BIGINT."""
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
@pytest.mark.parametrize("tables, expected", [
    ({"records": RECORD_INT4}, "integer"),
    ({"tool_uses": TOOL_INT4}, "integer"),
    (ROLLUP_BIGINT, "bigint"),
], ids=["record-measures", "tool-churn", "rollup-totals"])
def test_measure_column_types(tables, expected):
    name = scratch_db.create_database("measure_types")
    try:
        with psycopg.connect(f"postgresql:///{name}") as conn:
            for table, columns in tables.items():
                types = _column_types(conn, table)
                for column in columns:
                    assert types.get(column) == expected, (
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
