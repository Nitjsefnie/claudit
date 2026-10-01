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
import os
import threading
from contextlib import closing, contextmanager
from typing import LiteralString, cast

import psycopg
from psycopg_pool import ConnectionPool

# The schema, as an ordered list of files applied under one content
# stamp (issue #436). Re-exported because `db.SCHEMA_PATH` and
# `db.SCHEMA_PATHS` are the names the startup tests reach for, and
# the list itself lives in schema_files so this module stays inside
# the module-size ratchet's 500-line ceiling.
# pylint: disable=unused-import
from backend.schema_files import (  # noqa: F401
    SCHEMA_PATH, SCHEMA_PATHS, read_schema,
)

log = logging.getLogger("claudit.db")

_VIZ: ConnectionPool | None = None
_AUTH: ConnectionPool | None = None

# Connections currently checked out of the viz pool, so teardown can
# cancel a statement that is stuck server-side (issue #372). viz_conn is
# the single checkout point for both API traffic and ingest phases.
_VIZ_ACTIVE: set = set()
_VIZ_ACTIVE_LOCK = threading.Lock()


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


def _probe_first_connection(dsn: str, env_var: str, label: str) -> None:
    """Connect once, synchronously, before a pool opens (issue #449).

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
    if _VIZ is None:
        dsn = os.environ["DATABASE_URL_VIZ"]
        _probe_first_connection(dsn, "DATABASE_URL_VIZ", "viz")
        _VIZ = ConnectionPool(
            dsn,
            # The read endpoints are sync (blocking psycopg) and run on
            # FastAPI's threadpool, so requests now hit the DB genuinely
            # concurrently — a single dashboard load fans out to ~7. While
            # they were async-on-the-event-loop they serialised and 8 was
            # never exercised; it would now be the bottleneck.
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
        dsn = os.environ["DATABASE_URL_AUTH"]
        _probe_first_connection(dsn, "DATABASE_URL_AUTH", "auth")
        _AUTH = ConnectionPool(
            dsn,
            min_size=1, max_size=4, timeout=10,
            kwargs={"autocommit": True,
                    # claudit only READS the auth DB — enforce it at the
                    # server on every connection the pool opens, so a
                    # future write raises ReadOnlySqlTransaction instead
                    # of committing (issue #460). The option rides the
                    # startup packet, so pool reconnects inherit it.
                    "options": "-c default_transaction_read_only=on"},
            check=ConnectionPool.check_connection,
            # Same explicit open as the viz pool above.
            open=True,
        )
    return _AUTH


def reset_auth_pool() -> None:
    """Close and drop the cached auth pool so the next auth_pool() call
    re-reads DATABASE_URL_AUTH. Mirrors reset_viz_pool; test suites need
    this between auth-DB configurations, production code never calls it."""
    global _AUTH
    if _AUTH is not None:
        try:
            _AUTH.close()
        except Exception:
            pass
    _AUTH = None


@contextmanager
def viz_conn():
    with viz_pool().connection() as conn:
        with _VIZ_ACTIVE_LOCK:
            _VIZ_ACTIVE.add(conn)
        try:
            yield conn
        finally:
            with _VIZ_ACTIVE_LOCK:
                _VIZ_ACTIVE.discard(conn)


def cancel_viz_queries() -> None:
    """Cancel every statement in flight on a checked-out viz connection.

    Called from lifespan teardown after the abort request (issue #372):
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
# advisory boot lock and every DDL lock — before giving up (issue #387).
# A current schema takes no lock on `records` at all (the DDL is skipped
# by stamp); this bounds only the wait when a migration IS due and some
# long-running transaction (a psql analysis, a pg_dump) holds a
# conflicting lock. Without the bound the boot queued forever, and every
# other reader queued behind it. The value lives in code, never the
# environment, like every version constant here.
_SCHEMA_LOCK_TIMEOUT = "5s"


def _stamp_version(c) -> str | None:
    """The schema content stamp this database carries, or None when the
    DDL cannot be skipped (the stamp table missing — a DB from before
    issue #387 — or the row cleared to force a re-apply)."""
    row = c.execute(
        "SELECT to_regclass('public.schema_stamp')").fetchone()
    if row is None or row[0] is None:
        return None
    have = c.execute("SELECT version FROM schema_stamp").fetchone()
    return have[0] if have is not None else None


def apply_schema() -> None:
    """Apply the schema files (SCHEMA_PATHS) to the app DB at startup.

    The schema is idempotent by construction -- every statement is
    CREATE ... IF NOT EXISTS, ALTER TABLE ... ADD COLUMN IF NOT EXISTS,
    or a guarded DO block that widens usage_rollup's primary key (it
    drops the old constraint only when it does not already carry the
    widened grain, then re-adds it; ADD CONSTRAINT has no IF NOT
    EXISTS) -- so running it converges the database onto the shape the
    running code expects instead of trusting that a human ran psql
    after deploying (issue #43). Two deploys failed that way in one
    day: a checkout pulled code writing a new column, the migration
    step was missed, and every ingest then aborted with UndefinedColumn
    while the dashboard kept serving stale aggregates.

    The DDL runs only when it has something to do (issue #387): the
    schema is content-addressed in `schema_stamp` — the sha256 of every
    schema file's exact bytes in order, written by the same transaction
    as the DDL —
    and a boot whose stamp already matches takes no lock beyond an
    ACCESS SHARE on the stamp table itself. That is what keeps a long
    psql analysis or pg_dump over `records` from hanging a start, and
    being hung by it: every ADD COLUMN / SET / DO-block statement takes
    ACCESS EXCLUSIVE before its own IF NOT EXISTS check, even as a
    no-op — which is why the whole run is skipped rather than
    pre-checked statement by statement (the pre-checks would duplicate
    every guard in this file).

    The stamp asserts THIS file ran to completion under these exact
    bytes; it does not certify later catalog state. An out-of-band
    schema mutation (a hand-dropped column) needs
    `DELETE FROM schema_stamp` to force a re-apply — the same
    convention as DELETE FROM ingest_derived_state after out-of-band
    record mutations.

    Lock waits are bounded: the migration transaction sets lock_timeout
    (the advisory boot lock included). On expiry the transaction aborts
    WHOLE — one transaction, so the DDL prefix and the stamp write are
    discarded together and no half-applied schema survives — the
    advisory lock is released, and the boot fails with a clear error.
    systemd (Restart=always, RestartSec=5) retries the boot until the
    blocking transaction ends; the first boot after that converges.

    Executed through psycopg rather than shelling out to psql: the
    service already holds a connection with the right credentials, and a
    psql subprocess would add a PATH dependency the container need not
    satisfy.

    ROLLBACK IS ONE-DIRECTIONAL, and that is the accepted cost. Deploying
    forward then restarting an older binary leaves it running against a
    schema from the future. Every migration here is additive and
    nullable, with ONE allowed exception: that primary-key swap on
    usage_rollup. It is a guarded, idempotent constraint WIDENING of
    derived, DELETE+INSERT-rebuilt state, and the widened key is a
    superset of the old one, so an older binary's named-column INSERT
    still satisfies it. On a rolled-back binary, READS ignore what they
    do not know about, and its INGEST is guarded instead: a file whose
    stored parser_version is newer than the binary's own is never
    reparsed, so newer-column values survive a rollback. Erasure already
    committed by binaries older than that guard is not repaired by it.
    Reads tolerating a future schema plus the ingest guard are what make
    auto-apply safe, and any migration beyond those -- one that drops or
    retypes a column, or a second exception -- would break it.

    An older binary never touches the stamp row (its code predates the
    table), so a rollback changes nothing there: the old DDL is an
    additive subset of the stamped file's, and the next boot of THIS
    build fast-paths again.
    """
    ddl = read_schema()
    stamp = hashlib.sha256(ddl.encode("utf-8")).hexdigest()
    with viz_conn() as c:
        if _stamp_version(c) == stamp:
            # Current: the block exit ends the read transaction. No
            # DDL, no exclusive lock anywhere (issue #387).
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
                # Issue #154: the unlock (and the commit ending its
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
    JSONB (issue #368: a table keyed by another name passed the old
    check and 500ed every login with UndefinedColumn). An information-
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
            # An unseen column has three causes (issue #122) —
            # discriminate on this same connection before naming one.
            exists = c.execute(
                "SELECT to_regclass('public.users')"
            ).fetchone()
            if exists is None or exists[0] is None:
                raise RuntimeError(
                    "auth DB: no 'users' table visible — is "
                    "DATABASE_URL_AUTH pointing at the right database?"
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
