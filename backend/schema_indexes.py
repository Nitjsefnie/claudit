"""Collection-conditioned indexes in the atomic startup schema script.

Ordinary index builds can block startup and writes on initial boot or
reenabling collection. The schema lock_timeout bounds waiting, not build
duration; production build duration is unmeasured. CONCURRENTLY needs a
different transaction/recovery contract and cannot run in this transaction.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.flags import CollectionPolicy


# Fixed SQL only: configuration selects a branch, never an identifier.
# turn_flags is an INT4 mask. Postgres has no default integer GIN operator
# class, so no turn_context GIN index can be built; migration tests pin
# its absence.
_ANALYSIS_INDEXES = (
    (
        "context_intake",
        "CREATE INDEX IF NOT EXISTS tool_uses_read_targets_idx ON public.tool_uses USING GIN (read_targets);",
        "DROP INDEX IF EXISTS public.tool_uses_read_targets_idx;",
    ),
    (
        "context_intake",
        "CREATE INDEX IF NOT EXISTS tool_uses_reread_idx ON public.tool_uses (ts) WHERE is_reread;",
        "DROP INDEX IF EXISTS public.tool_uses_reread_idx;",
    ),
)


def analysis_index_sql(collection: CollectionPolicy) -> str:
    return "\n".join(create if collection.enabled(key) else drop
                     for key, create, drop in _ANALYSIS_INDEXES)


def compile_schema(static_sql: str, collection: CollectionPolicy) -> str:
    return static_sql + "\n" + analysis_index_sql(collection)


def startup_schema(static_sql: str) -> str:
    """Resolve policy only at startup, after the entrypoint loads dotenv."""
    from backend import flags  # pylint: disable=import-outside-toplevel

    return compile_schema(static_sql, flags.CONFIG.collection)
