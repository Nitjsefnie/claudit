from __future__ import annotations

import sys
import threading
import time

import pytest
from fastapi import HTTPException

from backend import cache as cache_mod
from backend.cache import _IdleLRU, _TTLCache, cache_response


def test_ttlcache_put_get():
    c = _TTLCache(ttl_seconds=60)
    c.put("k", {"v": 1})
    assert c.get("k") == {"v": 1}
    assert c.get("missing") is None


def test_ttlcache_expiry():
    c = _TTLCache(ttl_seconds=0)
    c.put("k", {"v": 1})
    time.sleep(0.01)
    assert c.get("k") is None


def test_ttlcache_clear():
    c = _TTLCache(ttl_seconds=60)
    c.put("k", {"v": 1})
    c.clear()
    assert c.get("k") is None


def test_cache_response_decorator_caches_and_bypasses():
    calls = []

    @cache_response
    def endpoint(rng: str = "30d", fresh: int = 0) -> dict:
        calls.append(rng)
        return {"range": rng, "n": len(calls)}

    first = endpoint(rng="30d", fresh=0)
    second = endpoint(rng="30d", fresh=0)
    assert first == second                    # served from cache
    assert len(calls) == 1                    # body ran once

    bypass = endpoint(rng="30d", fresh=1)
    assert len(calls) == 2                    # fresh=1 skips the cache
    assert bypass["n"] == 2


def test_invalidate_serves_stale_then_refreshes():
    """Ingest marks entries stale; a stale hit is served immediately and
    the value is recomputed off the request path."""
    calls = []

    @cache_response
    def endpoint(rng: str = "30d", fresh: int = 0) -> dict:
        calls.append(rng)
        return {"n": len(calls)}

    assert endpoint(rng="30d", fresh=0) == {"n": 1}

    cache_mod.response_cache.invalidate()

    # The stale value comes back straight away — NOT a recomputed one.
    assert endpoint(rng="30d", fresh=0) == {"n": 1}

    # ...and the refresh lands in the background. Poll the CACHE — what
    # the contract governs — not the endpoint's call log: a poll that
    # watches `calls` can exit in the window between the refresh
    # computing its value and that value being put, then the final read
    # serves stale and fails. Serving stale through that window is the
    # contract working, so wait on the cache itself. (Issue #155.)
    deadline = time.time() + 5
    while time.time() < deadline and endpoint(rng="30d", fresh=0) != {"n": 2}:
        time.sleep(0.02)
    assert endpoint(rng="30d", fresh=0) == {"n": 2}, \
        "background refresh never landed"


def test_invalidate_keeps_entries_servable():
    c = _TTLCache(ttl_seconds=60)
    c.put("k", {"v": 1})
    c.invalidate()
    entry = c.get_entry("k")
    assert entry is not None
    value, is_stale = entry
    assert value == {"v": 1}   # still servable, unlike clear()
    assert is_stale is True


# --------------------------------------------------------------------------
# Issue #105: _IdleLRU mutates the shared OrderedDict from get/put/_evict_idle
# with no lock while endpoints run in FastAPI's threadpool.

def test_idle_lru_thread_safety():
    """Concurrent get/put must not raise 'OrderedDict mutated during
    iteration'.

    Deterministic, not sleep-based: one barrier releases every thread at
    once so the accesses genuinely overlap, a shrunken interpreter switch
    interval makes GIL preemption frequent, and every exception is
    collected into a shared list instead of killing its thread silently.
    The cache is sized so the idle-eviction scan iterates a few hundred
    live entries on every put — that iteration is the race window.
    """
    lru = _IdleLRU(max_bytes=65536, idle_seconds=1200)
    errors: list[BaseException] = []
    n_threads = 8
    barrier = threading.Barrier(n_threads)

    def worker(tid: int) -> None:
        try:
            barrier.wait(timeout=10)
            for i in range(2000):
                key = f"k{tid}-{i % 50}"
                if i % 2:
                    lru.get(key)
                else:
                    lru.put(key, b"x" * 32)
        except BaseException as exc:  # the test IS the handler here
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(tid,), name=f"idle-lru-{tid}")
        for tid in range(n_threads)
    ]
    old_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
    finally:
        sys.setswitchinterval(old_interval)
    assert not any(t.is_alive() for t in threads), "a worker hung"
    assert not errors


# --------------------------------------------------------------------------
# Issue #113: put() inserts unconditionally, so an entry larger than the
# size cap stays resident above the advertised 256 MB.

def test_idle_lru_refuses_oversized_entry():
    """A value larger than max_bytes is not cached at all, and an existing
    entry under the same key is untouched by an oversized put."""
    lru = _IdleLRU(max_bytes=1024, idle_seconds=1200)

    lru.put("big", b"x" * 2048)
    assert lru.get("big") is None
    assert lru._size == 0  # pylint: disable=protected-access
    assert len(lru._items) == 0  # pylint: disable=protected-access

    lru.put("k", b"small")
    lru.put("k", b"x" * 2048)          # oversized put for an existing key
    assert lru.get("k") == b"small"    # the old entry still stands
    assert lru._size == len(b"small")  # pylint: disable=protected-access


# --------------------------------------------------------------------------
# Issue #269: the orphan sweep must be able to drop one cached transcript
# explicitly, so deleted source bytes do not stay served from the cache.

def test_idle_lru_evict_drops_entry_and_byte_count():
    """evict() pops one key and fixes the size accounting; a missing key
    is a no-op, and the surviving entry keeps its bytes counted."""
    lru = _IdleLRU(max_bytes=1024, idle_seconds=1200)
    lru.put("gone", b"x" * 300)
    lru.put("kept", b"y" * 200)
    assert lru._size == 500  # pylint: disable=protected-access

    lru.evict("gone")
    assert lru.get("gone") is None
    assert lru.get("kept") == b"y" * 200
    assert lru._size == 200  # pylint: disable=protected-access
    assert len(lru._items) == 1  # pylint: disable=protected-access

    lru.evict("missing")  # no-op, no raise
    assert lru._size == 200  # pylint: disable=protected-access
    assert len(lru._items) == 1  # pylint: disable=protected-access


# --------------------------------------------------------------------------
# Issue #96: the response cache's keyspace and its per-key locks grew
# without bound. The fix gives _TTLCache a max_entries cap and moves the
# per-key locks inside it, so a lock is reclaimed when its entry is dropped.

def test_ttl_cache_cap_evicts_expired_then_oldest(monkeypatch):
    """Over cap, expired entries go first, then the oldest by timestamp.

    Time is controlled deterministically by pinning time.time to a clock
    the test advances.
    """
    clock = {"t": 1000.0}
    monkeypatch.setattr(cache_mod.time, "time", lambda: clock["t"])
    c = _TTLCache(ttl_seconds=60, max_entries=3)

    c.put("e", "E")                    # t=1000; will expire
    clock["t"] = 1035.0
    c.put("f", "F")
    clock["t"] = 1070.0
    c.put("g", "G")
    clock["t"] = 1090.0
    c.put("h", "H")                    # 4 entries > cap 3; only "e" is expired

    assert c.get("e") is None          # the expired entry went first
    assert c.get("f") == "F"           # fresh beats expired
    assert c.get("g") == "G"
    assert c.get("h") == "H"
    assert len(c._items) == 3  # pylint: disable=protected-access

    clock["t"] = 1091.0
    c.put("i", "I")                    # over cap again; nothing expired now
    assert c.get("f") is None          # the oldest entry went instead
    assert c.get("g") == "G"
    assert c.get("h") == "H"
    assert c.get("i") == "I"
    assert len(c._items) == 3  # pylint: disable=protected-access


def test_ttl_cache_evicts_key_lock_with_entry(monkeypatch):
    """A key's lock is reclaimed whenever its entry is dropped: on the
    expiry pop, on cap eviction, and on clear()."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(cache_mod.time, "time", lambda: clock["t"])
    c = _TTLCache(ttl_seconds=60, max_entries=2)

    # expiry pop in get_entry
    c.put("exp", 1)
    exp_lock = c.lock_for("exp")
    clock["t"] += 1000.0
    assert c.get("exp") is None
    assert "exp" not in c._key_locks  # pylint: disable=protected-access
    assert c.lock_for("exp") is not exp_lock      # a new lock was minted

    # cap eviction in put
    c.put("cap1", 1)
    c.lock_for("cap1")
    clock["t"] += 1.0
    c.put("cap2", 2)
    c.lock_for("cap2")
    clock["t"] += 1.0
    c.put("cap3", 3)                  # over cap: oldest (cap1) is evicted
    c.lock_for("cap3")
    assert "cap1" not in c._key_locks  # pylint: disable=protected-access
    assert "cap2" in c._key_locks  # pylint: disable=protected-access
    assert "cap3" in c._key_locks  # pylint: disable=protected-access

    # clear()
    c.lock_for("solo")
    c.clear()
    assert len(c._key_locks) == 0  # pylint: disable=protected-access


def test_ttl_cache_keeps_held_lock_through_eviction(monkeypatch):
    """Reclaim is non-blocking: a lock someone still holds survives the
    sweep that drops its entry. Single-flighting then degrades to a
    duplicate compute, never to incorrect data (last put wins)."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(cache_mod.time, "time", lambda: clock["t"])

    # expiry pop while the lock is held
    c = _TTLCache(ttl_seconds=60)
    c.put("k", 1)
    held = c.lock_for("k")
    assert held.acquire(blocking=False)
    clock["t"] += 1000.0
    assert c.get("k") is None
    assert c.lock_for("k") is held    # kept — someone is on it
    held.release()

    # clear() while the lock is held
    c2 = _TTLCache(ttl_seconds=60)
    c2.put("k", 1)
    held2 = c2.lock_for("k")
    assert held2.acquire(blocking=False)
    c2.clear()
    assert c2.lock_for("k") is held2  # kept through the reclaim sweep
    held2.release()


def test_response_cache_keys_bounded_under_free_text_churn():
    """model= is free text a guest can mint freely (POST /login/guest), so
    keys minted from it must not grow the response cache without bound."""
    saved = cache_mod.response_cache
    cache_mod.response_cache = _TTLCache(ttl_seconds=3600, max_entries=8)
    try:

        @cache_response
        def endpoint(model: str = "", fresh: int = 0) -> dict:
            return {"model": model}

        for i in range(32):
            endpoint(model=f"guest-model-{i}")
        assert len(cache_mod.response_cache._items) <= 8  # pylint: disable=protected-access
    finally:
        cache_mod.response_cache = saved


def test_response_cache_is_capped():
    """The shipped response cache itself carries the cap."""
    assert cache_mod.response_cache.max_entries is not None


# --------------------------------------------------------------------------
# Issue #261: failed response-cache computes leave no entry to trigger the
# existing per-key lock reclamation paths.

def test_failed_compute_leaves_no_lock_behind(monkeypatch):
    """A failed decorated compute propagates and releases its empty key."""
    c = _TTLCache(ttl_seconds=3600, max_entries=256)
    monkeypatch.setattr(cache_mod, "response_cache", c)

    @cache_response
    def endpoint(rng: str) -> dict:
        raise HTTPException(status_code=400, detail="invalid range")

    with pytest.raises(HTTPException, match="invalid range"):
        endpoint(rng="invalid")

    assert len(c._key_locks) == 0  # pylint: disable=protected-access
    assert len(c._items) == 0  # pylint: disable=protected-access


def test_distinct_failing_requests_do_not_accumulate_locks(monkeypatch):
    """Distinct failing query values leave no locks or entries behind."""
    c = _TTLCache(ttl_seconds=3600, max_entries=256)
    monkeypatch.setattr(cache_mod, "response_cache", c)

    @cache_response
    def endpoint(rng: str) -> dict:
        if rng == "valid":
            return {"range": rng}
        raise HTTPException(status_code=400, detail="invalid range")

    for i in range(300):
        with pytest.raises(HTTPException, match="invalid range"):
            endpoint(rng=f"invalid-{i}")
        assert len(c._key_locks) == 0  # pylint: disable=protected-access
        assert len(c._items) == 0  # pylint: disable=protected-access

    assert endpoint(rng="valid") == {"range": "valid"}
    assert len(c._items) == 1  # pylint: disable=protected-access


def test_same_key_retry_after_failed_compute_reclaims_lock(monkeypatch):
    """A second failed compute on the same key gets its own error cleanly."""
    c = _TTLCache(ttl_seconds=3600, max_entries=256)
    monkeypatch.setattr(cache_mod, "response_cache", c)

    @cache_response
    def endpoint(rng: str) -> dict:
        raise HTTPException(status_code=400, detail="invalid range")

    for _ in range(2):
        with pytest.raises(HTTPException, match="invalid range"):
            endpoint(rng="invalid")
        assert len(c._key_locks) == 0  # pylint: disable=protected-access
        assert len(c._items) == 0  # pylint: disable=protected-access


# --------------------------------------------------------------------------
# Issue #371: an entry whose compute straddles an invalidate() must be
# STORED stale, so the next read refreshes it instead of serving
# pre-ingest data as current until the TTL expires.

def test_put_with_an_older_generation_is_stored_stale():
    """put() stamps the generation the compute SAW, not the one current
    when it returned; the default stays the put-time generation."""
    c = _TTLCache(ttl_seconds=60)
    c.invalidate()
    c.put("k", {"v": 1}, generation=0)
    assert c.get_entry("k") == ({"v": 1}, True), (
        "an entry from the pre-invalidate generation is born stale")
    c.put("k", {"v": 2})
    assert c.get_entry("k") == ({"v": 2}, False)


def test_inline_miss_across_an_invalidate_is_stored_stale(monkeypatch):
    """A compute that starts before the ingest's invalidate() and returns
    after it must not be stored fresh: the wrapper captures the
    generation before calling the endpoint and stamps the entry with it."""
    c = _TTLCache(ttl_seconds=3600)
    monkeypatch.setattr(cache_mod, "response_cache", c)
    calls = []

    @cache_response
    def endpoint() -> dict:
        calls.append(1)
        c.invalidate()  # the ingest finishes mid-compute
        return {"v": "old"}

    assert endpoint() == {"v": "old"}
    key = endpoint.__qualname__ + ":[]"
    assert c.get_entry(key) == ({"v": "old"}, True), (
        "an entry computed across the invalidation is born stale")


def test_background_refresh_across_an_invalidate_is_stored_stale(monkeypatch):
    """The same race on the stale-while-revalidate path: a refresh whose
    compute straddles an invalidate() is stored stale, and the served
    value stays the stale one."""
    class _InlinePool:
        def submit(self, fn, *args, **kwargs):
            fn(*args, **kwargs)

    c = _TTLCache(ttl_seconds=3600)
    monkeypatch.setattr(cache_mod, "response_cache", c)
    monkeypatch.setattr(cache_mod, "_refresh_pool", _InlinePool())
    calls = []

    @cache_response
    def endpoint(rng: str = "30d", fresh: int = 0) -> dict:
        calls.append(1)
        if len(calls) == 2:
            c.invalidate()  # the ingest finishes mid-refresh
        return {"n": len(calls)}

    assert endpoint(rng="30d", fresh=0) == {"n": 1}
    c.invalidate()  # mark the entry stale
    assert endpoint(rng="30d", fresh=0) == {"n": 1}, (
        "the stale value is still served while the refresh runs")
    key = endpoint.__qualname__ + ":" + repr(
        sorted({"rng": "30d", "fresh": 0}.items()))
    assert c.get_entry(key) == ({"n": 2}, True), (
        "a refresh that computed across the invalidation is born stale")
