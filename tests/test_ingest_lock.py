"""Ingest concurrency: the db-wide advisory lock and the cooperative abort.

The lock tests moved here from test_ingest.py (they serialise the suite
rather than exercise its parsing behaviour, and need only the scratch
database, the mini mirror and the advisory-lock key); the abort tests of
issue #103 were written for test_ingest.py and landed here so that
module stays under pylint's line cap without its old pragma. The
fixtures they all share come from test_ingest.
"""
import logging
import os
import threading
import time
import types
from contextlib import closing, contextmanager

import psycopg
import pytest
# The fixtures register on import; pylint only sees names nobody calls.
from test_ingest import (  # pylint: disable=unused-import
    _fresh_db_fixture, _mini_r2_env_fixture, _scalar,
)

from backend import db, ingest


def _wait_db_lock_free(timeout_s: float = 10.0) -> None:
    """Poll until no backend holds _INGEST_LOCK_KEY.

    pg_terminate_backend is asynchronous: the signal lands, the backend
    exits, and only then does the server release its session locks. A test
    that proceeds immediately would race the release, not the feature.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with closing(psycopg.connect(os.environ["DATABASE_URL_VIZ"],
                                     autocommit=True)) as probe:
            row = probe.execute(  # pylint: disable=no-member
                "SELECT pg_try_advisory_lock(%s)",
                (ingest._INGEST_LOCK_KEY,)).fetchone()  # pylint: disable=protected-access
            assert row is not None
            if row[0]:
                probe.execute(  # pylint: disable=no-member
                    "SELECT pg_advisory_unlock(%s)",
                    (ingest._INGEST_LOCK_KEY,))  # pylint: disable=protected-access
                return
        time.sleep(0.05)
    pytest.fail("_INGEST_LOCK_KEY never became free after the holder died")


def test_ingest_skips_when_another_instance_holds_the_db_lock(
        fresh_db, mini_r2_env):
    """Serialization was per-process only (_RUN_LOCK): two app instances on
    one database ran their ingests concurrently — one rollup rebuild crashed
    with UniqueViolation, both wrote ingest_runs rows. When ANOTHER instance
    holds the db-wide advisory lock, a run must decline in the same dict
    shape as the in-process skip, naming the other instance."""
    with closing(psycopg.connect(os.environ["DATABASE_URL_VIZ"],
                                 autocommit=True)) as holder:
        row = holder.execute(  # pylint: disable=no-member
            "SELECT pg_try_advisory_lock(%s)",
            (ingest._INGEST_LOCK_KEY,)).fetchone()  # pylint: disable=protected-access
        assert row is not None
        assert row[0] is True, "the fixture itself must acquire the lock"
        try:
            result = ingest.run_ingest(trigger="manual")
        finally:
            holder.execute(  # pylint: disable=no-member
                "SELECT pg_advisory_unlock(%s)",
                (ingest._INGEST_LOCK_KEY,))  # pylint: disable=protected-access

    assert result["skipped"] is True, result
    assert "another instance" in result["reason"], result

    after = ingest.run_ingest(trigger="manual")
    assert after.get("skipped") is not True, after
    assert after["error"] is None, after


def test_ingest_lock_released_after_a_run(fresh_db, mini_r2_env):
    """The lock connection is dedicated and closed after every run, so a
    fresh connection — another instance's next run — can take the lock."""
    ingest.run_ingest(trigger="manual")

    with closing(psycopg.connect(os.environ["DATABASE_URL_VIZ"],
                                 autocommit=True)) as probe:
        row = probe.execute(  # pylint: disable=no-member
            "SELECT pg_try_advisory_lock(%s)",
            (ingest._INGEST_LOCK_KEY,)).fetchone()  # pylint: disable=protected-access
        assert row is not None
        assert row[0] is True, "a completed run left the lock held"
        probe.execute(  # pylint: disable=no-member
            "SELECT pg_advisory_unlock(%s)",
            (ingest._INGEST_LOCK_KEY,))  # pylint: disable=protected-access


def test_ingest_lock_released_when_holder_dies(fresh_db, mini_r2_env):
    """A crashed instance must never leave the ingest permanently locked.
    The advisory lock is session-scoped: the SERVER releases it when the
    holding connection dies, so a surviving instance's next run proceeds
    with no unlock step ever having run."""
    holder = psycopg.connect(os.environ["DATABASE_URL_VIZ"], autocommit=True)
    try:
        row = holder.execute(  # pylint: disable=no-member
            "SELECT pg_try_advisory_lock(%s)",
            (ingest._INGEST_LOCK_KEY,)).fetchone()  # pylint: disable=protected-access
        assert row is not None
        assert row[0] is True, "the fixture itself must acquire the lock"
        pid_row = holder.execute(  # pylint: disable=no-member
            "SELECT pg_backend_pid()").fetchone()
        assert pid_row is not None

        with closing(psycopg.connect(os.environ["DATABASE_URL_VIZ"],
                                     autocommit=True)) as killer:
            killed = killer.execute(  # pylint: disable=no-member
                "SELECT pg_terminate_backend(%s)",
                (pid_row[0],)).fetchone()
        assert killed is not None
        assert killed[0] is True, "pg_terminate_backend refused the pid"

        _wait_db_lock_free()
    finally:
        holder.close()  # pylint: disable=no-member

    result = ingest.run_ingest(trigger="manual")
    assert result.get("skipped") is not True, result
    assert result["error"] is None, result


# ---------------------------------------------------------------------------
# Unlock failure must not mask the run's own error (issue #154): the unlock
# in _db_run_lock's inner finally used to run bare, so a lock connection
# that died during the run raised at exit and REPLACED the body's exception
# at the caller. The guard swallows it — a session-scoped advisory lock is
# released by the server at session death, so the swallow can never leave a
# stale lock, and the dedicated connection's close() in the outer finally
# ends the session on every exit path.
# ---------------------------------------------------------------------------


class _DeadAtUnlockConn:
    """A lock connection that dies at the unlock: the try-lock answers,
    every unlock raises the way a severed session would."""

    def __init__(self):
        self.closed = False
        self.unlock_calls = 0

    def execute(self, sql, params=None):
        if "pg_advisory_unlock" in sql:
            self.unlock_calls += 1
            raise RuntimeError("server closed the connection unexpectedly")
        return types.SimpleNamespace(fetchone=lambda: (True,))

    def close(self):
        self.closed = True


def _dead_at_unlock_psycopg(monkeypatch, conn):
    """Point backend.ingest's psycopg.Connection.connect at `conn`.

    Replaces the module reference INSIDE backend.ingest only, so no other
    code path in the process sees the stand-in.
    """
    monkeypatch.setattr(
        ingest, "psycopg",
        types.SimpleNamespace(
            Connection=types.SimpleNamespace(
                connect=lambda url, autocommit=False: conn)))


def test_db_run_lock_never_masks_the_body_error(monkeypatch):
    """A lock connection that dies during the run raised at the inner
    finally's unlock, and that failure REPLACED the body's exception at
    the caller. The caller must see THE BODY'S exception, with the unlock
    still attempted and the connection still closed."""
    conn = _DeadAtUnlockConn()
    _dead_at_unlock_psycopg(monkeypatch, conn)

    with pytest.raises(RuntimeError, match="the run itself failed"):
        with ingest._db_run_lock() as acquired:  # pylint: disable=protected-access
            assert acquired is True
            raise RuntimeError("the run itself failed")

    assert conn.unlock_calls == 1, "the unlock must still be attempted"
    assert conn.closed, "the connection must still be closed"


def test_db_run_lock_swallows_unlock_failure_after_a_successful_run(
        monkeypatch, caplog):
    """Same failure with a clean run: swallowed and logged at warning, so
    the caller sees the body's success. Raising here would fail a run
    whose work is already done over bookkeeping whose lock is gone either
    way — the session-scoped lock dies with the dedicated connection."""
    conn = _DeadAtUnlockConn()
    _dead_at_unlock_psycopg(monkeypatch, conn)

    with caplog.at_level(logging.WARNING, logger="claudit.ingest"):
        with ingest._db_run_lock() as acquired:  # pylint: disable=protected-access
            assert acquired is True

    assert conn.unlock_calls == 1
    assert conn.closed
    logged = [r for r in caplog.records
              if r.name == "claudit.ingest" and r.levelno >= logging.WARNING]
    assert logged, "a swallowed unlock failure must be logged, not silent"
    assert "server closed the connection" in logged[0].getMessage()


# ---------------------------------------------------------------------------
# Cooperative abort (issue #103): a shutdown request sets _SHUTDOWN; a run
# stops at its next bounded step, closes its ingest_runs row as aborted, and
# skips the derived-state rebuild, the ingest_done broadcast and the cache
# warm. wait_for_run bounds lifespan teardown's wait on the in-flight run.
# ---------------------------------------------------------------------------


def test_run_ingest_skips_when_shutdown_requested(fresh_db, mini_r2_env):
    """A run arriving after shutdown was requested must skip without
    opening an ingest_runs row — a queued cron tick can fire while
    lifespan teardown is already asking every run to stop."""
    ingest._SHUTDOWN.set()  # pylint: disable=protected-access
    try:
        result = ingest.run_ingest("manual")
    finally:
        ingest._SHUTDOWN.clear()  # pylint: disable=protected-access

    assert result["skipped"] is True, result
    assert result["reason"] == "shutdown requested", result
    with db.viz_conn() as c:
        assert _scalar(c, "SELECT COUNT(*) FROM ingest_runs") == 0


def test_run_ingest_skips_when_shutdown_lands_during_lock_take(
        fresh_db, mini_r2_env, monkeypatch):
    """The re-check AFTER both locks are acquired ("landed while waiting
    on the locks") had no test (issue #154): a regression there would pass
    the suite. Setting the event INSIDE _db_run_lock — while the locks are
    being taken — must yield the skip dict and open no ingest_runs row,
    exactly like a shutdown that landed before the run was attempted."""
    real = ingest._db_run_lock  # pylint: disable=protected-access

    @contextmanager
    def shutdown_during_take():
        ingest._SHUTDOWN.set()  # pylint: disable=protected-access
        with real() as db_acquired:
            yield db_acquired

    monkeypatch.setattr(ingest, "_db_run_lock", shutdown_during_take)
    try:
        result = ingest.run_ingest("manual")
    finally:
        ingest._SHUTDOWN.clear()  # pylint: disable=protected-access

    assert result["skipped"] is True, result
    assert result["reason"] == "shutdown requested", result
    with db.viz_conn() as c:
        assert _scalar(c, "SELECT COUNT(*) FROM ingest_runs") == 0


def test_ingest_aborts_mid_run(fresh_db, mini_r2_env, monkeypatch):
    """A shutdown requested mid-walk aborts the run: the ingest_runs row
    closes with finished_at set and an "aborted" error; the derived-state
    rebuild, the ingest_done broadcast and the cache warm are all skipped,
    and /health's progress readout returns to idle."""
    real = ingest._fetch_parse_persist  # pylint: disable=protected-access

    def abort_after_listing(todo, parser_version, failed, seen_keys):
        ingest._SHUTDOWN.set()  # pylint: disable=protected-access
        return real(todo, parser_version, failed, seen_keys)

    monkeypatch.setattr(ingest, "_fetch_parse_persist", abort_after_listing)
    rebuilt: list[int] = []
    monkeypatch.setattr(ingest, "_rebuild_derived_state",
                        lambda: rebuilt.append(1))
    broadcasts: list[tuple] = []
    monkeypatch.setattr(ingest.events, "broadcast_threadsafe",
                        lambda *args, **kwargs: broadcasts.append(args))

    try:
        result = ingest.run_ingest("manual")
    finally:
        ingest._SHUTDOWN.clear()  # pylint: disable=protected-access

    assert result.get("aborted") is True, result
    assert "aborted" in result["error"], result
    with db.viz_conn() as c:
        row = c.execute(
            "SELECT finished_at, error FROM ingest_runs "
            "ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None
    assert row[0] is not None, "an aborted run must still close its row"
    assert "aborted" in row[1], row[1]
    assert not rebuilt, "an aborted run must not rebuild derived state"
    assert not broadcasts, "an aborted run must not broadcast ingest_done"
    assert ingest.progress_snapshot()["phase"] == "idle"


def test_abort_releases_the_db_lock(fresh_db, mini_r2_env, monkeypatch):
    """The abort unwinds through _db_run_lock's finally, so a fresh
    connection — another instance's next run — can take the advisory
    lock again."""
    real = ingest._fetch_parse_persist  # pylint: disable=protected-access

    def abort_after_listing(todo, parser_version, failed, seen_keys):
        ingest._SHUTDOWN.set()  # pylint: disable=protected-access
        return real(todo, parser_version, failed, seen_keys)

    monkeypatch.setattr(ingest, "_fetch_parse_persist", abort_after_listing)
    try:
        result = ingest.run_ingest("manual")
    finally:
        ingest._SHUTDOWN.clear()  # pylint: disable=protected-access

    assert result.get("aborted") is True, result
    with closing(psycopg.connect(os.environ["DATABASE_URL_VIZ"],
                                 autocommit=True)) as probe:
        row = probe.execute(  # pylint: disable=no-member
            "SELECT pg_try_advisory_lock(%s)",
            (ingest._INGEST_LOCK_KEY,)  # pylint: disable=protected-access
        ).fetchone()
        assert row is not None
        assert row[0] is True, "an aborted run left the advisory lock held"
        probe.execute(  # pylint: disable=no-member
            "SELECT pg_advisory_unlock(%s)",
            (ingest._INGEST_LOCK_KEY,))  # pylint: disable=protected-access


def test_rebuild_phase_aborts(fresh_db, mini_r2_env, monkeypatch):
    """A shutdown landing during the derived-state rebuild stops after the
    phase in flight: later rebuilds are skipped, the row still closes as
    aborted, and nothing tells clients data changed. The abort lands AFTER
    the walk, so files DID change (this first run inserts every mirror
    file) — the broadcast, the invalidation and the warm are suppressed by
    `not aborted` alone, which is what the spies here pin."""
    real = ingest.rebuild_rollup

    def aborting_rollup():
        ingest._SHUTDOWN.set()  # pylint: disable=protected-access
        real()

    monkeypatch.setattr(ingest, "rebuild_rollup", aborting_rollup)
    later: list[int] = []
    monkeypatch.setattr(ingest, "rebuild_tool_rollup", lambda: later.append(1))
    broadcasts: list[tuple] = []
    monkeypatch.setattr(ingest.events, "broadcast_threadsafe",
                        lambda *args, **kwargs: broadcasts.append(args))
    invalidated: list[int] = []
    monkeypatch.setattr(ingest.cache.response_cache, "invalidate",
                        lambda: invalidated.append(1))
    warmed: list[int] = []
    monkeypatch.setattr(ingest, "warm_common", lambda: warmed.append(1))

    try:
        summary = ingest.run_ingest_locked("manual")
    finally:
        ingest._SHUTDOWN.clear()  # pylint: disable=protected-access

    assert summary.get("aborted") is True, summary
    assert "aborted" in summary["error"], summary
    assert not later, "rebuilds after the abort point must be skipped"
    assert not broadcasts, "an aborted run must not broadcast ingest_done"
    assert not invalidated, "an aborted run must not mark responses stale"
    assert not warmed, "an aborted run must not warm the cache"
    with db.viz_conn() as c:
        row = c.execute(
            "SELECT finished_at, error FROM ingest_runs WHERE id = %s",
            (summary["id"],)).fetchone()
    assert row is not None
    assert row[0] is not None, "the aborted rebuild must still close its row"
    assert "aborted" in row[1], row[1]


def test_wait_for_run(fresh_db, mini_r2_env):
    """wait_for_run reports False while a run holds _RUN_LOCK and True
    once it is free — immediate when idle, so lifespan teardown never
    stalls a shutdown that has nothing to wait for."""
    assert ingest.wait_for_run(0.1) is True, "idle: the lock is free"

    started = threading.Event()
    release = threading.Event()
    seen = {}
    real = ingest.run_ingest_locked

    def slow(trigger):
        started.set()
        release.wait(timeout=30)
        return real(trigger)

    ingest.run_ingest_locked = slow
    try:
        t = threading.Thread(
            target=lambda: seen.update(first=ingest.run_ingest("startup")))
        t.start()
        assert started.wait(timeout=10), "first run never entered"
        assert ingest.wait_for_run(0.2) is False, (
            "a run in flight holds the lock")
        release.set()
        t.join(timeout=60)
    finally:
        ingest.run_ingest_locked = real

    assert seen["first"].get("skipped") is not True, seen
    assert ingest.wait_for_run(0.1) is True, "the run released the lock"
