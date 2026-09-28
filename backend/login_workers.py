"""Run login CPU work without releasing its admission slots early."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, TypeVar

_WorkerResult = TypeVar("_WorkerResult")


class WorkerReservation:
    """Hold a login reservation until the request and its workers finish."""

    def __init__(self, release: Callable[[], None]) -> None:
        self._release = release
        self._pending_workers = 0
        self._closed = False
        self._released = False

    def track_worker(self, worker: asyncio.Task[Any]) -> None:
        self._pending_workers += 1
        worker.add_done_callback(self._worker_finished)

    def close(self) -> None:
        """Release now, or when the final shielded worker completes."""
        self._closed = True
        self._release_if_ready()

    def _worker_finished(self, worker: asyncio.Task[Any]) -> None:
        # A canceled request no longer awaits this task, so retrieve any
        # eventual worker exception to avoid an unobserved-task warning.
        try:
            worker.exception()
        except asyncio.CancelledError:
            pass
        self._pending_workers -= 1
        self._release_if_ready()

    def _release_if_ready(self) -> None:
        if self._closed and not self._pending_workers and not self._released:
            self._released = True
            self._release()


async def run_reserved_worker(
    reservation: WorkerReservation,
    worker: Callable[..., _WorkerResult],
    *args: Any,
) -> _WorkerResult:
    """Run blocking work without letting cancellation drop its slot."""
    task = asyncio.create_task(asyncio.to_thread(worker, *args))
    reservation.track_worker(task)
    return await asyncio.shield(task)
