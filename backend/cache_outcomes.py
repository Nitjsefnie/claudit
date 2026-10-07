"""The response cache's outcome counters (issue #641).

The production evidence for the slow-open split: a count cannot be moved
by load, so the journal line each invalidate() emits — the outcomes of
one inter-ingest window — and the cumulative readout in /health answer
"where do opens wait" without a deploy. Split out of cache.py for the
module-size ratchet.

Outcomes are counted on the cache instance, so the tests' swapped-in
instances carry their own counters and never bleed into the singleton.
"""
from __future__ import annotations

import threading
from collections import Counter

# The full outcome vocabulary: outcomes() reports all of it, zero
# included, so /health scrapers never read 'zero' as 'missing key'.
OUTCOME_KEYS = (
    "fresh_hit", "stale_hit", "miss_inline", "miss_waited", "miss_error",
    "refresh_run", "refresh_skip_fresh", "refresh_error",
    "warm_run", "warm_skip_fresh", "warm_skip_inflight", "warm_error",
)


class OutcomeCounters:
    """Cumulative counters plus the since-last-invalidate window.

    The window is taken (returned and zeroed) by the cache's
    ``invalidate()``, which logs it: one journal line per changed ingest,
    the outcomes of exactly the interval since the previous one. That
    interval is closed by the NEXT run's invalidate — the ingest tail
    orders invalidate() before warm_common() — so a run's own warm
    outcomes are counted into the FOLLOWING window; windows are
    inter-invalidate intervals, labeled by the invalidate that closes
    them (issue #641 review).
    """

    def __init__(self) -> None:
        self._total: Counter[str] = Counter()
        self._window: Counter[str] = Counter()
        self._guard = threading.Lock()

    def count(self, **deltas: int) -> None:
        with self._guard:
            self._total.update(deltas)
            self._window.update(deltas)

    def outcomes(self) -> dict[str, int]:
        """Every outcome, zero included."""
        with self._guard:
            return {k: self._total.get(k, 0) for k in OUTCOME_KEYS}

    def take_window(self) -> dict[str, int]:
        """Return and zero the since-last-invalidate window."""
        with self._guard:
            window, self._window = self._window, Counter()
            return {k: window.get(k, 0) for k in OUTCOME_KEYS}

    def reset(self) -> None:
        """Zero both. The tests' seam; ops never calls it."""
        with self._guard:
            self._total.clear()
            self._window.clear()
