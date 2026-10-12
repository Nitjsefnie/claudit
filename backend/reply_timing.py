"""Claude request timing: independent reply and generation anchors.

Non-empty user text anchors latency even when harness-injected text counts
as no prompt and splits no context turn. Human image blocks anchor too.
Replayed user records never change the anchor; whitespace-only text does
nothing. An interrupt marker or system/api_error clears it. Every assistant
record consumes it, including synthetic error stubs, rate-limit records and
records with no usage. A reply after a failed attempt has unknown latency
unless a fresh user message re-anchors it.

Generation timing instead anchors on every non-replayed user line, including
tool results and interrupts. Each new message.id consumes that anchor; its
last line supplies the end timestamp and output count, without max-merging.
Failure epochs invalidate only spans containing a failed attempt.

CLAUDIT_COLLECT_GENERATION_SPEED is read once at startup (issue #955).
Unset or empty enables collection; 0/false/no disables both columns.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import os

from backend.parse_common import _to_dt

GENERATION_S_MAX = 3600
COLLECT_GENERATION_SPEED = os.environ.get(
    "CLAUDIT_COLLECT_GENERATION_SPEED", "").lower() not in ("0", "false", "no")


@dataclass(slots=True)
class _GenerationSpan:
    start: datetime | None
    first_epoch: int
    last_epoch: int = 0
    end: str = ""
    output: int | float | None = None


class GenerationTiming:
    """One file's generation clock, keyed by message.id rather than requestId."""

    def __init__(self) -> None:
        self._anchor: datetime | None = None
        self._failure_epoch = 0
        self.requests: dict[str, _GenerationSpan] = {}
        self._user_uuids: dict[str, int] = {}

    def anchor(self, ts_str: str) -> None:
        """A user line supersedes the anchor, even with no valid timestamp."""
        self._anchor = _to_dt(ts_str)

    def observe_user(self, obj: dict, line_num: int) -> None:
        """Anchor every user line's content shape, except earlier UUID replays."""
        uuid = obj.get("uuid")
        if uuid and self._user_uuids.setdefault(uuid, line_num) != line_num:
            return
        self.anchor(obj.get("timestamp", "") or "")

    def fail(self) -> None:
        """Clear the pending anchor and mark a boundary for open spans."""
        self._anchor = None
        self._failure_epoch += 1

    def observe(self, obj: dict, msg: dict) -> _GenerationSpan | None:
        """Advance the last line even when no usage-bearing record is emitted."""
        if obj.get("isApiErrorMessage") is True:
            self.fail()
        mid = msg.get("id")
        # Idless lines skip the type check; a truthy id still must be a string.
        if not mid or not isinstance(mid, str):
            return None
        span = self.requests.get(mid)
        if span is None:
            span = self.requests[mid] = _GenerationSpan(self._anchor, self._failure_epoch)
            self._anchor = None
        span.last_epoch = self._failure_epoch
        span.end = obj.get("timestamp", "") or ""
        usage = msg.get("usage")
        span.output = usage.get("output_tokens") if isinstance(usage, dict) else None
        return span


def generation_values(span: _GenerationSpan | None) -> tuple[float | None, float | None]:
    """Validate the final span and divide the last line's provider output total."""
    if span is None or span.start is None or span.first_epoch != span.last_epoch:
        return None, None
    end = _to_dt(span.end)
    if end is None:
        return None, None
    seconds = (end - span.start).total_seconds()
    if not 0 < seconds <= GENERATION_S_MAX:
        return None, None
    output = span.output
    return seconds, output / seconds if isinstance(output, (int, float)) else None


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


def failed_attempt(reply: ReplyTiming, generation: GenerationTiming | None) -> None:
    """Invalidate both clocks at a system/api_error line."""
    reply.clear()
    if generation is not None:
        generation.fail()
