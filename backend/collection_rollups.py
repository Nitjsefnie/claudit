"""Global cleanup and rebuild eligibility for optional analytical rollups."""
from __future__ import annotations

from collections.abc import Callable

from psycopg import sql

from backend import db
from backend.flags import CollectionPolicy


def disabled_rollups(collection: CollectionPolicy) -> frozenset[str]:
    """Read optional table ownership from the collection registry."""
    return frozenset(table for spec in collection.specs
                     if spec.key in collection.disabled
                     for table in spec.rollup_tables)


def clear_disabled_rollups(tables: frozenset[str]) -> int:
    """Empty disabled tables globally in one transaction, even on a zero-todo run."""
    if not tables:
        return 0
    with db.viz_conn() as conn:
        deleted = 0
        for table in sorted(tables):
            cur = conn.execute(sql.SQL('DELETE FROM {}').format(
                sql.Identifier('public', table)))
            deleted += cur.rowcount
        conn.commit()
    return deleted


def collected_phases(phases: tuple[tuple[str, Callable[[], int]], ...],
                     tables: frozenset[str]) -> tuple[tuple[str, Callable[[], int]], ...]:
    """Keep retained phase order while excluding disabled rebuild callables."""
    return tuple((name, rebuild) for name, rebuild in phases if name not in tables)
