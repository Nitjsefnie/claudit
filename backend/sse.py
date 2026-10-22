"""SSE body iteration and ownership of its queue and shutdown waiters."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import Request

from backend import events


async def event_stream_body(request: Request) -> AsyncIterator[str]:
    q = events.subscribe()
    shutdown = events.shutdown_event()
    try:
        yield ": connected\n\n"
        while True:
            if await request.is_disconnected():
                break
            if shutdown is not None and shutdown.is_set():
                break
            message = asyncio.create_task(q.get())
            wait_tasks = [message]
            if shutdown is not None:
                wait_tasks.append(asyncio.create_task(shutdown.wait()))
            try:
                done, _ = await asyncio.wait(
                    wait_tasks,
                    timeout=15,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for task in wait_tasks:
                    task.cancel()
                await asyncio.gather(*wait_tasks, return_exceptions=True)
            if not done:
                yield ": ping\n\n"
                continue
            if shutdown is not None and shutdown.is_set():
                break
            try:
                yield message.result()
            except asyncio.CancelledError:
                break
    finally:
        events.unsubscribe(q)
