"""Crash recovery: the startup sweeps for what a SIGKILL leaves behind.

A SIGKILL never runs the lifespan teardown, so the graceful paths (#372
for the run row, #363/#414 for the export children) cannot close what it
leaves behind. #440: the crashed run's ingest_runs row stayed open
forever — the sweep at the top of the next run closes it, and holding
the db-wide advisory lock at that moment is the proof the opener's
process is gone. #441: the crashed export's partial claudit_export_*
PNG stayed in the tmp forever — the lifespan startup sweep unlinks it,
aged past any live render's lifetime.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
# The fixtures register on import; pylint only sees names nobody calls.
from test_ingest import (  # pylint: disable=unused-import
    _fresh_db_fixture, _mini_r2_env_fixture, _scalar,
)

import backend.app as app_mod
from backend import api_export, db, ingest

_CRASH_LABEL = "aborted: previous run died without closing (crash)"
_SHUTDOWN_LABEL = "aborted: shutdown requested"


def _insert_open_run(started_before_s: int = 3600) -> int:
    """Book a crash-shaped leftover: an open row, started in the past."""
    with db.viz_conn() as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO ingest_runs (started_at, trigger) VALUES (%s, %s) "
            "RETURNING id",
            (datetime.now(timezone.utc) - timedelta(seconds=started_before_s),
             "startup"),
        )
        run_row = cur.fetchone()
        assert run_row is not None  # INSERT ... RETURNING always yields
        run_id = run_row[0]
        c.commit()
    return run_id


def test_sweep_stale_runs_closes_only_open_rows(fresh_db):
    """The sweep closes a crash-shaped leftover with the crash label, and
    never rewrites a row a run already closed (its counters and error are
    the run's own verdict — the sweep is not a reaper of history)."""
    done_finished = datetime.now(timezone.utc) - timedelta(minutes=59)
    with db.viz_conn() as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO ingest_runs (started_at, finished_at, trigger, "
            "error) VALUES (%s, %s, %s, %s) RETURNING id",
            (datetime.now(timezone.utc) - timedelta(hours=1), done_finished,
             "cron", "closed by the run"),
        )
        done_row = cur.fetchone()
        assert done_row is not None
        done_id = done_row[0]
        stale_id = _insert_open_run()
        c.commit()

    # The plant is asserted before the sweep: a failed plant must fail
    # here, not read as a sweep verdict below.
    with db.viz_conn() as c:
        planted = c.execute(
            "SELECT finished_at, error FROM ingest_runs WHERE id = %s",
            (stale_id,)).fetchone()
    assert planted is not None and planted[0] is None and planted[1] is None

    assert ingest.sweep_stale_runs() == 1
    with db.viz_conn() as c:
        done = c.execute(
            "SELECT finished_at, error FROM ingest_runs WHERE id = %s",
            (done_id,)).fetchone()
        stale = c.execute(
            "SELECT finished_at, error FROM ingest_runs WHERE id = %s",
            (stale_id,)).fetchone()
    assert done is not None and stale is not None
    assert done == (done_finished, "closed by the run")
    assert stale[0] is not None
    assert stale[1] == _CRASH_LABEL
    assert stale[1] != _SHUTDOWN_LABEL
    assert ingest.sweep_stale_runs() == 0


def test_next_run_sweeps_the_leftover_before_booking(fresh_db, mini_r2_env):
    """The sweep runs inside the run lock, before the new row is booked:
    holding the db-wide advisory lock proves any open row's process is
    gone (the server released the lock the moment that connection died).
    After the run, the leftover reads as aborted-by-crash and the new
    run's own row is the newest, closed with no error."""
    stale_id = _insert_open_run()

    summary = ingest.run_ingest(trigger="manual")
    assert summary["error"] is None
    with db.viz_conn() as c:
        stale = c.execute(
            "SELECT finished_at, error FROM ingest_runs WHERE id = %s",
            (stale_id,)).fetchone()
        fresh = c.execute(
            "SELECT started_at FROM ingest_runs WHERE id = %s",
            (summary["id"],)).fetchone()
    assert stale is not None and fresh is not None
    assert stale[0] is not None and stale[1] == _CRASH_LABEL
    assert stale[0] <= fresh[0]
    assert ingest.sweep_stale_runs() == 0


# ---------------------------------------------------------------------------
# #441: the export tmp sweep.
# ---------------------------------------------------------------------------


def test_sweep_stale_exports_removes_only_aged_orphans(tmp_path):
    """A claudit_export_* file older than any live render (the render
    subprocess is bounded at _EXPORT_TIMEOUT_S and the handler unlinks on
    every exit path) is an orphan of a crashed process; anything younger
    may be another process's live export in a shared tmp, and anything
    outside the export prefix, or unstatable, is left alone."""
    now = time.time()
    old = tmp_path / "claudit_export_old.png"
    young = tmp_path / "claudit_export_young.png"
    other = tmp_path / "claudit_other.png"
    for path in (old, young, other):
        path.write_bytes(b"\x89PNG\r\n")
    os.utime(old, (now - 10_000, now - 10_000))
    os.utime(young, (now - 10, now - 10))
    os.utime(other, (now - 10_000, now - 10_000))

    # Same live-oracle rule: every file is provably present before the
    # sweep, so the "kept" assertions below cannot pass on a failed plant.
    for path in (old, young, other):
        assert path.exists()

    assert api_export.sweep_stale_exports(directory=str(tmp_path)) == 1

    assert not old.exists()
    assert young.exists()
    assert other.exists()


@pytest.mark.asyncio
async def test_lifespan_sweeps_stale_exports_at_startup(monkeypatch):
    """The export sweep rides the same startup pass, before the scheduler
    can book the startup ingest — the same convergence-at-boot contract
    the run-row sweep meets from inside the first run."""
    calls: list = []
    _stub_lifespan_env(monkeypatch, calls)
    monkeypatch.setattr(
        app_mod.api_export, "sweep_stale_exports",
        lambda directory=None: calls.append("sweep_exports") or 0)

    async with app_mod.lifespan(FastAPI()):
        pass

    assert calls.index("sweep_exports") < calls.index("sched_start")


def _stub_lifespan_env(monkeypatch, calls: list) -> None:
    """Silence the lifespan's schema, scheduler and events surfaces (the
    teardown path is asserted nowhere here; the stubs keep module state
    clean for the next test)."""
    class _Scheduler:
        def __init__(self, **_kwargs):
            pass

        def add_job(self, *_args, **_kwargs):
            pass

        def start(self):
            calls.append("sched_start")

        def shutdown(self, *, wait):
            pass

    monkeypatch.setattr(app_mod.db, "apply_schema", lambda: None)
    monkeypatch.setattr(app_mod.db, "schema_check", lambda: None)
    monkeypatch.setattr(app_mod.db, "cancel_viz_queries", lambda: None)
    monkeypatch.setattr(app_mod, "BackgroundScheduler", _Scheduler)
    monkeypatch.setattr(app_mod.events, "set_loop", lambda _loop: None)
    monkeypatch.setattr(app_mod.events, "signal_shutdown", lambda: None)
    monkeypatch.setattr(app_mod.events, "clear_loop", lambda: None,
                        raising=False)
    monkeypatch.setattr(app_mod.ingest, "_SHUTDOWN", threading.Event())
    # The teardown must never reach the real fallback close: the real one
    # reads progress state the last DB test may have left behind.
    monkeypatch.setattr(app_mod.ingest, "close_open_run",
                        lambda: calls.append("close_row"))
    monkeypatch.setattr(app_mod.ingest, "wait_for_run", lambda _t: False)
    monkeypatch.setattr(app_mod.ingest, "clear_shutdown",
                        lambda: calls.append("clear"))
