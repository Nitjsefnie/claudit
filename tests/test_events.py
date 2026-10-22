from __future__ import annotations

import asyncio
import gc
import inspect
import json
import signal
import threading
import weakref
from collections.abc import AsyncGenerator
from typing import cast

from fastapi import FastAPI
import pytest
from starlette.requests import Request
from starlette.responses import StreamingResponse

import backend.app as app_mod
from backend import api, events

# Failure bound, not pacing: a delivery that needs this long has already
# failed the assertion that follows it.
_BOUND_S = 5.0


def _stub_scheduler(calls):
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
@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
async def test_lifespan_signal_wakes_stream_and_delegates(monkeypatch, signum):
    set_loop, signal_shutdown = events.set_loop, events.signal_shutdown
    _stub_lifespan_env(monkeypatch, [])
    monkeypatch.setattr(events, "set_loop", set_loop)
    monkeypatch.setattr(events, "signal_shutdown", signal_shutdown)
    monkeypatch.setattr(events, "_main_loop", None)
    monkeypatch.setattr(events, "_shutdown_event", None)
    received = []

    def previous(sig, frame):
        received.append((sig, frame))

    original = signal.signal(signum, previous)
    try:
        async with app_mod.lifespan(FastAPI()):
            handler = signal.getsignal(signum)
            assert callable(handler) and handler is not previous, (
                "lifespan did not install an early shutdown handler")
            handler(signum, None)
            assert received == [(signum, None)], "previous handler was swallowed"
            await asyncio.sleep(0)
            shutdown = events.shutdown_event()
            assert shutdown is not None and shutdown.is_set(), (
                "signal did not wake the SSE shutdown event")
        assert signal.getsignal(signum) is previous
    finally:
        signal.signal(signum, original)


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_shutdown_signal_delegates_when_wakeup_raises(monkeypatch, signum):
    received = []
    failure = RuntimeError("shutdown wake-up failed")
    frame = inspect.currentframe()
    assert frame is not None

    def previous(sig, received_frame):
        received.append((sig, received_frame))

    def wakeup():
        raise failure

    monkeypatch.setattr(events, "signal_shutdown", wakeup)
    originals = {sig: signal.getsignal(sig)
                 for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        signal.signal(signum, previous)
        with events.shutdown_signals():
            handler = signal.getsignal(signum)
            assert callable(handler) and handler is not previous
            with pytest.raises(RuntimeError) as raised:
                handler(signum, frame)
            assert raised.value is failure
            assert received == [(signum, frame)]
    finally:
        for sig, original in originals.items():
            signal.signal(sig, original)


def test_shutdown_signals_restores_handlers_when_context_raises():
    previous = {sig: lambda _sig, _frame: None
                for sig in (signal.SIGTERM, signal.SIGINT)}
    originals = {sig: signal.getsignal(sig) for sig in previous}
    failure = RuntimeError("serving failed")
    try:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        with pytest.raises(RuntimeError) as raised:
            with events.shutdown_signals():
                for sig, handler in previous.items():
                    installed = signal.getsignal(sig)
                    assert callable(installed) and installed is not handler
                raise failure
        assert raised.value is failure
        for sig, handler in previous.items():
            assert signal.getsignal(sig) is handler
    finally:
        for sig, original in originals.items():
            signal.signal(sig, original)


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
    # teardown revokes request_shutdown(), and the real module state
    # must not leak into other tests.
    monkeypatch.setattr(app_mod.ingest, "_SHUTDOWN", threading.Event())
    monkeypatch.setattr(app_mod.ingest, "wait_for_run", lambda _t: False)
    monkeypatch.setattr(app_mod.ingest, "close_open_run", lambda: None)
    cleared = []
    monkeypatch.setattr(
        app_mod.ingest, "clear_shutdown", lambda: cleared.append("clear")
    )

    async with app_mod.lifespan(FastAPI()):
        pass

    assert not cleared


# ---------------------------------------------------------------------------
# Broadcaster + /api/events stream coverage. Everything below
# drives the real queue/subscribe machinery and asserts on its objects; the
# only wall-clock wait in the file is the heartbeat test compressing the
# generator's literal 15 s timeout, and every async pull is bounded by
# asyncio.wait_for, so a missing delivery fails the test instead of
# hanging it.
# ---------------------------------------------------------------------------


def _install_loop(monkeypatch):
    """Point the broadcaster at this test's running loop.

    monkeypatch records the previous module state so both globals are
    restored when the test ends.
    """
    monkeypatch.setattr(events, "_main_loop", None)
    monkeypatch.setattr(events, "_shutdown_event", None)
    events.set_loop(asyncio.get_running_loop())


@pytest.mark.asyncio
async def test_set_loop_and_clear_loop_roundtrip(monkeypatch):
    """set_loop() mints a fresh shutdown event beside the captured loop;
    clear_loop() releases both and shutdown_event() returns None again."""
    monkeypatch.setattr(events, "_main_loop", None)
    monkeypatch.setattr(events, "_shutdown_event", None)
    assert events.shutdown_event() is None
    events.set_loop(asyncio.get_running_loop())
    assert events.shutdown_event() is not None
    events.clear_loop()
    assert events.shutdown_event() is None
    assert events._main_loop is None  # pylint: disable=protected-access


@pytest.mark.asyncio
async def test_subscribe_unsubscribe_bookkeeping(monkeypatch):
    """subscribe() registers a bounded queue; unsubscribe() removes it and
    discarding an unknown queue leaves the others alone."""
    monkeypatch.setattr(events, "_subscribers", set())
    queue = events.subscribe()
    assert isinstance(queue, asyncio.Queue)
    assert queue.maxsize == 16
    assert events._subscribers == {queue}  # pylint: disable=protected-access
    events.unsubscribe(queue)
    assert events._subscribers == set()  # pylint: disable=protected-access
    survivor = events.subscribe()
    events.unsubscribe(queue)
    assert events._subscribers == {survivor}  # pylint: disable=protected-access
    events.unsubscribe(survivor)


@pytest.mark.asyncio
async def test_broadcast_delivers_ingest_done_to_every_subscriber(monkeypatch):
    """broadcast_threadsafe() called from a non-loop thread lands the SSE
    wire payload on every subscriber queue."""
    _install_loop(monkeypatch)
    monkeypatch.setattr(events, "_subscribers", set())
    first_q, second_q = events.subscribe(), events.subscribe()
    thread = threading.Thread(
        target=lambda: events.broadcast_threadsafe(
            "ingest_done", {"files": 3, "elapsed_s": 1.5})
    )
    thread.start()
    try:
        first = await asyncio.wait_for(first_q.get(), timeout=_BOUND_S)
        second = await asyncio.wait_for(second_q.get(), timeout=_BOUND_S)
    finally:
        thread.join()
        events.unsubscribe(first_q)
        events.unsubscribe(second_q)
    assert first == second
    lines = first.splitlines()
    assert lines[0] == "event: ingest_done"
    assert json.loads(lines[1].removeprefix("data: ")) == {
        "files": 3, "elapsed_s": 1.5,
    }
    assert first.endswith("\n\n")


@pytest.mark.asyncio
async def test_broadcast_full_queue_drops_only_that_subscriber(monkeypatch):
    """A subscriber whose queue is already at maxsize misses the event —
    asyncio.QueueFull is swallowed per queue — while every other
    subscriber still receives it."""
    _install_loop(monkeypatch)
    monkeypatch.setattr(events, "_subscribers", set())
    slow_q, healthy_q = events.subscribe(), events.subscribe()
    for i in range(slow_q.maxsize):
        slow_q.put_nowait(f"stale-{i}")
    thread = threading.Thread(
        target=lambda: events.broadcast_threadsafe("ingest_done", {})
    )
    thread.start()
    try:
        payload = await asyncio.wait_for(healthy_q.get(), timeout=_BOUND_S)
    finally:
        thread.join()
        events.unsubscribe(slow_q)
        events.unsubscribe(healthy_q)
    assert payload.startswith("event: ingest_done\n")
    assert slow_q.qsize() == slow_q.maxsize
    assert slow_q.get_nowait() == "stale-0"


@pytest.mark.asyncio
async def test_broadcast_without_a_captured_loop_is_a_noop(monkeypatch):
    """Before startup (or after clear_loop) nothing is delivered and
    nothing raises — the caller is a scheduler thread and must survive."""
    monkeypatch.setattr(events, "_subscribers", set())
    monkeypatch.setattr(events, "_main_loop", None)
    monkeypatch.setattr(events, "_shutdown_event", None)
    queue = events.subscribe()
    events.broadcast_threadsafe("ingest_done", {"n": 1})
    assert queue.empty()


class _LoopClosingMidCall:
    """A loop that closes between the entry guard and the schedule."""

    def __init__(self):
        self._closed = False

    def is_closed(self):
        return self._closed

    def call_soon_threadsafe(self, _callback):
        self._closed = True
        raise RuntimeError("Event loop is closed")


class _LoopNotActuallyClosed:
    """A loop whose RuntimeError is not a close — must be re-raised."""

    def is_closed(self):
        return False

    def call_soon_threadsafe(self, _callback):
        raise RuntimeError("something else")


def test_broadcast_swallows_loop_closing_mid_call(monkeypatch):
    """The loop can close between broadcast_threadsafe's is_closed() check
    and call_soon_threadsafe; exactly that RuntimeError is swallowed, so a
    scheduler thread racing shutdown survives."""
    monkeypatch.setattr(events, "_main_loop", _LoopClosingMidCall())
    events.broadcast_threadsafe("ingest_done", {})


def test_broadcast_reraises_unrelated_runtime_error(monkeypatch):
    """A RuntimeError that is not a mid-call loop close is not swallowed
    (raising here would hide a real defect from the caller)."""
    monkeypatch.setattr(events, "_main_loop", _LoopNotActuallyClosed())
    with pytest.raises(RuntimeError):
        events.broadcast_threadsafe("ingest_done", {})


def test_signal_shutdown_swallows_loop_closing_mid_call(monkeypatch):
    """The same mid-call close guard as broadcast, on the shutdown path:
    the RuntimeError racing a closing loop is swallowed and the event
    stays unset."""
    monkeypatch.setattr(events, "_shutdown_event", asyncio.Event())
    monkeypatch.setattr(events, "_main_loop", _LoopClosingMidCall())
    event = events.shutdown_event()
    assert event is not None
    events.signal_shutdown()
    assert not event.is_set()


def test_signal_shutdown_reraises_unrelated_runtime_error(monkeypatch):
    """A RuntimeError that is not a mid-call loop close is not swallowed
    on the shutdown path either."""
    monkeypatch.setattr(events, "_shutdown_event", asyncio.Event())
    monkeypatch.setattr(events, "_main_loop", _LoopNotActuallyClosed())
    with pytest.raises(RuntimeError):
        events.signal_shutdown()


def test_signal_shutdown_after_loop_closed_is_a_noop(monkeypatch):
    """A shutdown signal arriving after the loop closed neither raises nor
    sets the event (mirrors the broadcast guard)."""
    monkeypatch.setattr(events, "_main_loop", None)
    monkeypatch.setattr(events, "_shutdown_event", None)
    loop = asyncio.new_event_loop()
    events.set_loop(loop)
    loop.close()
    event = events.shutdown_event()
    assert event is not None
    events.signal_shutdown()
    assert not event.is_set()


class _StubRequest(Request):
    """A real Request whose disconnect is test-controlled: the /api/events
    generator only ever awaits is_disconnected(), which the real
    implementation reads off the ASGI receive channel."""

    def __init__(self):
        super().__init__(
            {"type": "http", "method": "GET", "path": "/api/events",
             "headers": []},
        )
        self.disconnected = False

    async def is_disconnected(self) -> bool:
        return self.disconnected


async def _next_chunk(body):
    """Pull the next stream chunk under a failure bound — a stream that
    stalls has failed the test, not paced it."""
    return await asyncio.wait_for(anext(body), timeout=_BOUND_S)


def _body(response: StreamingResponse) -> AsyncGenerator[str, None]:
    """The response body as the async generator it is at runtime.

    StreamingResponse's declared type only promises iteration, but these
    tests drive anext()/aclose() on the endpoint's generator directly.
    """
    return cast("AsyncGenerator[str, None]", response.body_iterator)


@pytest.mark.asyncio
async def test_event_stream_connect_deliver_disconnect(monkeypatch):
    """The full /api/events cycle: stream headers at connect, the
    connected comment, one ingest_done delivered mid-stream, then a
    client disconnect ends the stream and unsubscribes its queue."""
    _install_loop(monkeypatch)
    monkeypatch.setattr(events, "_subscribers", set())
    request = _StubRequest()

    response = await api.event_stream(request)
    assert isinstance(response, StreamingResponse)
    assert response.media_type == "text/event-stream"
    assert response.headers["cache-control"] == "no-cache, no-transform"
    assert response.headers["connection"] == "keep-alive"
    assert response.headers["x-accel-buffering"] == "no"
    # The subscription happens inside the generator: a handler return
    # alone has not subscribed anyone yet.
    assert events._subscribers == set()  # pylint: disable=protected-access

    body = _body(response)
    assert await _next_chunk(body) == ": connected\n\n"
    assert len(events._subscribers) == 1  # pylint: disable=protected-access

    thread = threading.Thread(
        target=lambda: events.broadcast_threadsafe(
            "ingest_done", {"files": 2, "elapsed_s": 0.25})
    )
    thread.start()
    try:
        payload = await _next_chunk(body)
    finally:
        thread.join()
    lines = payload.splitlines()
    assert lines[0] == "event: ingest_done"
    assert json.loads(lines[1].removeprefix("data: ")) == {
        "files": 2, "elapsed_s": 0.25,
    }

    request.disconnected = True
    with pytest.raises(StopAsyncIteration):
        await _next_chunk(body)
    assert events._subscribers == set()  # pylint: disable=protected-access


@pytest.mark.asyncio
async def test_event_stream_abrupt_close_unsubscribes(monkeypatch):
    """A client vanishing mid-stream closes the generator at its
    suspension point — what StreamingResponse does server-side on
    disconnect — and the finally still unsubscribes the queue."""
    _install_loop(monkeypatch)
    monkeypatch.setattr(events, "_subscribers", set())
    response = await api.event_stream(_StubRequest())
    body = _body(response)
    assert await _next_chunk(body) == ": connected\n\n"
    queue = next(iter(events._subscribers))  # pylint: disable=protected-access
    await body.aclose()
    assert queue not in events._subscribers  # pylint: disable=protected-access
    assert events._subscribers == set()  # pylint: disable=protected-access


@pytest.mark.asyncio
async def test_event_stream_heartbeat_while_idle(monkeypatch):
    """An idle stream — no event, no shutdown — emits the ':' heartbeat
    comment when the wait times out, and keeps the subscriber attached.
    The generator's literal 15 s timeout is compressed for the test; the
    branch exercised is the real one: both waiters pending, empty done,
    pending tasks cancelled."""
    _install_loop(monkeypatch)
    monkeypatch.setattr(events, "_subscribers", set())
    request = _StubRequest()
    real_wait = asyncio.wait

    async def compressed_wait(aws, *, timeout=None, return_when=None):
        return await real_wait(
            aws, timeout=0.05,
            return_when=return_when or asyncio.FIRST_COMPLETED)

    response = await api.event_stream(request)
    body = _body(response)
    assert await _next_chunk(body) == ": connected\n\n"
    with monkeypatch.context() as m:
        m.setattr(asyncio, "wait", compressed_wait)
        assert await _next_chunk(body) == ": ping\n\n"
    assert events._subscribers  # pylint: disable=protected-access
    request.disconnected = True
    with pytest.raises(StopAsyncIteration):
        await _next_chunk(body)
    assert events._subscribers == set()  # pylint: disable=protected-access


@pytest.mark.asyncio
async def test_event_stream_shutdown_ends_the_stream(monkeypatch):
    """A cross-thread shutdown ends current and subsequent streams and removes subscribers."""
    _install_loop(monkeypatch)
    monkeypatch.setattr(events, "_subscribers", set())
    response = await api.event_stream(_StubRequest())
    body = _body(response)
    assert await _next_chunk(body) == ": connected\n\n"

    thread = threading.Thread(target=events.signal_shutdown)
    thread.start()
    try:
        with pytest.raises(StopAsyncIteration):
            await _next_chunk(body)
    finally:
        thread.join()
    assert events._subscribers == set()  # pylint: disable=protected-access

    # A stream opened after the signal exits at the top-of-loop check,
    # right after its connected comment.
    response_after = await api.event_stream(_StubRequest())
    body_after = _body(response_after)
    assert await _next_chunk(body_after) == ": connected\n\n"
    with pytest.raises(StopAsyncIteration):
        await _next_chunk(body_after)
    assert events._subscribers == set()  # pylint: disable=protected-access


async def _drive_stream_ending(monkeypatch, ending, waiters):
    real_wait = asyncio.wait
    _install_loop(monkeypatch)
    monkeypatch.setattr(events, "_subscribers", set())
    request = _StubRequest()
    body = _body(await api.event_stream(request))
    assert await anext(body) == ": connected\n\n"
    entered = asyncio.Event()

    async def observed_wait(aws, *, timeout, return_when):
        assert timeout == 15
        waiters.extend(weakref.ref(task) for task in aws)
        entered.set()
        return await real_wait(
            aws, timeout=0 if ending == "ping_timeout" else timeout,
            return_when=return_when)

    monkeypatch.setattr(asyncio, "wait", observed_wait)
    if ending == "connect":
        request.disconnected = True
        with pytest.raises(StopAsyncIteration):
            await anext(body)
        assert not waiters
    elif ending in {"client_disconnect_after_delivery", "ping_timeout", "delivered_event"}:
        if ending != "ping_timeout":
            events.broadcast_threadsafe("ingest_done", {})
        chunk = await anext(body)
        if ending == "ping_timeout":
            assert chunk == ": ping\n\n"
        else:
            assert chunk == 'event: ingest_done\ndata: {}\n\n'
        if ending == "delivered_event":
            await body.aclose()
        else:
            request.disconnected = True
            with pytest.raises(StopAsyncIteration):
                await anext(body)
    else:
        pull = asyncio.create_task(anext(body))
        await asyncio.wait_for(entered.wait(), timeout=_BOUND_S)
        if ending == "shutdown":
            events.signal_shutdown()
            with pytest.raises(StopAsyncIteration):
                await asyncio.wait_for(pull, timeout=_BOUND_S)
        else:
            request.disconnected = True
            pull.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pull
            await body.aclose()
    if ending != "connect":
        assert len(waiters) == 2
    pending_at_end = [
        repr(task) for ref in waiters
        if (task := ref()) is not None and not task.done()]
    events.clear_loop()
    return pending_at_end


@pytest.mark.parametrize("ending", [
    "connect", "ping_timeout", "delivered_event",
    "client_disconnect_after_delivery", "client_disconnect_while_waiting", "shutdown",
])
def test_event_stream_reaps_waiters_without_loop_errors(monkeypatch, ending):
    loop = asyncio.new_event_loop()
    errors = []
    waiters = []
    loop.set_exception_handler(
        lambda _loop, context: errors.append(
            (context["message"], repr(context.get("exception")))))

    try:
        pending_at_end = loop.run_until_complete(asyncio.wait_for(
            _drive_stream_ending(monkeypatch, ending, waiters), timeout=_BOUND_S))
        gc.collect()
        loop.run_until_complete(asyncio.sleep(0))
        loop.run_until_complete(loop.shutdown_asyncgens())
        gc.collect()
        assert not errors, errors
        assert not pending_at_end, pending_at_end
    finally:
        remaining = asyncio.all_tasks(loop)
        for task in remaining:
            task.cancel()
        if remaining:
            loop.run_until_complete(
                asyncio.gather(*remaining, return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


@pytest.mark.asyncio
async def test_lifespan_cancels_statements_and_closes_the_open_run(monkeypatch):
    """Cancel queries before the fallback closes a timed-out run."""
    calls: list = []
    _stub_lifespan_env(monkeypatch, calls)
    monkeypatch.setattr(app_mod.db, "cancel_viz_queries",
                        lambda: calls.append("cancel"))
    monkeypatch.setattr(app_mod.ingest, "close_open_run",
                        lambda: calls.append("close_row"))
    monkeypatch.setattr(app_mod.ingest, "_SHUTDOWN", threading.Event())
    monkeypatch.setattr(app_mod.ingest, "wait_for_run", lambda _t: False)
    monkeypatch.setattr(
        app_mod.ingest, "clear_shutdown", lambda: cleared.append("clear"))
    cleared: list = []

    async with app_mod.lifespan(FastAPI()):
        pass

    assert "cancel" in calls
    assert "close_row" in calls
    # Cancel rides the abort request; the fallback close rides the timeout.
    assert calls.index("cancel") < calls.index("close_row")
