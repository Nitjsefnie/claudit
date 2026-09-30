"""Liveness guard for the db-wide ingest advisory lock (issue #374).

The advisory lock is session-scoped: the server releases it when the
holding connection dies. `_db_run_lock` opens that connection once and
trusts it for the whole run, so a session the server ends mid-run (a
pg_terminate_backend, a database restart) left the run going WITHOUT the
lock — and a second instance on the same database could then start a
concurrent ingest, the exact failure the lock exists to prevent (issue
#102). The run's bounded steps consult this guard, and unwind via
IngestAborted with "lost ingest lock", which the run row records.
"""
from __future__ import annotations

import psycopg

from backend.ingest_reprice import IngestAborted

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
