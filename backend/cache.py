"""In-process caches:
  - transcript LRU keyed by r2_etag, 256 MB, 20-min idle eviction
  - response cache with stale-while-revalidate for the heavy read endpoints
"""
from __future__ import annotations

import functools
import inspect
import logging
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

log = logging.getLogger("claudit.cache")


class _IdleLRU:
    """LRU with size cap + idle-time eviction.

    `idle_seconds` is measured against the LAST ACCESS, not insert.
    A get() refreshes the timestamp; eviction removes anything not
    touched in the last `idle_seconds`.

    Reads and writes are guarded by a lock: the decorated endpoints run
    in FastAPI's threadpool, so concurrent `get`/`put` race otherwise
    (the idle scan iterates the shared OrderedDict on every put).

    A value larger than `max_bytes` is NOT cached — it is served
    uncached rather than being pinned above the cap, and a put of one
    leaves any existing entry under that key untouched.
    """

    def __init__(self, max_bytes: int, idle_seconds: int):
        self.max_bytes = max_bytes
        self.idle_seconds = idle_seconds
        self._items: "OrderedDict[str, tuple[bytes, float]]" = OrderedDict()
        self._size = 0
        self._guard = threading.Lock()

    def get(self, key: str) -> bytes | None:
        with self._guard:
            item = self._items.get(key)
            if item is None:
                return None
            data, _ts = item
            self._items[key] = (data, time.time())
            self._items.move_to_end(key)
            return data

    def put(self, key: str, data: bytes) -> None:
        if len(data) > self.max_bytes:
            return
        with self._guard:
            self._evict_idle()
            if key in self._items:
                old_data, _ = self._items.pop(key)
                self._size -= len(old_data)
            while self._size + len(data) > self.max_bytes and self._items:
                _, (oldest_data, _) = self._items.popitem(last=False)
                self._size -= len(oldest_data)
            self._items[key] = (data, time.time())
            self._size += len(data)

    def _evict_idle(self) -> None:
        """Drop entries idle past `idle_seconds`. Caller holds the lock."""
        now = time.time()
        threshold = now - self.idle_seconds
        stale = [k for k, (_, ts) in self._items.items() if ts < threshold]
        for k in stale:
            data, _ = self._items.pop(k)
            self._size -= len(data)


transcript_cache = _IdleLRU(max_bytes=256 * 1024 * 1024, idle_seconds=1200)


class _TTLCache:
    """Process-local cache with a flat TTL, generation-based staleness,
    and an optional cap on the number of entries.

    Values are whatever the decorated endpoint returns (dicts). The
    keyspace is (endpoint × range × project × model); `max_entries`
    bounds it, so keys minted from free-text query values (``model=`` is
    one a guest can set freely) cannot grow the cache without bound.

    Ingest used to ``clear()`` this, which dropped every user onto the
    cold path once an hour — and cold means 8s+ for the dashboard. It now
    calls ``invalidate()``, which bumps a generation counter so existing
    entries read as STALE but stay servable. A stale hit is returned
    immediately and refreshed in the background; the TTL remains the hard
    limit past which an entry is dropped and must be recomputed inline.

    The per-key locks that single-flight a cold compute live here too
    (``lock_for``), and are reclaimed whenever their entry is dropped.
    A lock whose entry was evicted while another thread still holds it
    is kept until its holder releases it; that race only degrades
    single-flighting into a duplicate compute, never into incorrect
    data (last put wins).
    """

    def __init__(self, ttl_seconds: int, *, max_entries: int | None = None):
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._items: dict[str, tuple[Any, float, int]] = {}
        self._generation = 0
        self._guard = threading.Lock()
        self._key_locks: dict[str, threading.Lock] = {}

    def lock_for(self, key: str) -> threading.Lock:
        """Return the single-flight lock for `key`, creating it once."""
        with self._guard:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._key_locks[key] = lock
            return lock

    def _reclaim_lock(self, key: str) -> None:
        """Drop `key`'s lock unless someone is single-flighting on it.

        Caller holds `self._guard`.
        """
        lock = self._key_locks.get(key)
        if lock is not None and lock.acquire(blocking=False):
            lock.release()
            del self._key_locks[key]

    def get_entry(self, key: str) -> tuple[Any, bool] | None:
        """Return ``(value, is_stale)``, or None on miss/expiry."""
        with self._guard:
            item = self._items.get(key)
            if item is None:
                return None
            value, ts, generation = item
            if time.time() - ts > self.ttl_seconds:
                self._items.pop(key, None)
                self._reclaim_lock(key)
                return None
            return value, generation != self._generation

    def get(self, key: str) -> Any | None:
        entry = self.get_entry(key)
        return None if entry is None else entry[0]

    def put(self, key: str, value: Any) -> None:
        with self._guard:
            self._items[key] = (value, time.time(), self._generation)
            cap = self.max_entries
            if cap is not None and len(self._items) > cap:
                self._evict_over_cap(len(self._items) - cap)

    def _evict_over_cap(self, overflow: int) -> None:
        """Drop entries until the cache is within `max_entries`.

        Expired entries go first, then the oldest by timestamp. Caller
        holds `self._guard`.
        """
        now = time.time()
        expired = [
            k for k, (_, ts, _) in self._items.items()
            if now - ts > self.ttl_seconds
        ]
        for k in expired:
            del self._items[k]
            self._reclaim_lock(k)
        overflow -= len(expired)
        if overflow <= 0:
            return
        oldest = sorted(self._items.items(), key=lambda kv: kv[1][1])[:overflow]
        for k, _ in oldest:
            del self._items[k]
            self._reclaim_lock(k)

    def invalidate(self) -> None:
        """Mark every entry stale WITHOUT dropping it."""
        with self._guard:
            self._generation += 1

    def clear(self) -> None:
        with self._guard:
            self._items.clear()
            for key in list(self._key_locks):
                self._reclaim_lock(key)
            self._generation += 1


response_cache = _TTLCache(ttl_seconds=3600, max_entries=256)


# Background refreshes for stale entries. Small pool on purpose: a refresh
# is a full uncached query, and running many at once would starve the
# connection pool that live requests need.
_refresh_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="cache-refresh")
_refreshing: set[str] = set()
_refreshing_guard = threading.Lock()


def _schedule_refresh(key: str, fn: Callable[..., dict], kwargs: dict[str, Any]) -> None:
    """Recompute `key` off the request path, at most once concurrently."""
    with _refreshing_guard:
        if key in _refreshing:
            return
        _refreshing.add(key)

    def _run() -> None:
        try:
            response_cache.put(key, fn(**kwargs))
        except Exception:
            # A failed refresh leaves the stale entry in place, which is
            # the whole point — better stale than a 500 or an 8s wait.
            log.exception("background refresh failed for %s", key)
        finally:
            with _refreshing_guard:
                _refreshing.discard(key)

    _refresh_pool.submit(_run)


def warm(fn: Callable[..., dict], **overrides: Any) -> None:
    """Populate the cache entry a request with `overrides` would produce.

    An ingest leaves Postgres' buffer cache cold — recompute_canonical and
    rebuild_rollup rewrite the tables — and a restart leaves the response
    cache empty on top of that, so the first visitor pays both: measured
    6.0s vs 1.4s warm for /api/dashboard. Stale-while-revalidate cannot
    help there because there is nothing stale to serve yet.

    The key MUST match what the decorated wrapper computes for a real
    request, and FastAPI passes every declared query param as a keyword.
    So start from the endpoint's own defaults and apply the overrides on
    top; guessing the kwargs would warm a key nobody ever reads.
    """
    target = getattr(fn, "__wrapped__", fn)
    kwargs: dict[str, Any] = {}
    for name, param in inspect.signature(target).parameters.items():
        default = param.default
        # Query(...) defaults carry the real value on `.default`.
        kwargs[name] = getattr(default, "default", default)
    kwargs.update(overrides)
    if "fresh" in kwargs:
        # A truthy `fresh` bypasses the cache on both read and write, so
        # warming with it would compute the response and store nothing.
        kwargs["fresh"] = 0

    def _run() -> None:
        try:
            fn(**kwargs)
        except Exception:
            log.exception("cache warm failed for %s %r", fn.__qualname__, overrides)

    _refresh_pool.submit(_run)


def cache_response(fn: Callable[..., dict]) -> Callable[..., dict]:
    """Cache an endpoint's dict result keyed by its keyword args.

    FastAPI calls endpoints with all params as keywords and resolves the
    signature through ``functools.wraps``' ``__wrapped__`` link, so the
    wrapper can keep a ``**kwargs`` signature while FastAPI still parses
    the original query params. A truthy ``fresh`` kwarg bypasses the
    cache (read+write) — only ``/api/dashboard`` declares ``fresh``.

    The decorated endpoints are SYNC (blocking psycopg), so this wrapper
    is sync too and FastAPI runs it in its threadpool. Three behaviours
    matter here:

    - fresh hit  → return it.
    - stale hit  → return it IMMEDIATELY and refresh in the background.
      Serving slightly-old numbers beats blocking a page load for 8s+.
    - miss       → compute inline, single-flighted per key so a burst of
      concurrent cold requests (a dashboard fires ~7 at once) runs the
      query once instead of N times.
    """

    @functools.wraps(fn)
    def wrapper(**kwargs: Any) -> dict:
        if kwargs.get("fresh"):
            return fn(**kwargs)
        key = fn.__qualname__ + ":" + repr(sorted(kwargs.items()))

        entry = response_cache.get_entry(key)
        if entry is not None:
            value, is_stale = entry
            if is_stale:
                _schedule_refresh(key, fn, kwargs)
            return value

        with response_cache.lock_for(key):
            # Another thread may have populated it while we queued.
            entry = response_cache.get_entry(key)
            if entry is not None:
                return entry[0]
            result = fn(**kwargs)
            response_cache.put(key, result)
            return result

    return wrapper
