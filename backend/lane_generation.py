"""Generation windows bound by a lane's advancing usage events."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from backend.reply_timing import GENERATION_S_MAX


@dataclass(slots=True)
class _Window:
    anchor_ts: datetime | None
    end_ts: datetime | None
    failed: bool = False


class LaneGeneration:
    """Keep the latest completed response until a usage event books it."""

    def __init__(self) -> None:
        self._pending_anchor: datetime | None = None
        self._open: _Window | None = None
        self._closed: _Window | None = None

    def _close(self) -> None:
        if self._open is not None:
            self._closed = self._open
            self._open = None

    def anchor_line(self, ts: datetime | None) -> None:
        self._close()
        self._pending_anchor = ts

    def response_line(self, ts: datetime | None) -> None:
        if self._open is None:
            self._open = _Window(self._pending_anchor, ts)
            self._pending_anchor = None
        else:
            self._open.end_ts = ts

    def fail(self) -> None:
        self._pending_anchor = None
        if self._open is not None:
            self._open.failed = True

    def bind(self) -> float | None:
        self._close()
        window = self._closed
        self._closed = None
        if (window is None or window.failed
                or window.anchor_ts is None or window.end_ts is None):
            return None
        seconds = (window.end_ts - window.anchor_ts).total_seconds()
        return seconds if 0 < seconds <= GENERATION_S_MAX else None


def codex_generation_line(clock: LaneGeneration, rtype: str,
                          payload: dict, ts: datetime | None) -> None:
    """Map Codex input and response lines without treating interrupts as failures."""
    ptype = payload.get("type")
    if rtype == "response_item":
        role = payload.get("role") if ptype == "message" else None
        if role == "user" or ptype in ("custom_tool_call_output", "function_call_output"):
            clock.anchor_line(ts)
        elif role == "assistant" or ptype in (
                "agent_message", "reasoning", "custom_tool_call", "function_call"):
            clock.response_line(ts)
    elif rtype == "event_msg":
        item = payload.get("item")
        if (ptype in ("agent_message", "agent_reasoning")
                or (ptype == "item_completed" and isinstance(item, dict)
                    and item.get("type") in ("AgentMessage", "Reasoning"))):
            clock.response_line(ts)
