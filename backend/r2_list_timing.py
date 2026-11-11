"""CLAUDIT_DEBUG_LIST_TIMING: per-range, per-bucket and per-pass lines of the S3 listing."""
from __future__ import annotations

import logging
import threading
from collections.abc import Iterable, Iterator
from time import perf_counter, process_time, thread_time

from backend.env_config import getenv

LIST_TIMING_ON = getenv("CLAUDIT_DEBUG_LIST_TIMING", "").lower() not in ("", "0", "false", "no")
log = logging.getLogger("claudit.r2")


class _Bucket:
    __slots__ = ("ranges", "requests", "chain", "in_flight", "peak")

    def __init__(self) -> None:
        self.ranges = self.requests = self.chain = self.in_flight = self.peak = 0


class _Range:
    """One key range: its requests' wall and keys, its thread CPU."""

    def __init__(self, listing: ListTiming, bucket: str,
                 lower: str | None, upper: str | None) -> None:
        self._listing, self._bounds = listing, (bucket, lower, upper)
        self._t0, self._cpu0 = perf_counter(), thread_time()
        self._setup: float | None = None
        self._request_ms: list[str] = []
        self._page_keys: list[str] = []

    def pages(self, pages: Iterable[dict]) -> Iterator[dict]:
        bucket = self._bounds[0]
        it = iter(pages)
        while True:
            started = perf_counter()
            if self._setup is None:
                self._setup = started - self._t0
            self._listing.request(bucket, +1)
            try:
                page = next(it)
            except StopIteration:
                return
            finally:
                self._listing.request(bucket, -1)
            self._request_ms.append(f"{(perf_counter() - started) * 1000:.0f}")
            self._page_keys.append(str(len(page.get("Contents", []))))
            yield page

    def done(self, kept: int) -> None:
        cpu, wall = thread_time() - self._cpu0, perf_counter() - self._t0
        self._listing.range_done(self._bounds[0], len(self._request_ms))
        log.info("TIMING list_range bucket=%s lower=%r upper=%r pages=%d request_ms=%s "
                 "page_keys=%s kept=%d setup_ms=%.0f wall_ms=%.0f cpu_s=%.3f",
                 *self._bounds, len(self._request_ms), ",".join(self._request_ms),
                 ",".join(self._page_keys), kept, (self._setup or 0.0) * 1000,
                 wall * 1000, cpu)


class ListTiming:
    """One listing pass, from the split-point read to the sorted result."""

    def __init__(self, buckets: list[str]) -> None:
        self._t0, self._cpu0 = perf_counter(), process_time()
        self._split = 0.0
        self._lock = threading.Lock()
        self._buckets = {bucket: _Bucket() for bucket in buckets}
        self._in_flight = self._peak = 0

    def splits_read(self) -> None:
        self._split = perf_counter() - self._t0

    def range(self, bucket: str, lower: str | None, upper: str | None) -> _Range:
        return _Range(self, bucket, lower, upper)

    def request(self, bucket: str, step: int) -> None:
        with self._lock:
            stats = self._buckets[bucket]
            stats.in_flight += step
            stats.peak = max(stats.peak, stats.in_flight)
            self._in_flight += step
            self._peak = max(self._peak, self._in_flight)

    def range_done(self, bucket: str, pages: int) -> None:
        with self._lock:
            stats = self._buckets[bucket]
            stats.ranges += 1
            stats.requests += pages
            stats.chain = max(stats.chain, pages)

    def done(self) -> None:
        wall, cpu = perf_counter() - self._t0, process_time() - self._cpu0
        for bucket, s in self._buckets.items():
            log.info("TIMING list_bucket bucket=%s ranges=%d requests=%d longest_chain=%d "
                     "max_in_flight=%d", bucket, s.ranges, s.requests, s.chain, s.peak)
        stats = self._buckets.values()
        log.info("TIMING list_pass buckets=%d ranges=%d requests=%d longest_chain=%d "
                 "max_in_flight=%d split_ms=%.0f wall_ms=%.0f cpu_s=%.3f",
                 len(self._buckets), sum(s.ranges for s in stats),
                 sum(s.requests for s in stats), max((s.chain for s in stats), default=0),
                 self._peak, self._split * 1000, wall * 1000, cpu)
