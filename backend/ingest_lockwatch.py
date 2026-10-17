"""Session-scoped ingest advisory lock and liveness guard.

A lost connection releases the lock; the next guard check at a bounded step
raises IngestAborted, potentially after another instance acquires it.
The dedicated connection holds and probes the lock. ingest re-exports
_db_run_lock and _INGEST_LOCK_KEY for the established call surface.
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
    _holder["conn"] = conn
    _holder["key"] = key


def release() -> None:
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
    """Try the db-wide ingest advisory lock on a dedicated connection.

    A process-local lock cannot serialize app instances sharing one DB.
    The server releases this session lock when its connection dies, so
    crashes never strand it. Close the dedicated connection in finally
    on every exit path; a pooled connection must never retain the lock.

    Yield False when another instance owns it: skipping is correct, not
    an error. Connection failures propagate as database failures."""
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
                # The unlock must never mask the body's error;
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
