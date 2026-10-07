"""In-process caches:
  - transcript LRU keyed by (file_key, etag), 256 MB, 20-min idle eviction
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

from backend.cache_outcomes import OutcomeCounters

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

    def evict(self, key: str) -> None:
        """Drop one entry outright, fixing the byte/entry accounting.

        The orphan sweep calls this for each deleted file's composite
        key from `transcript_key` (issue #269): the transcript bytes are
        keyed by (file_key, etag), so a file deleted from the bucket
        must not stay readable from the cache once its DB rows are gone.
        A missing key is a no-op.
        """
        with self._guard:
            item = self._items.pop(key, None)
            if item is not None:
                self._size -= len(item[0])

    def _evict_idle(self) -> None:
        """Drop entries idle past `idle_seconds`. Caller holds the lock."""
        now = time.time()
        threshold = now - self.idle_seconds
        stale = [k for k, (_, ts) in self._items.items() if ts < threshold]
        for k in stale:
            data, _ = self._items.pop(k)
            self._size -= len(data)


transcript_cache = _IdleLRU(max_bytes=256 * 1024 * 1024, idle_seconds=1200)


def transcript_key(file_key: str, etag: str) -> str:
    """The transcript-cache key for one stored object (issue #375).

    The key must identify the OBJECT, never the validator alone: in
    file:// mode the etag is sha1 over mtime and size alone
    (r2._list_keys_file), so two transcripts of equal size and mtime
    share an etag, and an etag-only key served one session's transcript
    for another. The bucket-qualified file key supplies the object
    identity; the etag stays in the key as the validator, so an object
    replaced under its key (new etag → new key) cannot serve its
    predecessor's stale bytes.

    The `:` separator is unambiguous: an etag never contains one (hex,
    or hex with a `-N` multipart suffix).
    """
    return f"{file_key}:{etag}"


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
    immediately and refreshed in the background; the TTL is a soft bound
    on served age, not a drop: an entry past it is served stale exactly
    like a generation-stale one, because dropping it put the request on
    the cold inline path — the cold build a first open after a quiet gap
    paid (issue #641). The ``max_entries`` cap stays the hard bound on
    memory, and it is the only thing that drops an entry while it is
    being served.

    The per-key locks that single-flight a cold compute live here too
    (``lock_for``), and are reclaimed whenever their entry is dropped or
    a compute raises (issue #261), keeping locks bounded by the same cap
    discipline as entries on both the success and failure paths.
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
        self.counters = OutcomeCounters()

    def outcomes(self) -> dict[str, int]:
        """Every outcome counter, zero included, so a /health scraper
        never distinguishes 'zero' from 'missing key'."""
        return self.counters.outcomes()

    def count(self, **deltas: int) -> None:
        """Count request/background outcomes (see cache_outcomes)."""
        self.counters.count(**deltas)

    def _take_since(self) -> dict[str, int]:
        """Return and zero the since-last-invalidate window."""
        return self.counters.take_window()

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

    def reclaim_failed_lock(self, key: str) -> None:
        """Reclaim a failed compute's idle single-flight lock.

        cache_response calls this after an endpoint raised and stored no
        entry: the lock (and its key string) would otherwise remain for the
        process lifetime, since every other reclamation rides an entry being dropped
        (issue #261). The non-blocking acquire test inside _reclaim_lock
        keeps a lock another thread started single-flighting on in the
        meantime — that race only degrades single-flighting, never data.
        """
        with self._guard:
            self._reclaim_lock(key)

    def get_entry(self, key: str) -> tuple[Any, bool] | None:
        """Return ``(value, is_stale)``, or None on miss.

        An entry past its TTL reads STALE, not absent: serving it costs
        nothing and the recompute moves off the request path, where
        dropping it put every first open after a quiet gap (issue #641).
        The entry heals when the refresh's put re-stamps its timestamp.
        """
        with self._guard:
            item = self._items.get(key)
            if item is None:
                return None
            value, ts, generation = item
            if time.time() - ts > self.ttl_seconds:
                return value, True
            return value, generation != self._generation

    def get(self, key: str) -> Any | None:
        """The raw value, or None when absent OR past its TTL — the unit
        accessor, not a serving path (the serving paths read
        ``get_entry``, which serves the expired entry stale)."""
        with self._guard:
            item = self._items.get(key)
            if item is None:
                return None
            value, ts, _gen = item
            if time.time() - ts > self.ttl_seconds:
                return None
            return value

    def current_generation(self) -> int:
        """The generation a compute starting now must stamp its entry with.

        An entry stamped with anything older reads stale on its first
        get_entry, because invalidate() fired while it was computing —
        its value was derived from pre-ingest data (issue #371).
        """
        with self._guard:
            return self._generation

    def put(self, key: str, value: Any,
            generation: int | None = None) -> None:
        """Store an entry, stamped with the generation the COMPUTE saw.

        The optional `generation` is the value current_generation()
        returned before the compute started (issue #371): an invalidate()
        that lands mid-compute leaves the entry stamped with the older
        generation, so it reads stale immediately instead of presenting
        pre-ingest data as fresh until the TTL expires. None stamps the
        generation current at put time (direct puts of a value just
        read under the current generation).
        """
        with self._guard:
            self._items[key] = (
                value, time.time(),
                self._generation if generation is None else generation)
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
        """Mark every entry stale WITHOUT dropping it.

        Also logs the outcome counters accumulated since the last
        invalidate, so the journal carries the cache outcomes of one
        inter-ingest window per line — the split by ingest overlap the
        slow-open investigation needs, with no post-hoc joining (issue
        #641).
        """
        with self._guard:
            self._generation += 1
        log.info("response-cache outcomes since the last invalidate: %s",
                 self._take_since())

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
            entry = response_cache.get_entry(key)
            if entry is not None and not entry[1]:
                # A warm (or another refresh) healed this key while ours
                # sat queued behind them: recomputing would double a full
                # build (issue #641).
                response_cache.count(refresh_skip_fresh=1)
                return
            # Capture BEFORE the compute: an invalidate() landing mid-run
            # must leave the entry born stale (issue #371).
            generation = response_cache.current_generation()
            response_cache.put(key, fn(**kwargs), generation=generation)
            response_cache.count(refresh_run=1)
        except Exception:
            # A failed refresh leaves the stale entry in place, which is
            # the whole point — better stale than a 500 or an 8s wait.
            response_cache.count(refresh_error=1)
            log.exception("background refresh failed for %s", key)
        finally:
            with _refreshing_guard:
                _refreshing.discard(key)

    _refresh_pool.submit(_run)


def _cache_key(fn: Callable[..., dict], kwargs: dict[str, Any]) -> str:
    """The response-cache key for `fn(**kwargs)`.

    `cache_response` and `warm` must agree on it exactly -- a warm that
    derives its own key warms a key no request reads (issue #641, and
    pitfall 07 of the performance field guide) -- so both build it here.
    `functools.wraps` copies `__qualname__`, so the wrapper `warm` is
    handed and the raw function `cache_response` decorates name the same
    key.
    """
    return fn.__qualname__ + ":" + repr(sorted(kwargs.items()))


def warm(fn: Callable[..., dict], **overrides: Any) -> None:
    """Populate the cache entry a request with `overrides` would produce.

    An ingest leaves Postgres' buffer cache cold — recompute_canonical and
    rebuild_rollup rewrite the tables — and a restart leaves the response
    cache empty on top of that, so the first visitor pays both: measured
    6.0s vs 1.4s warm for /api/dashboard. After an ingest the entries are
    still there but STALE, so a request is served the old value and the
    recompute is deferred; after a restart there is nothing to serve and
    the request computes inline. This is what makes the warmed value worth
    having: it is the fresh one, not merely a present one.

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
        key = _cache_key(fn, kwargs)
        try:
            # Recompute HERE, not by calling the decorated `fn`: on the
            # stale entries an ingest's `invalidate()` leaves behind, a
            # decorated call returns the old value immediately and defers
            # the recompute to `_schedule_refresh`, so the warm consumed a
            # refresh-pool slot, queued a second one behind every other
            # warm already queued, and left the entry stale (issue #641).
            # The single-flight lock is still taken, so a request that
            # misses the same key waits for this compute instead of
            # duplicating it.
            with response_cache.lock_for(key):
                entry = response_cache.get_entry(key)
                if entry is not None and not entry[1]:
                    response_cache.count(warm_skip_fresh=1)
                    return  # someone beat us to it; nothing left to warm
                # Claim the key in `_refreshing` for the duration of the
                # compute. The entry is still stale, so a request arriving
                # now is served the old value and schedules a refresh of
                # this same key; without the claim the warm and that
                # refresh exclude neither and the key computes twice
                # (issue #641, review).
                with _refreshing_guard:
                    if key in _refreshing:
                        response_cache.count(warm_skip_inflight=1)
                        return  # a refresh is already computing this key
                    _refreshing.add(key)
                try:
                    generation = response_cache.current_generation()
                    response_cache.put(
                        key, target(**kwargs), generation=generation)
                    response_cache.count(warm_run=1)
                finally:
                    with _refreshing_guard:
                        _refreshing.discard(key)
        except Exception:
            # A failed compute stores no entry, so neither reclamation path
            # that rides `_items` (expiry in `get_entry`, `_evict_over_cap`)
            # ever runs, and `max_entries` bounds `_items`, not `_key_locks`.
            # Without this the warm leaks a lock per failing key — and it
            # runs right after `invalidate()`, where the database is
            # busiest and a blip is likeliest.
            response_cache.count(warm_error=1)
            response_cache.reclaim_failed_lock(key)
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
      An entry past its TTL takes this path too (issue #641).
    - miss       → compute inline, single-flighted per key so a burst of
      concurrent cold requests (a dashboard fires ~7 at once) runs the
      query once instead of N times.
    """

    @functools.wraps(fn)
    def wrapper(**kwargs: Any) -> dict:
        if kwargs.get("fresh"):
            return fn(**kwargs)
        key = _cache_key(fn, kwargs)

        entry = response_cache.get_entry(key)
        if entry is not None:
            value, is_stale = entry
            if is_stale:
                response_cache.count(stale_hit=1)
                _schedule_refresh(key, fn, kwargs)
            else:
                response_cache.count(fresh_hit=1)
            return value

        try:
            with response_cache.lock_for(key):
                # Another thread may have populated it while we queued.
                entry = response_cache.get_entry(key)
                if entry is not None:
                    response_cache.count(miss_waited=1)
                    return entry[0]
                # Capture BEFORE the compute (issue #371): an invalidate()
                # landing mid-compute must leave the entry born stale.
                generation = response_cache.current_generation()
                result = fn(**kwargs)
                response_cache.put(key, result, generation=generation)
                response_cache.count(miss_inline=1)
                return result
        except BaseException:  # noqa: BLE001
            response_cache.count(miss_error=1)
            response_cache.reclaim_failed_lock(key)
            raise

    return wrapper
