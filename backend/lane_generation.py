"""Generation windows bound by a lane's advancing usage events."""
from __future__ import annotations

from datetime import datetime

from backend.reply_timing import GENERATION_S_MAX


class LaneGeneration:
    """Keep the latest completed response until a usage event books it."""

    __slots__ = ("_pending_anchor", "_open", "_closed")

    def __init__(self) -> None:
        self._pending_anchor: datetime | None = None
        self._open: tuple[datetime | None, datetime | None] | None = None
        self._closed: tuple[datetime | None, datetime | None] | None = None

    def anchor_line(self, ts: datetime | None) -> None:
        if self._open is not None:
            self._closed = self._open
            self._open = None
        self._pending_anchor = ts

    def response_line(self, ts: datetime | None) -> None:
        if self._open is None:
            self._open = (self._pending_anchor, ts)
            self._pending_anchor = None
        else:
            self._open = (self._open[0], ts)

    def fail(self) -> None:
        self._pending_anchor = None
        if self._open is not None:
            self._open = (None, self._open[1])

    def bind(self) -> float | None:
        window = self._open or self._closed
        self._open = self._closed = None
        if window is None:
            return None
        start, end = window
        if start is None or end is None:
            return None
        seconds = (end - start).total_seconds()
        return seconds if 0 < seconds <= GENERATION_S_MAX else None
