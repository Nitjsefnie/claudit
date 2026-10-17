"""Thread-safe ingest-completion SSE broadcast.

APScheduler and request worker threads publish through the uvicorn event loop
captured at startup; call_soon_threadsafe feeds per-subscriber asyncio queues.
Browsers re-fetch the dashboard on ingest_done.
"""
from __future__ import annotations

import asyncio
import json
import threading

_subscribers: set[asyncio.Queue] = set()
_subscribers_lock = threading.Lock()
_main_loop: asyncio.AbstractEventLoop | None = None
_shutdown_event: asyncio.Event | None = None


def set_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Called once at startup. Required before broadcast_threadsafe works."""
    global _main_loop, _shutdown_event
    _main_loop = loop
    _shutdown_event = asyncio.Event()


def clear_loop() -> None:
    global _main_loop, _shutdown_event
    _main_loop = None
    _shutdown_event = None


def shutdown_event() -> asyncio.Event | None:
    """Subscribed-to by SSE generators so they can exit promptly when the
    server is shutting down (otherwise uvicorn's graceful-shutdown waits
    forever for the never-ending response)."""
    return _shutdown_event


def signal_shutdown() -> None:
    loop = _main_loop
    event = _shutdown_event
    if event is None or loop is None or loop.is_closed():
        return
    try:
        loop.call_soon_threadsafe(event.set)
    except RuntimeError:
        # The event loop can close between is_closed() and this call.
        if not loop.is_closed():
            raise


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue(maxsize=16)
    with _subscribers_lock:
        _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    with _subscribers_lock:
        _subscribers.discard(q)


def broadcast_threadsafe(event: str, data: dict) -> None:
    """Push an SSE event to every connected subscriber. Safe from any
    thread (worker, scheduler, request handler). No-op if startup
    hasn't captured the loop yet."""
    loop = _main_loop
    if loop is None or loop.is_closed():
        return
    payload = f"event: {event}\ndata: {json.dumps(data)}\n\n"
    with _subscribers_lock:
        targets = list(_subscribers)

    def _put_all() -> None:
        for q in targets:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    try:
        loop.call_soon_threadsafe(_put_all)
    except RuntimeError:
        # Lifespan teardown can close the loop after the check above.
        if not loop.is_closed():
            raise
