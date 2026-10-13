"""Liveness guard for the db-wide ingest advisory lock (issue #374).

The advisory lock is session-scoped: the server releases it when the
holding connection dies. `_db_run_lock` opens that connection once and
trusts it for the whole run, so a session the server ends mid-run (a
pg_terminate_backend, a database restart) left the run going WITHOUT the
lock — and a second instance on the same database could then start a
concurrent ingest, the exact failure the lock exists to prevent (issue
#102). The run's bounded steps consult this guard, and unwind via
IngestAborted with "lost ingest lock", which the run row records.

The lock itself lives here too — `_db_run_lock` below takes it on its
dedicated connection and arms this guard for the whole run — so the lock
and its liveness guard share one module. `backend.ingest` re-exports
`_db_run_lock` and `_INGEST_LOCK_KEY`, keeping the established call
surface (`ingest._db_run_lock(...)`, `ingest._INGEST_LOCK_KEY`) intact.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg

from backend.ingest_reprice import IngestAborted
from backend.env_config import require_env

log = logging.getLogger("claudit.ingest")

# Advisory-lock key for the db-wide ingest lock (_db_run_lock below). A
# sibling of db._SCHEMA_LOCK_KEY (0x5356_4D49) — same prefix, next value —
# so the schema migration and an ingest run can never take each other's
# lock on one database.
_INGEST_LOCK_KEY = 0x5356_4D4A

# The dedicated lock connection and the advisory-lock key it holds, stashed
# by ingest._db_run_lock around the production body; a test double may take
# the slot instead. _RUN_LOCK admits one run at a time per process, so a
# one-slot holder cannot interleave two runs.
_holder: dict = {"conn": None, "key": None}


def hold(conn, key: int) -> None:
    """Arm the guard with the lock connection for the run in flight."""
    _holder["conn"] = conn
    _holder["key"] = key


def release() -> None:
    """Disarm the guard on every exit path through the run body."""
    _holder["conn"] = None
    _holder["key"] = None


def check_lock_alive() -> None:
    """Raise IngestAborted when the advisory lock is no longer held.

    The dedicated connection is both the lock holder and the probe: a
    session the server has ended fails the query, a live session that lost
    the lock finds no pg_locks row. No run in flight (guard unarmed)
    passes silently — warm and test callers consult it outside a run.
    """
    conn, key = _holder["conn"], _holder["key"]
    if conn is None:
        return
    if conn.closed:
        raise IngestAborted("lost ingest lock")
    try:
        row = conn.execute(
            "SELECT 1 FROM pg_locks WHERE locktype = 'advisory' "
            "AND pid = pg_backend_pid() "
            "AND classid = %s AND objid = %s",
            ((key >> 32) & 0xFFFFFFFF, key & 0xFFFFFFFF),
        ).fetchone()
    except psycopg.Error as exc:
        raise IngestAborted("lost ingest lock") from exc
    if row is None:
        raise IngestAborted("lost ingest lock")


@contextmanager
def _db_run_lock() -> Iterator[bool]:
    """Try the db-wide ingest advisory lock on a DEDICATED connection.

    `ingest._RUN_LOCK` serialises one process; two app instances sharing
    one database still ran their ingests concurrently (issue #102: two
    runs 2.4 ms apart, one rollup rebuild UniqueViolation, two
    ingest_runs rows). pg_try_advisory_lock is session-scoped, so the
    server releases it when the holding connection dies — a crashed
    instance can never leave the ingest locked.

    The connection exists ONLY to hold the lock: opened here, closed in
    the outer finally on every exit path (success, fatal error, and any
    abort unwinding through the yield), so no pooled connection ever
    returns to the pool with the lock still held.

    Yields False when another instance holds it — a skipped run is correct
    behaviour, not an error. A connect failure propagates, the same as
    today's behaviour when the database is down.
    """
    conn = psycopg.Connection.connect(
        require_env("CLAUDIT_DATABASE_URL_VIZ"), autocommit=True)
    try:
        row = conn.execute(  # pylint: disable=no-member
            "SELECT pg_try_advisory_lock(%s)", (_INGEST_LOCK_KEY,)
        ).fetchone()
        assert row is not None  # SELECT always yields exactly one row
        acquired = bool(row[0])
        hold(conn, _INGEST_LOCK_KEY)
        try:
            yield acquired
        finally:
            release()
            if acquired:
                # Issue #154: the unlock must never mask the body's error;
                # the swallow leaves no stale lock either way — the expected
                # failure is the lock session dying mid-run, where the
                # server releases a session-scoped advisory lock at session
                # death, and a still-live session is ended by the close()
                # in the outer finally on every exit path regardless.
                try:
                    conn.execute(  # pylint: disable=no-member
                        "SELECT pg_advisory_unlock(%s)", (_INGEST_LOCK_KEY,))
                except Exception as exc:
                    log.warning(
                        "ingest advisory-lock unlock failed; the lock is "
                        "released server-side when the lock session dies, "
                        "and a still-live session is ended by the close() "
                        "in the outer finally; any in-flight run error is "
                        "preserved: %s", exc)
    finally:
        conn.close()  # pylint: disable=no-member
