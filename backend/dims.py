"""Dimension id resolution shared by the ingest write paths (#959).

The fact tables store dim FK ids (`dim_model`, `dim_provider`, ...), so
every writer resolves its TEXT values through these helpers first. Read
what the table already holds, insert only the values it does not, then
read those ids back — never an upsert of a value that is already stored,
because a tuple's DEFAULT nextval fires while the tuple is formed,
BEFORE the unique index sees the conflict: the attempted insert would
burn an id it never uses, and DO UPDATE would additionally lock a row no
writer needed to touch (the four persist threads queueing behind one hot
dim row). `value` is the UNIQUE text, so resolving '' lands on the
reserved id 0 row the schema seeds (R8).
"""
from __future__ import annotations

from collections.abc import Iterable

import psycopg

from backend import db

# The only tables dim_ids may interpolate: the fixed dim lookup set the
# schema creates (checked before any interpolation, so an unknown name
# is refused here rather than reaching SQL).
_DIM_TABLES = frozenset({
    "dim_agent_model", "dim_agent_type", "dim_cli_version", "dim_effort",
    "dim_error_kind", "dim_model", "dim_pricing_version", "dim_provider",
    "dim_rate_fingerprint", "dim_stop_reason", "dim_tool_name",
})


def dim_ids(conn: psycopg.Connection, table: str,
            values: Iterable[str | None]) -> dict[str, int]:
    """Resolve every non-NULL value to its id; return {value: id}."""
    if table not in _DIM_TABLES:
        raise ValueError(f"not a dimension table: {table!r}")
    distinct = sorted({v for v in values if v is not None})
    if not distinct:
        return {}
    select = db.sql_text(
        f"SELECT value, id FROM {table} "  # table checked vs _DIM_TABLES
        "WHERE value = ANY(%s)"
    )
    found = dict(conn.execute(select, (distinct,)).fetchall())
    missing = [value for value in distinct if value not in found]
    if missing:
        conn.execute(
            db.sql_text(
                f"INSERT INTO {table} (value) "  # checked vs _DIM_TABLES
                "SELECT v FROM unnest(%s::text[]) AS t(v) "
                "ON CONFLICT (value) DO NOTHING"
            ),
            (missing,),
        )
        # Read the ids back instead of trusting RETURNING: a concurrent
        # writer may have inserted some of them, and DO NOTHING returns
        # no row for the ones it lost.
        found.update(conn.execute(select, (missing,)).fetchall())
    return {value: found[value] for value in distinct}


def dim_id(conn: psycopg.Connection, table: str, value: str) -> int:
    """The id for one value, upserting it (a frequently-used single)."""
    return dim_ids(conn, table, [value])[value]
