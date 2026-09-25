"""SSE broadcaster lifecycle regressions."""
import asyncio
import threading

from fastapi import FastAPI
import pytest

import backend.app as app_mod
from backend import events


def _stub_scheduler(calls):
    """A BackgroundScheduler double recording shutdown(wait) into calls."""
    class _Scheduler:
        def __init__(self, **_kwargs):
            pass

        def add_job(self, *_args, **_kwargs):
            pass

        def start(self):
            pass

        def shutdown(self, *, wait):
            calls.append(("scheduler", wait))

    return _Scheduler


def test_broadcast_after_loop_closed_is_a_noop(monkeypatch):
    """A scheduler job finishing after shutdown must not raise from its
    daemon thread when the captured event loop has already closed."""
    monkeypatch.setattr(events, "_main_loop", None)
    monkeypatch.setattr(events, "_shutdown_event", None)
    loop = asyncio.new_event_loop()
    events.set_loop(loop)
    loop.close()

    events.broadcast_threadsafe("late", {"ok": True})


def _stub_lifespan_env(monkeypatch, calls):
    """Silence the lifespan's DB, scheduler and events surfaces.

    apply_schema/schema_check would need the scratch database, and the
    real events functions would mutate module state the next test sees.
    """
    monkeypatch.setattr(app_mod.db, "apply_schema", lambda: None)
    monkeypatch.setattr(app_mod.db, "schema_check", lambda: None)
    monkeypatch.setattr(app_mod, "BackgroundScheduler", _stub_scheduler(calls))
    monkeypatch.setattr(
        app_mod.events, "set_loop", lambda _loop: calls.append("set")
    )
    monkeypatch.setattr(
        app_mod.events, "signal_shutdown", lambda: calls.append("signal")
    )
    monkeypatch.setattr(
        app_mod.events,
        "clear_loop",
        lambda: calls.append("clear"),
        raising=False,
    )


@pytest.mark.asyncio
async def test_lifespan_signals_before_clearing_loop(monkeypatch):
    """SSE drain must be signalled before broadcaster state is cleared."""
    calls = []
    _stub_lifespan_env(monkeypatch, calls)

    async with app_mod.lifespan(FastAPI()):
        pass

    assert calls[-3:] == ["signal", ("scheduler", False), "clear"]


@pytest.mark.asyncio
async def test_lifespan_keeps_shutdown_request_when_wait_times_out(monkeypatch):
    """A run still unwinding past the bounded wait keeps its abort request.

    Revoking the request while a straggler could still be unwinding would
    let it run to completion instead of aborting at its next bounded step,
    so the teardown clears it only when the wait succeeded.
    """
    calls = []
    _stub_lifespan_env(monkeypatch, calls)
    # A throwaway event: with the negative branch taken, nothing in the
    # teardown revokes request_shutdown(), and the real module-level flag
    # must not leak into other tests.
    monkeypatch.setattr(app_mod.ingest, "_SHUTDOWN", threading.Event())
    monkeypatch.setattr(app_mod.ingest, "wait_for_run", lambda _t: False)
    cleared = []
    monkeypatch.setattr(
        app_mod.ingest, "clear_shutdown", lambda: cleared.append("clear")
    )

    async with app_mod.lifespan(FastAPI()):
        pass

    assert cleared == []
