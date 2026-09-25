from __future__ import annotations

import sys
import threading
import time

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

    # ...and the refresh lands in the background.
    deadline = time.time() + 5
    while time.time() < deadline and len(calls) < 2:
        time.sleep(0.02)
    assert len(calls) == 2, "background refresh never ran"
    assert endpoint(rng="30d", fresh=0) == {"n": 2}


def test_invalidate_keeps_entries_servable():
    c = _TTLCache(ttl_seconds=60)
    c.put("k", {"v": 1})
    c.invalidate()
    entry = c.get_entry("k")
    assert entry is not None
    value, is_stale = entry
    assert value == {"v": 1}   # still servable, unlike clear()
    assert is_stale is True


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
