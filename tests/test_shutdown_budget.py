"""The lifespan teardown's stop budget against the shipped unit (#410, #414).

uvicorn spends `--timeout-graceful-shutdown` draining connections BEFORE the
lifespan teardown runs, and systemd SIGKILLs at `TimeoutStopSec`. EVERY term
the teardown can spend -- the render reap (#414), then the bounded wait on the
in-flight run, then the fallback row close behind it -- has to fit in what is
left. A term sized past its share means systemd kills the process while the
row is still open, which is the finished_at NULL this pins shut.
"""
from __future__ import annotations

import asyncio
import contextlib
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
import pytest
# The fixtures register on import; pylint only sees names nobody calls here.
from test_ingest import (  # pylint: disable=unused-import
    _fresh_db_fixture,
)

import backend.app as app_mod
from backend import api_export, constants, db, ingest

_UNIT = (Path(__file__).resolve().parent.parent
         / "examples" / "claudit.service")

# At least this much of the unit's stop budget must survive the graceful window
# and the wait, for the fallback close (one single-row UPDATE) and the tail of
# the teardown to land before systemd's SIGKILL. The code's own margin is wider;
# this is the floor below which the shape is wrong, not a second source of truth.
_MIN_FALLBACK_MARGIN_S = 1.0

# The reap bound test's own arithmetic. The bound is pinned STRUCTURALLY,
# not in wall time: the timeouts the reap passes asyncio.wait_for are
# recorded at that seam, and must sum to at most one budget — the per-child
# shape sums to N budgets. The numbers are argument floats, so runner load
# cannot flip the verdict (the +1e-9 in the comparison absorbs clock
# rounding; a per-child sum clears the ceiling by five orders of
# magnitude); a wall ceiling here read 0.61s of runner stall against a
# 0.5s bound on the Windows leg (issue #736). One catch the wall ceiling
# had is dropped deliberately: 0.5–5s of real whole-budget slowness that
# never passes the seam (a sleep ahead of the loop) is now invisible — a
# planted 0.6s sleep leaves both reap tests green — and that is the
# accepted price of a bound that cannot judge the shape on runner load.
_REAP_BUDGET_S = 0.2
_WEDGED_CHILDREN = 3


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


class _WedgedRender:
    """A render child the reap's SIGKILL never collects.

    `wait()` is what a killed-but-unreaped child does: it stays pending
    forever, so the reap's own bounded wait is the only thing that ends it.
    That is the shape issue #414 is about -- a per-child bound drawn N
    times is what overran the unit's stop budget.
    """

    returncode = None

    def __init__(self, pid: int = 4242):
        self.pid = pid
        self.killed = False

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        await asyncio.Event().wait()
        return 0  # pragma: no cover - the await never returns


def _wedged_render(pid: int = 4242) -> _WedgedRender:
    return _WedgedRender(pid)


class _ExitingRender:
    """A render child that is reaped normally, after a real delay.

    The other half of the deadline from `_WedgedRender`: a child that DOES
    exit, so a reap that skips the wait entirely is distinguishable from one
    that waits and is bounded.
    """

    def __init__(self, delay_s: float, pid: int = 4242):
        self.pid = pid
        self.killed = False
        self.reaped = False
        self.returncode = None
        self._delay_s = delay_s

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        await asyncio.sleep(self._delay_s)
        self.reaped = True
        self.returncode = -9
        return self.returncode


def _render_that_exits_after(delay_s: float) -> _ExitingRender:
    return _ExitingRender(delay_s)


@contextlib.contextmanager
def _live_renders(children: list):
    """Register fake children in the module's live-render table, then clear."""
    for child in children:
        api_export._live_renders[child] = (  # pylint: disable=protected-access
            "/nonexistent/claudit-export.png")
    try:
        yield
    finally:
        for child in children:
            api_export._live_renders.pop(  # pylint: disable=protected-access
                child, None)


# ---------------------------------------------------------------------------
# The arithmetic: the wait the teardown actually uses, against the unit that
# kills the process.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_teardown_wait_leaves_room_for_the_fallback_close(monkeypatch):
    """The bounded wait, plus everything ahead of it, must leave the fallback
    close inside the unit's TimeoutStopSec.

    The wait the teardown passes is captured by running the real lifespan, so
    this fails for a wait sized past the budget no matter where the number is
    written — a literal in app.py or a derived constant. The render reap is
    subtracted too: it runs BETWEEN the graceful window and this wait (that
    ordering is issue #414), so a wait that fits only by ignoring it is a wait
    that does not fit.
    """
    _stub_lifespan(monkeypatch)
    used: list[float] = []
    # None is the unfixed shape: a reap called with no budget at all.
    reaps: list[float | None] = []

    async def fake_reap(budget=None):
        reaps.append(budget)

    monkeypatch.setattr(app_mod.api_export, "reap_live_renders", fake_reap)
    monkeypatch.setattr(app_mod.ingest, "wait_for_run",
                        lambda t: used.append(t) or True)
    monkeypatch.setattr(app_mod.ingest, "close_open_run", lambda: None)

    await _teardown()

    assert len(used) == 1, f"the teardown waited {len(used)} times"
    assert len(reaps) == 1 and reaps[0] is not None, (
        f"the teardown reaped with {reaps!r}; the reap spends this window too")
    graceful, stop = _unit_stop_budget()
    left = stop - (graceful + reaps[0] + used[0])
    assert left >= _MIN_FALLBACK_MARGIN_S, (
        f"uvicorn spends {graceful}s draining before the teardown starts, the "
        f"render reap then takes {reaps[0]}s and the teardown waits {used[0]}s, "
        f"so only {left}s of the unit's TimeoutStopSec={stop}s is left for the "
        f"fallback row close — systemd SIGKILLs first and finished_at stays NULL")


def test_stop_budget_constants_match_the_shipped_unit():
    """Every term is derived from the unit's own numbers, so the split can
    only stay honest while the constants and the unit agree.

    Someone raising TimeoutStopSec, or dropping the graceful flag, must come
    back here rather than leave a term sized against a window that moved.

    The sum at the end is a TRIPWIRE, not the control on this budget: the wait
    is derived from the same numbers, so the sum holds by construction and can
    only fail if a term is hand-edited into a literal. The control is
    `test_the_terms_the_teardown_uses_sum_to_the_stop_budget`, which checks the
    values the teardown actually PASSES and so does not cancel.
    """
    graceful, stop = _unit_stop_budget()
    assert constants.SHUTDOWN_GRACEFUL_S == graceful, (
        "the graceful window uvicorn spends before the lifespan teardown runs")
    assert constants.SHUTDOWN_STOP_BUDGET_S == stop, "the unit's TimeoutStopSec"
    assert constants.SHUTDOWN_RENDER_REAP_S > 0, (
        "the render reap must be positive: a killed child is normally "
        "reaped instantly, and a zero budget gives up reaping every one")
    assert constants.SHUTDOWN_RUN_WAIT_S > 0, (
        "the wait must be positive: the run it waits for still has to abort "
        "cooperatively and close its own row")
    assert (constants.SHUTDOWN_GRACEFUL_S + constants.SHUTDOWN_RENDER_REAP_S
            + constants.SHUTDOWN_RUN_WAIT_S + constants.SHUTDOWN_MARGIN_S
            <= constants.SHUTDOWN_STOP_BUDGET_S), (
        "graceful + render reap + wait + margin must fit inside TimeoutStopSec")


@pytest.mark.asyncio
async def test_the_terms_the_teardown_uses_sum_to_the_stop_budget(monkeypatch):
    """The budgets the teardown PASSES -- not the ones written down -- must
    fit the unit's stop budget, the render reap included (#414).

    Captured by running the real lifespan, so a per-child reap budget that
    is never bounded, a wait sized past what is left, or a term nobody
    accounted for all fail here no matter where the number is written.
    """
    _stub_lifespan(monkeypatch)
    reaps: list = []
    waits: list[float] = []
    order: list[str] = []

    async def fake_reap(budget=None):
        reaps.append(budget)
        order.append("reap")

    monkeypatch.setattr(app_mod.api_export, "reap_live_renders", fake_reap)
    monkeypatch.setattr(app_mod.ingest, "request_shutdown",
                        lambda: order.append("abort"))
    monkeypatch.setattr(app_mod.ingest, "wait_for_run",
                        lambda t: waits.append(t) or order.append("wait") or True)

    await _teardown()

    assert reaps == [constants.SHUTDOWN_RENDER_REAP_S], (
        f"the teardown reaped with {reaps!r}; the render reap has to draw from "
        f"the stop budget ({constants.SHUTDOWN_RENDER_REAP_S}s), because it "
        f"runs ahead of the ingest wait and a budget it keeps for itself "
        f"starves the row close behind it (issue #414)")
    assert len(waits) == 1, f"the teardown waited {len(waits)} times"
    graceful, stop = _unit_stop_budget()
    spent = graceful + reaps[0] + waits[0] + constants.SHUTDOWN_MARGIN_S
    assert spent <= stop, (
        f"uvicorn spends {graceful}s draining, the render reap {reaps[0]}s, "
        f"the ingest wait {waits[0]}s and the fallback margin "
        f"{constants.SHUTDOWN_MARGIN_S}s: {spent}s of a TimeoutStopSec={stop}s, "
        f"so systemd SIGKILLs before the fallback row close lands")
    assert order == ["abort", "reap", "wait"], (
        f"the teardown ran {order!r}: the abort is signalled first so the run "
        f"is unwinding through the reap, not starting to unwind after it — "
        f"the reap is dead time for the run otherwise (issue #414)")


@pytest.mark.asyncio
async def test_the_reap_bounds_itself_as_a_whole_not_per_child(monkeypatch):
    """`reap_live_renders(budget_s)` bounds the reap as a whole.

    A per-child wait drawn once per live child is what overran the stop
    budget (issue #414): with three wedged children it spends three times
    the budget and the ingest row close behind it never runs. The bound is
    read at the asyncio.wait_for seam — the timeouts the reap passes must
    sum to at most ONE budget however many children are live — because a
    wall ceiling judged the correct shape on runner load: the Windows leg
    read 0.61s against a 0.5s bound (issue #736). Every child must still
    be KILLED — that is instant and free — so only the reaping of a wedged
    one is what the budget governs.
    """
    children = [_wedged_render() for _ in range(_WEDGED_CHILDREN)]
    waits: list[float | None] = []
    real_wait_for = asyncio.wait_for

    async def recording_wait_for(aws, timeout=None, **kwargs):
        if getattr(aws, "cr_code", None) is _WedgedRender.wait.__code__:
            waits.append(timeout)
        return await real_wait_for(aws, timeout, **kwargs)

    monkeypatch.setattr(asyncio, "wait_for", recording_wait_for)

    with _live_renders(children):
        # A reap that ignores the budget hangs on the first wedged child;
        # the outer bound turns that into a failure rather than a hung suite.
        await asyncio.wait_for(
            api_export.reap_live_renders(_REAP_BUDGET_S), timeout=5.0)

    assert waits, (
        "the reap issued no bounded wait at the asyncio.wait_for seam: a "
        "budget that never waits is the shape the companion test below "
        "exists to catch, and an empty recording would pass the sum here "
        "for free — if the reap moved to another wait primitive, this is "
        "the assertion that says so")
    waited = sum(t for t in waits if t is not None)
    assert waited <= _REAP_BUDGET_S + 1e-9, (
        f"the reap's bounded waits summed to {waited:.2f}s against a "
        f"{_REAP_BUDGET_S}s budget: the wait is bounded per CHILD, so N live "
        f"renders spend N times the stop budget's share (issue #414)")
    assert all(child.killed for child in children), (
        "every live child must be killed even when the budget is gone")


@pytest.mark.asyncio
async def test_the_reap_actually_waits_for_a_child_it_has_budget_for():
    """The bound test above is blind to a reap that never reaps at all: kill
    the children, unlink the outputs, return instantly, and it passes.

    A child whose `wait()` resolves after a real delay, under a budget far
    longer than that delay, must be waited FOR: the reap returns only once it
    resolved. The positive half of the deadline, on the same fake the bound
    test uses.
    """
    delay = 0.15
    child = _render_that_exits_after(delay)
    with _live_renders([child]):
        started = time.monotonic()
        await asyncio.wait_for(
            api_export.reap_live_renders(10 * delay), timeout=5.0)
        elapsed = time.monotonic() - started

    assert child.reaped, "the reap returned before the child was reaped"
    assert elapsed >= delay, (
        f"the reap returned after {elapsed:.3f}s, less than the {delay}s the "
        f"child took to exit: it skipped the wait instead of bounding it")


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


@pytest.mark.asyncio
async def test_a_live_render_at_shutdown_still_leaves_the_row_closed(
        fresh_db, monkeypatch):
    """The defect of #414, against a real row and real wedged children.

    A run holds the lock and is unwinding, and two render children are live
    and unreapable when the stop arrives. The teardown must close the row
    inside the teardown's own share of the stop budget: before the fix the
    reap waited its per-child bound on EACH of them, so the window the
    fallback close needs was already gone and systemd SIGKILLed the process
    with finished_at still NULL.
    """
    _stub_lifespan(monkeypatch)
    run_id = _open_run()
    # The run is in flight: it holds the lock, so the teardown's real
    # bounded wait spends its whole timeout and the fallback close runs.
    # No `with`: the lock is released in the finally below, and the
    # teardown below is the thing that would have to release it.
    ingest._RUN_LOCK.acquire()  # pylint: disable=protected-access,consider-using-with
    children = [_wedged_render(4242), _wedged_render(4243)]
    # A teardown that never returns is the same defect at its limit — a
    # reap with no bound at all hangs on the first wedged child — so the
    # ceiling is enforced here too and reports as a failure, not a hang.
    ceiling = constants.SHUTDOWN_TEARDOWN_S + 1.0
    try:
        with _live_renders(children):
            started = time.monotonic()
            try:
                await asyncio.wait_for(_teardown(), timeout=ceiling)
            except asyncio.TimeoutError:
                pytest.fail(
                    f"the teardown did not return within {ceiling}s: the "
                    f"render reap is unbounded, so a wedged child keeps the "
                    f"ingest row close behind it from ever running "
                    f"(issue #414)")
            elapsed = time.monotonic() - started
        row = _row(run_id)
    finally:
        ingest._RUN_LOCK.release()  # pylint: disable=protected-access
        ingest._set_progress(run_id=None)  # pylint: disable=protected-access

    assert row is not None
    assert row[0] is not None, (
        "a stop must never leave finished_at NULL, whatever else the process "
        "was doing when the signal arrived")
    assert row[1] == "aborted: shutdown requested", row[1]
    assert elapsed <= constants.SHUTDOWN_TEARDOWN_S, (
        f"the teardown took {elapsed:.2f}s, over its "
        f"{constants.SHUTDOWN_TEARDOWN_S}s share of the stop budget: the "
        f"render reap ran ahead of the row close and spent the window the "
        f"close needs (issue #414)")
    assert all(child.killed for child in children), (
        "the live render children must still be killed")
