"""Postgres connection pools.

Two pools:
- viz_pool   → claudit (this app's tables)
- auth_pool  → external users DB (read-only access to users.config for auth)

The pools never join across DBs.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import LiteralString, cast

from psycopg_pool import ConnectionPool

_VIZ: ConnectionPool | None = None
_AUTH: ConnectionPool | None = None


def sql_text(query: str) -> LiteralString:
    """Mark a programmatically assembled query string as executable.

    psycopg types Cursor.execute() as taking a LiteralString, so a static
    checker flags every runtime-built SQL string. The queries passed
    through here are assembled exclusively from our own fragments —
    branch-selected WHERE clauses, integer bucket widths — and user input
    only ever reaches the DB as a %s parameter, so the literal-only
    guarantee the type wants is upheld by construction.

    The return type is typing.LiteralString and NOT psycopg.abc.Query,
    which is what it used to be. psycopg 3.3 split that alias: execute()
    now takes QueryNoTemplate, because Query gained
    string.templatelib.Template for 3.14 t-strings, and a value typed as
    the whole union matches neither overload. LiteralString is a member of
    both aliases and belongs to the standard library, so this no longer
    tracks psycopg's type-alias churn at all.
    """
    return cast(LiteralString, query)


def viz_pool() -> ConnectionPool:
    global _VIZ
    if _VIZ is None:
        _VIZ = ConnectionPool(
            os.environ["DATABASE_URL_VIZ"],
            # The read endpoints are sync (blocking psycopg) and run on
            # FastAPI's threadpool, so requests now hit the DB genuinely
            # concurrently — a single dashboard load fans out to ~7. While
            # they were async-on-the-event-loop they serialised and 8 was
            # never exercised; it would now be the bottleneck.
            min_size=2, max_size=20, timeout=10,
            kwargs={"autocommit": False},
            check=ConnectionPool.check_connection,
        )
    return _VIZ


def reset_viz_pool() -> None:
    """Close and drop the cached viz pool so the next viz_pool() call
    re-reads DATABASE_URL_VIZ. Test suites need this between scratch-DB
    configurations; production code never calls it."""
    global _VIZ
    if _VIZ is not None:
        try:
            _VIZ.close()
        except Exception:
            pass
    _VIZ = None


def auth_pool() -> ConnectionPool:
    global _AUTH
    if _AUTH is None:
        _AUTH = ConnectionPool(
            os.environ["DATABASE_URL_AUTH"],
            min_size=1, max_size=4, timeout=10,
            kwargs={"autocommit": True},
            check=ConnectionPool.check_connection,
        )
    return _AUTH


@contextmanager
def viz_conn():
    with viz_pool().connection() as conn:
        yield conn


@contextmanager
def auth_conn():
    with auth_pool().connection() as conn:
        yield conn


# backend/schema.sql, resolved next to this module so the working
# directory the service was started from does not matter.
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

# Arbitrary but fixed key for the advisory lock the migration holds.
# Two processes starting at once would otherwise run the same DDL
# concurrently: IF NOT EXISTS makes each statement individually safe, but
# two backends creating the same index still deadlock against each other.
_SCHEMA_LOCK_KEY = 0x5356_4D49


def apply_schema() -> None:
    """Apply backend/schema.sql to the app DB at startup.

    schema.sql is idempotent by construction -- every statement is
    CREATE ... IF NOT EXISTS or ALTER TABLE ... ADD COLUMN IF NOT EXISTS
    -- so running it on every boot converges the database onto the shape
    the running code expects instead of trusting that a human ran psql
    after deploying (issue #43). Two deploys failed that way in one day:
    a checkout pulled code writing a new column, the migration step was
    missed, and every ingest then aborted with UndefinedColumn while the
    dashboard kept serving stale aggregates.

    Executed through psycopg rather than shelling out to psql: the
    service already holds a connection with the right credentials, and a
    psql subprocess would add a PATH dependency the container need not
    satisfy.

    ROLLBACK IS ONE-DIRECTIONAL, and that is the accepted cost. Deploying
    forward then restarting an older binary leaves it running against a
    schema from the future. Every migration here is additive and
    nullable, so an older binary ignores what it does not know about;
    that property is what makes auto-apply safe, and a migration that
    drops or retypes a column would break it.
    """
    ddl = SCHEMA_PATH.read_text(encoding="utf-8")
    with viz_conn() as c:
        # Serialize concurrent boots. Session-scoped, released on the
        # connection returning to the pool at the end of this block.
        c.execute("SELECT pg_advisory_lock(%s)", (_SCHEMA_LOCK_KEY,))
        try:
            c.execute(sql_text(ddl))
            c.commit()
        finally:
            c.execute("SELECT pg_advisory_unlock(%s)", (_SCHEMA_LOCK_KEY,))
            c.commit()


def schema_check() -> None:
    """Fail fast at startup if either DB's required shape is missing.

    For claudit: 'files' table exists.
    For the auth DB: 'users' table has a JSONB 'config' column.
    Raises RuntimeError on any mismatch.
    """
    with viz_conn() as c:
        row = c.execute(
            "SELECT to_regclass('public.files')"
        ).fetchone()
        if row is None or row[0] is None:
            raise RuntimeError(
                "claudit.files missing — run backend/schema.sql"
            )
    with auth_conn() as c:
        row = c.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='users' "
            "AND column_name='config'"
        ).fetchone()
        if row is None:
            raise RuntimeError(
                "auth DB users table has no 'config' column"
            )
        if row[0] != "jsonb":
            raise RuntimeError(
                f"auth DB users.config must be JSONB, got {row[0]!r}"
            )


def load_dotenv(path: str = ".env") -> None:
    """Tiny dotenv loader. Avoids the python-dotenv dependency.

    Constraints (intentional simplicity — keep values plain):
    - Values are read literally; quotes are NOT stripped. ADMIN_TOKEN="abc"
      stores the value with the literal quotes.
    - The 'export ' prefix is NOT supported (the line key would become
      'export ADMIN_TOKEN', not 'ADMIN_TOKEN').
    - The first '=' splits key from value, so values may contain '=' freely.
    - Existing env vars are NEVER overwritten (uses os.environ.setdefault).
    - Comment lines (starting with '#') and blank lines are skipped.
    """
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
