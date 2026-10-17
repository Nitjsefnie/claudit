"""Schema reapplication must not consume PostgreSQL attribute numbers.
Adding a column then dropping it still consumes an attnum permanently.
Compare pg_attribute counts, including dropped attributes, across fresh
and migrated schemas and repeated applies; the final states must agree."""
from __future__ import annotations

import psycopg
import pytest

from backend import db
from tests import scratch_db

# The fact and rollup tables the retired columns lived on.
_LEAKY = ("records", "tool_uses", "usage_rollup")


def _attnats(conn) -> dict[str, int]:
    """pg_attribute rows per public base table — dropped ones included."""
    return dict(conn.execute(
        "SELECT c.relname, COUNT(*) "
        "  FROM pg_attribute a "
        "  JOIN pg_class c ON c.oid = a.attrelid "
        "  JOIN pg_namespace n ON n.oid = c.relnamespace "
        " WHERE n.nspname = 'public' AND c.relkind = 'r' "
        " GROUP BY c.relname"
    ).fetchall())


def _live_columns(conn, table: str) -> set[str]:
    return scratch_db.table_columns(conn, table)


def _apply_twice(monkeypatch, name: str) -> dict[str, int]:
    monkeypatch.setenv("CLAUDIT_DATABASE_URL_VIZ", f"postgresql:///{name}")
    db.reset_viz_pool()
    try:
        db.apply_schema()
        with psycopg.connect(f"postgresql:///{name}") as conn:
            first = _attnats(conn)
            conn.execute("DELETE FROM schema_stamp")
            conn.commit()
        db.apply_schema()
        with psycopg.connect(f"postgresql:///{name}") as conn:
            second = _attnats(conn)
        assert second == first, (
            "a re-apply changed the catalog: "
            + ", ".join(f"{t}: {first[t]} -> {second[t]}"
                        for t in sorted(set(first) | set(second))
                        if first.get(t) != second.get(t)))
        return second
    finally:
        db.reset_viz_pool()


@pytest.mark.db
def test_fresh_and_migrated_end_states_carry_the_same_catalog(monkeypatch):
    fresh = scratch_db.create_database("attnat_fresh")
    migrated = scratch_db.create_database("attnat_migrate",
                                          schema=scratch_db.OLD_SCHEMA)
    try:
        fresh_attnats = _apply_twice(monkeypatch, fresh)
        migrated_attnats = _apply_twice(monkeypatch, migrated)
        for table in _LEAKY:
            assert fresh_attnats[table] == migrated_attnats[table], table
        with psycopg.connect(f"postgresql:///{fresh}") as fresh_conn, \
                psycopg.connect(f"postgresql:///{migrated}") as migrated_conn:
            for table in _LEAKY:
                assert (_live_columns(fresh_conn, table)
                        == _live_columns(migrated_conn, table)), table
                # The retired TEXT columns are gone at rest, not merely
                # dropped-and-re-added: a leaked one would still be live
                # between applies only mid-transaction, so the live set
                # pins the shape and the attnat count pins the leak.
                assert not ({"provider", "stop_reason", "effort",
                             "pricing_version", "rate_fingerprint",
                             "request_fee_usd", "model", "error_kind",
                             "agent_type", "agent_model", "session_id",
                             "project_id", "tool_name"} &
                            _live_columns(fresh_conn, table)), table
    finally:
        scratch_db.drop_database(fresh)
        scratch_db.drop_database(migrated)
