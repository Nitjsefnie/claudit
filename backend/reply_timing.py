"""Claude reply latency from the latest non-interrupt user message.

Non-empty user text anchors latency even when harness-injected text counts
as no prompt and splits no context turn. Human image blocks anchor too.
Replayed user records never change the anchor; whitespace-only text does
nothing. An interrupt marker or system/api_error clears it. Every assistant
record consumes it, including synthetic error stubs, rate-limit records and
records with no usage. A reply after a failed attempt has unknown latency
unless a fresh user message re-anchors it.
"""
from __future__ import annotations

from datetime import datetime

from backend.parse_common import _to_dt


class ReplyTiming:
    """One file's reply-latency anchor, independent of prompt counting."""

    def __init__(self) -> None:
        self._anchor_ts: datetime | None = None

    def anchor(self, ts: datetime | None) -> None:
        """Supersede the anchor only when the user message has a valid ts."""
        if ts is not None:
            self._anchor_ts = ts

    def clear(self) -> None:
        """Invalidate an interrupted or failed attempt's measurement."""
        self._anchor_ts = None

    def consume(self, obj: dict) -> float | None:
        """Gap to the first assistant line; consume even when no row is stored.

        NULL when unanchored, unstamped, or the delta is negative (restore /
        compaction replay). Clamping negative deltas would pollute latency
        percentiles. Error stubs and no-usage lines close the window too,
        so the next real reply never inherits an earlier failed turn's wait.
        """
        reply_latency_s = None
        if self._anchor_ts is not None:
            assistant_dt = _to_dt(obj.get("timestamp", "") or "")
            if assistant_dt is not None:
                delta_s = (assistant_dt - self._anchor_ts).total_seconds()
                if delta_s >= 0:
                    reply_latency_s = delta_s
        self._anchor_ts = None
        return reply_latency_s
