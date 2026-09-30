"""The lifespan teardown's stop budget against the shipped unit (issue #410).

uvicorn spends `--timeout-graceful-shutdown` draining connections BEFORE the
lifespan teardown runs, and systemd SIGKILLs at `TimeoutStopSec`. The teardown's
bounded wait on the in-flight run, plus the fallback row close behind it, has to
fit in what is left -- a wait sized past it means systemd kills the process
while the row is still open, which is the finished_at NULL this pins shut.
"""
from __future__ import annotations

import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
import pytest
# The fixtures register on import; pylint only sees names nobody calls here.
from test_ingest import (  # pylint: disable=unused-import
    _fresh_db_fixture,
)

import backend.app as app_mod
from backend import constants, db, ingest

_UNIT = (Path(__file__).resolve().parent.parent
         / "examples" / "claudit.service")

# At least this much of the unit's stop budget must survive the graceful window
# and the wait, for the fallback close (one single-row UPDATE) and the tail of
# the teardown to land before systemd's SIGKILL. The code's own margin is wider;
# this is the floor below which the shape is wrong, not a second source of truth.
_MIN_FALLBACK_MARGIN_S = 1.0


def _unit_text() -> str:
    """The shipped unit, continuations joined so the flags read as one line."""
    return re.sub(r"\\\n\s*", " ", _UNIT.read_text(encoding="utf-8"))


def _unit_stop_budget() -> tuple[int, int]:
    """(graceful seconds, TimeoutStopSec) as the shipped unit sets them."""
    text = _unit_text()
    graceful = re.search(r"--timeout-graceful-shutdown (\d+)", text)
    stop = re.search(r"^TimeoutStopSec=(\d+)", text, re.M)
    assert graceful is not None, (
        f"{_UNIT.name} sets no --timeout-graceful-shutdown for uvicorn")
    assert stop is not None, f"{_UNIT.name} sets no TimeoutStopSec"
    return int(graceful.group(1)), int(stop.group(1))


def _stub_lifespan(monkeypatch) -> None:
    """Silence the lifespan's schema, scheduler and events surfaces.

    apply_schema/schema_check would need the scratch database the row tests
    below use deliberately; the real events functions would mutate module state
    the next test reads.
    """
    class _Scheduler:
        def __init__(self, **_kwargs):
            pass

        def add_job(self, *_args, **_kwargs):
            pass

        def start(self):
            pass

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
    # A throwaway abort request: the teardown must not leave the real module
    # event set for the next test.
    monkeypatch.setattr(app_mod.ingest, "_SHUTDOWN", threading.Event())


async def _teardown() -> None:
    async with app_mod.lifespan(FastAPI()):
        pass


# ---------------------------------------------------------------------------
# The arithmetic: the wait the teardown actually uses, against the unit that
# kills the process.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_teardown_wait_leaves_room_for_the_fallback_close(monkeypatch):
    """The bounded wait plus uvicorn's graceful window must leave the
    fallback close inside the unit's TimeoutStopSec.

    The wait the teardown passes is captured by running the real lifespan, so
    this fails for a wait sized past the budget no matter where the number is
    written — a literal in app.py or a derived constant.
    """
    _stub_lifespan(monkeypatch)
    used: list[float] = []
    monkeypatch.setattr(app_mod.ingest, "wait_for_run",
                        lambda t: used.append(t) or True)
    monkeypatch.setattr(app_mod.ingest, "close_open_run", lambda: None)

    await _teardown()

    assert len(used) == 1, f"the teardown waited {len(used)} times"
    graceful, stop = _unit_stop_budget()
    left = stop - (graceful + used[0])
    assert left >= _MIN_FALLBACK_MARGIN_S, (
        f"uvicorn spends {graceful}s draining before the teardown starts, the "
        f"teardown then waits {used[0]}s, so only {left}s of the unit's "
        f"TimeoutStopSec={stop}s is left for the fallback row close — systemd "
        f"SIGKILLs first and finished_at stays NULL")


def test_stop_budget_constants_match_the_shipped_unit():
    """The wait is derived from the unit's own numbers, so it can only stay
    honest while the constants and the unit agree.

    Someone raising TimeoutStopSec, or dropping the graceful flag, must come
    back here rather than leave the wait sized against a window that moved.
    """
    graceful, stop = _unit_stop_budget()
    assert constants.SHUTDOWN_GRACEFUL_S == graceful, (
        "the graceful window uvicorn spends before the lifespan teardown runs")
    assert constants.SHUTDOWN_STOP_BUDGET_S == stop, "the unit's TimeoutStopSec"
    assert constants.SHUTDOWN_RUN_WAIT_S > 0, (
        "the wait must be positive: the run it waits for still has to abort "
        "cooperatively and close its own row")
    assert (constants.SHUTDOWN_GRACEFUL_S + constants.SHUTDOWN_RUN_WAIT_S
            + constants.SHUTDOWN_MARGIN_S
            <= constants.SHUTDOWN_STOP_BUDGET_S), (
        "graceful + wait + margin must fit inside TimeoutStopSec")


# ---------------------------------------------------------------------------
# The two outcomes of the wait, against a real ingest_runs row.
# ---------------------------------------------------------------------------


def _open_run() -> int:
    run_id = ingest._open_run(  # pylint: disable=protected-access
        datetime.now(timezone.utc), "manual")
    ingest._set_progress(run_id=run_id)  # pylint: disable=protected-access
    return run_id


def _row(run_id: int):
    with db.viz_conn() as c:
        return c.execute(
            "SELECT finished_at, error, reparsed FROM ingest_runs "
            "WHERE id = %s", (run_id,)).fetchone()


@pytest.mark.asyncio
async def test_outliving_the_wait_closes_the_row_as_aborted(
        fresh_db, monkeypatch):
    """A run still unwinding when the wait expires gets its row closed as the
    abort it is, with finished_at written — the row the stop would otherwise
    leave NULL behind.

    The real lifespan teardown and the real row close, against a real row.
    """
    _stub_lifespan(monkeypatch)
    run_id = _open_run()
    monkeypatch.setattr(app_mod.ingest, "wait_for_run", lambda _t: False)
    try:
        await _teardown()
        row = _row(run_id)
    finally:
        ingest._set_progress(run_id=None)  # pylint: disable=protected-access

    assert row is not None
    assert row[0] is not None, "a stop must never leave finished_at NULL"
    assert row[1] == "aborted: shutdown requested", row[1]


@pytest.mark.asyncio
async def test_a_run_finishing_inside_the_wait_keeps_its_own_close(
        fresh_db, monkeypatch):
    """A run the wait freed closes its own row, with its own counters and no
    error. The fallback must not touch that row at all — an aborted marker
    over a clean close is what /health would read."""
    _stub_lifespan(monkeypatch)
    run_id = _open_run()
    fallback: list = []
    real_close = ingest.close_open_run
    monkeypatch.setattr(app_mod.ingest, "close_open_run",
                        lambda: fallback.append(1) or real_close())

    def finished_inside_the_wait(_timeout: float) -> bool:
        """Stand in for the run thread unwinding and closing inside the wait."""
        ingest._close_run(  # pylint: disable=protected-access
            run_id, datetime.now(timezone.utc), 7, 2, 3, 0, 0, None)
        return True

    monkeypatch.setattr(app_mod.ingest, "wait_for_run", finished_inside_the_wait)
    try:
        await _teardown()
        row = _row(run_id)
    finally:
        ingest._set_progress(run_id=None)  # pylint: disable=protected-access

    assert not fallback, "a successful wait must not run the fallback close"
    assert row is not None
    assert row[0] is not None
    assert row[1] is None, f"the run closed clean; {row[1]!r} overwrote it"
    assert row[2] == 2, f"the run's own counters must survive: {row!r}"
