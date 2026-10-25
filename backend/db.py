"""Postgres connection pools.

Two pools:
- viz_pool   → claudit (this app's tables)
- auth_pool  → external users DB (READ-ONLY from this application:
               password hashes only; user session secrets live in
               claudit's own user_session table)

The pools never join across DBs.
"""
from __future__ import annotations

import hashlib
import logging
import threading
from contextlib import closing, contextmanager
from typing import LiteralString, cast

import psycopg
from psycopg_pool import ConnectionPool

# The schema, as an ordered list of files applied under one content
# stamp. Re-exported because `db.SCHEMA_PATH` and
# `db.SCHEMA_PATHS` are the names the startup tests reach for, and
# the list itself lives in schema_files so this module stays inside
# the size-ratchet action's 500-line ceiling.
# pylint: disable=unused-import
from backend.env_config import load_dotenv, require_env
from backend.schema_files import (  # noqa: F401
    SCHEMA_PATH, SCHEMA_PATHS, read_schema,
)
from backend.schema_indexes import startup_schema

log = logging.getLogger("claudit.db")

_VIZ: ConnectionPool | None = None
_AUTH: ConnectionPool | None = None
_POOL_LOCK = threading.Lock()

# Connections currently checked out of the viz pool, so teardown can
# cancel a statement that is stuck server-side. viz_conn is
# the single checkout point for both API traffic and ingest phases.
_VIZ_ACTIVE: set = set()
_VIZ_ACTIVE_LOCK = threading.Lock()


def track_viz_conn(conn) -> None:
    with _VIZ_ACTIVE_LOCK:
        _VIZ_ACTIVE.add(conn)


def untrack_viz_conn(conn) -> None:
    with _VIZ_ACTIVE_LOCK:
        _VIZ_ACTIVE.discard(conn)


def sql_text(query: str) -> LiteralString:
    """Mark a programmatically assembled query as executable LiteralString.

    Only owned SQL fragments and integer bucket widths are assembled;
    user input reaches SQL solely as %s parameters. LiteralString
    therefore preserves execute's literal-only guarantee by construction
    and fits both psycopg Query and QueryNoTemplate without including
    templatelib.Template's incompatible overload."""
    return cast(LiteralString, query)


def _probe_first_connection(dsn: str, env_var: str, label: str) -> None:
    """Connect once, synchronously, before a pool opens.

    The pool starts its connection workers in the background: a DSN the
    server rejects (wrong password, missing database) fails only inside
    those workers, which log it on the psycopg pool logger at most, while
    the first client sees a bare PoolTimeout with the cause suppressed
    (`raise ... from None` in getconn). First boot then fails on a
    timeout that names neither the database nor the reason.

    One bounded direct connection surfaces the driver's diagnostics: on
    failure the server's FATAL (or the dial error) reaches the log and
    the boot abort chained to a RuntimeError naming the env var whose
    DSN to check — the SV-SCHEMA-FAIL-FAST shape. On success the
    connection is discarded; the pool's own workers fill from there.
    """
    try:
        with closing(psycopg.connect(dsn, connect_timeout=5)):
            pass
    except Exception as exc:
        log.error(
            "the %s pool could not make its first connection: %s — "
            "check %s (host, port, database name, credentials)",
            label, exc, env_var)
        raise RuntimeError(
            f"the {label} pool could not make its first connection: {exc}"
            f" — check {env_var} (host, port, database name, credentials)"
        ) from exc


def viz_pool() -> ConnectionPool:
    global _VIZ
    with _POOL_LOCK:
        if _VIZ is None:
            dsn = require_env("CLAUDIT_DATABASE_URL_VIZ")
            _probe_first_connection(dsn, "CLAUDIT_DATABASE_URL_VIZ", "viz")
            _VIZ = ConnectionPool(
                dsn,
                # Sync read endpoints run blocking psycopg in FastAPI's threadpool;
                # a dashboard fans out concurrent queries, so the pool must accommodate
                # that concurrency instead of serializing the request fan-out.
                min_size=2, max_size=20, timeout=10,
                kwargs={"autocommit": False},
                check=ConnectionPool.check_connection,
                # Explicit open: the implicit-open default is deprecated in
                # psycopg_pool and flips to False in a future release.
                open=True,
            )
        return _VIZ


def reset_viz_pool() -> None:
    """Close and drop the cached viz pool so the next viz_pool() call
    re-reads CLAUDIT_DATABASE_URL_VIZ. Test suites need this between scratch-DB
    configurations; production code never calls it."""
    global _VIZ
    with _POOL_LOCK:
        if _VIZ is not None:
            try:
                _VIZ.close()
            except Exception:
                pass
        _VIZ = None


def viz_pool_statistics() -> dict:
    """Current viz-pool gauges for /health's cache readout:
    requests_waiting is the acquire-queue depth; {} when no pool exists,
    because its absence must not gain /health a failure mode."""
    try:
        stats = viz_pool().get_stats()
    except Exception:  # noqa: BLE001 - a readout must not fail the readout
        return {}
    return {k: stats[k] for k in
            ("pool_available", "pool_max", "pool_size", "requests_waiting")}


def auth_pool() -> ConnectionPool:
    global _AUTH
    with _POOL_LOCK:
        if _AUTH is None:
            dsn = require_env("CLAUDIT_DATABASE_URL_AUTH")
            _probe_first_connection(dsn, "CLAUDIT_DATABASE_URL_AUTH", "auth")
            _AUTH = ConnectionPool(
                dsn,
                min_size=1, max_size=4, timeout=10,
                kwargs={"autocommit": True,
                        # claudit only READS the auth DB — enforce it at the
                        # server on every connection the pool opens, so a
                        # future write raises ReadOnlySqlTransaction instead
                        # of committing. The option rides the
                        # startup packet, so pool reconnects inherit it.
                        "options": "-c default_transaction_read_only=on"},
                check=ConnectionPool.check_connection,
                # Same explicit open as the viz pool above.
                open=True,
            )
        return _AUTH


def reset_auth_pool() -> None:
    """Close and drop the cached auth pool so the next auth_pool() call
    re-reads CLAUDIT_DATABASE_URL_AUTH. Mirrors reset_viz_pool; test suites need
    this between auth-DB configurations, production code never calls it."""
    global _AUTH
    with _POOL_LOCK:
        if _AUTH is not None:
            try:
                _AUTH.close()
            except Exception:
                pass
        _AUTH = None


@contextmanager
def viz_conn():
    with viz_pool().connection() as conn:
        track_viz_conn(conn)
        try:
            yield conn
        finally:
            untrack_viz_conn(conn)


def cancel_viz_queries() -> None:
    """Cancel every statement in flight on a checked-out viz connection.

    Called from lifespan teardown after the abort request:
    the bounded steps cannot fire while a run sits inside one long
    single-statement phase — the clean restamp, a rollup rebuild — so the
    cancel is what stops the statement server-side; the driver raises
    QueryCanceled, which the run classifies as the abort arriving.
    Cancels API-traffic statements too: at teardown the process is
    stopping and their requests are doomed with it. Best-effort — a
    connection already gone, or a cancel refusal, must not break teardown.
    """
    with _VIZ_ACTIVE_LOCK:
        active = list(_VIZ_ACTIVE)
    for conn in active:
        try:
            conn.cancel()
        except Exception:  # noqa: BLE001
            log.warning("cancel_viz_queries: cancelling a connection failed",
                        exc_info=True)


@contextmanager
def auth_conn():
    with auth_pool().connection() as conn:
        yield conn


# Arbitrary but fixed key for the advisory lock the migration holds.
# Two processes starting at once would otherwise run the same DDL
# concurrently: IF NOT EXISTS makes each statement individually safe, but
# two backends creating the same index still deadlock against each other.
_SCHEMA_LOCK_KEY = 0x5356_4D49

# How long the schema transaction waits for ANY single lock — the
# advisory boot lock and every DDL lock — before giving up.
# A current schema takes no lock on `records` at all (the DDL is skipped
# by stamp); this bounds only the wait when a migration IS due and some
# long-running transaction (a psql analysis, a pg_dump) holds a
# conflicting lock. Without a bound, boot and readers behind it can wait indefinitely. The value lives in code, never the
# environment, like every version constant here.
_SCHEMA_LOCK_TIMEOUT = "5s"


def _stamp_version(c) -> str | None:
    """Schema content stamp, or None when DDL cannot be skipped.

    A missing stamp table or a cleared row forces reapplication."""
    row = c.execute(
        "SELECT to_regclass('public.schema_stamp')").fetchone()
    if row is None or row[0] is None:
        return None
    have = c.execute("SELECT version FROM schema_stamp").fetchone()
    return have[0] if have is not None else None


def apply_schema() -> None:
    """Converge static schema and collection index policy in one transaction.

    The SHA256 of the complete effective script is the schema_stamp. A
    matching stamp skips DDL and the migration lock; a changed script is
    rechecked under the advisory lock, then applied and stamped atomically.
    Lock waits are bounded by lock_timeout, not index-build duration.
    Ordinary initial/reenable builds may block boot and writers; production
    duration is unmeasured. Failed DDL rolls back before unlock, so a retry
    converges without a partial schema or stamp.

    Additive columns, nullable relaxations, guarded key/type widenings and
    nonconstraint index policy preserve older named-column writes. Older
    ingest never reparses a newer parser_version. An older binary may
    recreate removed indexes; its differing stamp makes the next forward
    boot reconverge. Out-of-band catalog damage still requires clearing
    schema_stamp: the stamp proves execution, not subsequent catalog state.
    """
    ddl = startup_schema(read_schema())
    stamp = hashlib.sha256(ddl.encode("utf-8")).hexdigest()
    with viz_conn() as c:
        if _stamp_version(c) == stamp:
            # Current: the block exit ends the read transaction. No
            # DDL, no exclusive lock anywhere.
            c.commit()
            return
        # A migration is due, or the stamp is missing. Bounded waits:
        # lock_timeout is transaction-local and covers the advisory
        # boot lock and every DDL lock.
        c.execute("SELECT set_config('lock_timeout', %s, true)",
                  (_SCHEMA_LOCK_TIMEOUT,))
        locked = False
        committed = False
        try:
            c.execute("SELECT pg_advisory_lock(%s)", (_SCHEMA_LOCK_KEY,))
            locked = True
            if _stamp_version(c) == stamp:
                # A boot that queued beside us converged the schema
                # first; its commit is visible here under READ
                # COMMITTED.
                return
            c.execute(sql_text(ddl))
            c.execute(
                "INSERT INTO schema_stamp (singleton, version) "
                "VALUES (TRUE, %s) ON CONFLICT (singleton) DO UPDATE "
                "SET version = EXCLUDED.version, applied_at = now()",
                (stamp,))
            c.commit()
            committed = True
        except psycopg.errors.LockNotAvailable as exc:
            log.error(
                "schema migration gave up on the %s lock timeout; a "
                "long-running transaction is blocking startup DDL. The "
                "transaction aborted whole — nothing applied, the "
                "advisory lock is released, and systemd restarts the "
                "boot: %s", _SCHEMA_LOCK_TIMEOUT, exc)
            raise RuntimeError(
                "schema migration could not take its locks within "
                f"{_SCHEMA_LOCK_TIMEOUT} (a long-running transaction "
                "holds a conflicting lock — end it, or apply "
                "backend/schema.sql out of band); nothing was applied"
            ) from exc
        finally:
            try:
                # Roll back before unlocking so the unlock runs outside
                # an aborted transaction and can actually succeed.
                if not committed:
                    c.rollback()
                if locked:
                    c.execute("SELECT pg_advisory_unlock(%s)",
                              (_SCHEMA_LOCK_KEY,))
                    c.commit()
            except Exception as exc:  # noqa: BLE001
                # The unlock (and the commit ending its
                # transaction) must never mask the DDL's own error — the
                # diagnosable one. Either failure leaves the lock free or
                # the boot dead: a dead session's lock died with it, and
                # an unlock blocked by the failed DDL's aborted
                # transaction accompanies a migration error that aborts
                # this boot, whose exit takes every pooled session —
                # lock included — with it.
                log.warning(
                    "schema advisory-lock unlock failed; the migration's "
                    "own error, if any, is preserved: %s", exc)


# The auth-DB columns the application actually reads, with the types it
# can use: `user_id` keys the login lookup (`session.load_user_config`'s
# WHERE clause) and `config` is the payload it selects. Everything else
# the login path needs (`web_password_hash`, `web_password_salt`, ...)
# is a JSON key INSIDE config, never a column — do not widen this map
# with one.
AUTH_COLUMNS: dict[str, tuple[tuple[str, ...], str]] = {
    "user_id": (
        ("smallint", "integer", "bigint"),
        "an integer type (smallint, integer or bigint)",
    ),
    "config": (("jsonb",), "JSONB"),
}


def schema_check() -> None:
    """Fail fast at startup if either DB's required shape is missing.

    For claudit: 'files' table exists.
    For the auth DB: 'users' carries every column the app reads
    (AUTH_COLUMNS) with a usable type — user_id an integer, config
    JSONB; checking names as well as types prevents UndefinedColumn on login. An information-
    schema probe that does not show a column has three causes,
    discriminated on the same connection before naming one: (a) no
    'users' table visible — the database is wrong or empty; (b) the
    role holds no SELECT on 'users' — an unprivileged table is invisible
    in information_schema.columns; (c) the table is visible and readable
    but the column is genuinely absent. Raises RuntimeError on any
    mismatch.
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
        found = dict(c.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='users' "
            "AND column_name = ANY(%s)",
            (list(AUTH_COLUMNS),),
        ).fetchall())
        missing = [name for name in AUTH_COLUMNS if name not in found]
        if missing:
            # An unseen column has three causes —
            # discriminate on this same connection before naming one.
            exists = c.execute(
                "SELECT to_regclass('public.users')"
            ).fetchone()
            if exists is None or exists[0] is None:
                raise RuntimeError(
                    "auth DB: no 'users' table visible — is "
                    "CLAUDIT_DATABASE_URL_AUTH pointing at the right database?"
                )
            priv_row = c.execute(
                "SELECT has_table_privilege(current_user, 'public.users', "
                "'SELECT'), current_user"
            ).fetchone()
            granted = bool(priv_row and priv_row[0])
            role = str(priv_row[1]) if priv_row else ""
            if not granted:
                raise RuntimeError(
                    f"auth DB: role '{role}' lacks SELECT on 'users' — "
                    f"grant SELECT ON users TO {role} (a table without "
                    "SELECT is invisible in information_schema.columns, "
                    "which is why this looked like a missing column)"
                )
            if len(missing) == 1:
                raise RuntimeError(
                    f"auth DB users table has no '{missing[0]}' column"
                )
            quoted = " and ".join(f"'{name}'" for name in missing)
            raise RuntimeError(
                f"auth DB users table has no {quoted} columns"
            )
        for name, (types, expected) in AUTH_COLUMNS.items():
            if found[name] not in types:
                raise RuntimeError(
                    f"auth DB users.{name} must be {expected}, "
                    f"got {found[name]!r}"
                )
