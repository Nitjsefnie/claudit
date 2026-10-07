"""Issue #641 tests: cache outcome counters, and the TTL-expired entry
served stale instead of dropped onto the cold path.
"""
from __future__ import annotations

import ast
import importlib
import json
import logging
import threading
from types import SimpleNamespace

from backend import cache as cache_mod
from backend import db as db_mod
import backend.ingest_progress as progress_mod
from backend.cache import _TTLCache, cache_response


class _Pool:
    """Records submitted tasks instead of running them."""

    def __init__(self):
        self.tasks = []
        self.submitted = 0

    def submit(self, fn, *args, **kwargs):
        self.submitted += 1
        self.tasks.append(lambda: fn(**kwargs))

    def run_all(self):
        while self.tasks:
            self.tasks.pop(0)()


def _key_for(fn, kwargs):
    return fn.__qualname__ + ":" + repr(sorted(kwargs.items()))


def _make_cache(monkeypatch, ttl_seconds=3600):
    """A fresh response cache on the module seam, refreshes queued until
    the test drains them."""
    c = _TTLCache(ttl_seconds=ttl_seconds)
    monkeypatch.setattr(cache_mod, "response_cache", c)
    pool = _Pool()
    monkeypatch.setattr(cache_mod, "_refresh_pool", pool)  # noqa: SLF001
    return c, pool


def _seeded_endpoint(calls, name):
    @cache_response
    def endpoint(rng: str = "all") -> dict:
        calls.append(1)
        return {"n": len(calls)}

    # The key is built from the RAW function's qualname (the wrapper
    # reads its closure); both sides carry the test's name.
    endpoint.__qualname__ = name
    endpoint.__wrapped__.__qualname__ = name  # pylint: disable=protected-access
    return endpoint


def test_expired_entry_serves_stale_instead_of_a_cold_miss(monkeypatch):
    clock = {"t": 1000.0}
    monkeypatch.setattr(cache_mod.time, "time", lambda: clock["t"])
    c, pool = _make_cache(monkeypatch, ttl_seconds=60)
    calls = []
    endpoint = _seeded_endpoint(calls, "test_expired_entry_serves_stale_instead_of_a_cold_miss")

    endpoint(rng="all")          # seed the entry
    clock["t"] += 61.0           # past the TTL
    endpoint(rng="all")          # must SERVE the old value, not recompute
    assert calls == [1], "a TTL-expired entry was dropped onto the cold path"

    pool.run_all()
    assert calls == [1, 1], "the deferred refresh never ran"
    entry = c.get_entry(_key_for(endpoint, {"rng": "all"}))
    assert entry is not None and entry[1] is False, (
        "the refresh did not heal the entry back to fresh")


def test_expired_entry_schedules_exactly_one_refresh(monkeypatch):
    """Repeated serves of the same expired entry do not stack refreshes:
    the claim dedups them until the refresh's put heals the entry."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(cache_mod.time, "time", lambda: clock["t"])
    c, pool = _make_cache(monkeypatch, ttl_seconds=60)
    calls = []
    endpoint = _seeded_endpoint(calls, "test_expired_entry_schedules_exactly_one_refresh")

    endpoint(rng="all")          # seed
    clock["t"] += 61.0
    endpoint(rng="all")          # expired -> stale serve + refresh claim
    endpoint(rng="all")          # second serve while the claim is held
    del calls[:]                 # outcome asserts read only the new calls

    pool.run_all()
    assert calls == [1], (
        f"the expired entry scheduled {len(calls)} refresh compute(s)")
    entry = c.get_entry(_key_for(endpoint, {"rng": "all"}))
    assert entry is not None, "the refresh dropped the entry outright"
    assert entry[1] is False, "the refresh never healed the entry"


# ---------------------------------------------------------------------------
# The instrument: outcome counters on the response cache. Load cannot move
# a count, so the production journal gives the slow-open split the issue
# asks for: outcomes logged at every invalidate (one line per changed
# ingest), cumulative in /health.

def test_outcomes_discriminate_request_paths(monkeypatch):
    c, pool = _make_cache(monkeypatch)
    calls = []
    endpoint = _seeded_endpoint(calls, "test_outcomes_discriminate_request_paths")

    endpoint(rng="30d")            # miss -> inline compute
    endpoint(rng="30d")            # fresh hit
    c.invalidate()
    endpoint(rng="30d")            # stale hit (+ refresh queued, unrun)

    outcomes = c.outcomes()
    assert outcomes["miss_inline"] == 1
    assert outcomes["fresh_hit"] == 1
    assert outcomes["stale_hit"] == 1
    pool.run_all()
    assert c.outcomes()["refresh_run"] == 1

    # miss_waited: a request whose key has NO entry and whose single-flight
    # lock another actor holds while the entry appears. A second endpoint
    # keeps that key free of the entry the earlier calls left behind.
    calls2 = []

    @cache_response
    def endpoint2(rng: str = "30d") -> dict:
        calls2.append(1)
        return {"n": len(calls2)}

    key2 = _key_for(endpoint2, {"rng": "30d"})
    lock = c.lock_for(key2)
    lock.acquire()
    started = threading.Event()
    th = threading.Thread(
        target=lambda: (started.set(), endpoint2(rng="30d")))
    th.start()
    assert started.wait(5), "the blocking request never started"
    c.put(key2, {"n": 42})
    lock.release()
    th.join(5)
    assert not th.is_alive(), "the blocking request hung on the key lock"
    assert c.outcomes()["miss_waited"] == 1
    assert not calls2, (
        "the request computed even though another actor had just stored "
        "the entry")


def test_outcomes_vocabulary_is_stable_for_health_scrapers():
    """outcomes() names every outcome it can ever report, zero included: a
    /health scraper must not distinguish 'zero' from 'missing key'."""
    c = _TTLCache(ttl_seconds=60)
    assert set(c.outcomes()) == {
        "fresh_hit", "stale_hit", "miss_inline", "miss_waited", "miss_error",
        "refresh_run", "refresh_skip_fresh", "refresh_error",
        "warm_run", "warm_skip_fresh", "warm_skip_inflight", "warm_error",
    }


def test_invalidate_logs_the_outcome_window(monkeypatch, caplog):
    """One log line per invalidate, carrying the outcomes since the last
    invalidate: the journal then gives cache outcomes split by ingest
    overlap with no post-hoc joining."""
    c, _pool = _make_cache(monkeypatch)
    calls = []
    endpoint = _seeded_endpoint(calls, "test_invalidate_logs_the_outcome_window")

    with caplog.at_level(logging.INFO, logger="claudit.cache"):
        endpoint(rng="all")        # miss_inline
        endpoint(rng="all")        # fresh_hit
        c.invalidate()
        window = [
            r for r in caplog.records
            if r.name == "claudit.cache" and "fresh_hit" in r.getMessage()
        ]
        assert window, "the invalidate logged no outcome window"

        # The window resets: the next invalidate reports only what
        # happened since.
        caplog.clear()
        endpoint(rng="all")        # stale_hit (the first invalidate staled it)
        c.invalidate()
        second = [
            ast.literal_eval(r.getMessage().split(": ", 1)[1])
            for r in caplog.records
            if r.name == "claudit.cache" and "outcome" in r.getMessage()
        ]
    assert len(second) == 1, second
    assert second[0]["miss_inline"] == 0, (
        "the outcome window did not reset at the last invalidate")
    assert second[0]["stale_hit"] == 1


# ---------------------------------------------------------------------------
# The refresh double-compute fix: a queued refresh recomputed a key the
# warm had already refreshed — every SSE-driven refetch after an ingest
# queued one per key, behind the warms on the two-worker pool.

def test_scheduled_refresh_skips_when_the_entry_is_already_fresh(monkeypatch):
    c, pool = _make_cache(monkeypatch)
    calls = []
    endpoint = _seeded_endpoint(calls, "test_scheduled_refresh_skips_when_the_entry_is_already_fresh")

    endpoint(rng="all")            # seed
    c.invalidate()                 # the ingest
    endpoint(rng="all")            # stale serve + refresh queued (unrun)
    # Another actor (a warm) heals the entry while the refresh sits queued:
    c.put(_key_for(endpoint, {"rng": "all"}), {"n": 99})
    pool.run_all()

    assert calls == [1], (
        f"the queued refresh recomputed a fresh entry "
        f"({len(calls) - 1} wasted compute(s))")
    assert c.outcomes()["refresh_skip_fresh"] == 1


def test_scheduled_refresh_still_computes_a_stale_entry(monkeypatch):
    """The skip must not eat the compute it exists to dedup: a refresh
    dequeued while its entry is still stale recomputes."""
    c, pool = _make_cache(monkeypatch)
    calls = []
    endpoint = _seeded_endpoint(calls, "test_scheduled_refresh_still_computes_a_stale_entry")

    endpoint(rng="all")
    c.invalidate()
    endpoint(rng="all")            # stale serve + refresh queued
    pool.run_all()
    assert calls == [1, 1], "the skip ate a needed compute"
    assert c.outcomes()["refresh_run"] == 1


# ---------------------------------------------------------------------------
# /health carries the readout: cumulative outcome counters and the
# connection-pool gauges, so the slow-open split and the pool-queue
# hypothesis are both answerable from production without a deploy.

def test_health_carries_the_cache_block(monkeypatch):
    app_mod = importlib.import_module("backend.app")

    class _FakeCursor:
        @staticmethod
        def fetchone():
            return None

    class _FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        @staticmethod
        def execute(*_args, **_kwargs):
            return _FakeCursor()

    _c, _pool = _make_cache(monkeypatch)
    calls = []
    endpoint = _seeded_endpoint(calls, "test_health_carries_the_cache_block")
    endpoint(rng="all")            # miss_inline
    endpoint(rng="all")            # fresh_hit

    monkeypatch.setattr(app_mod.db, "viz_conn", _FakeConn)
    monkeypatch.setattr(progress_mod, "progress_snapshot",
                        lambda: {"phase": "idle"})
    monkeypatch.setattr(app_mod.db, "viz_pool_statistics",
                        lambda: {"pool_available": 3, "pool_max": 20,
                                 "pool_size": 5, "requests_waiting": 0})

    response = app_mod.health()
    payload = json.loads(response.body)

    assert response.status_code == 200
    assert payload["cache"]["outcomes"]["fresh_hit"] == 1
    assert payload["cache"]["outcomes"]["miss_inline"] == 1
    assert payload["cache"]["pool"]["requests_waiting"] == 0


def test_viz_pool_statistics_degrades_to_empty(monkeypatch):
    def _boom():
        raise RuntimeError("no pool")

    monkeypatch.setattr(db_mod, "viz_pool", _boom)
    assert db_mod.viz_pool_statistics() == {}

    stub = SimpleNamespace(get_stats=lambda: {"pool_min": 2, "pool_max": 20,
                                              "pool_size": 4,
                                              "pool_available": 3,
                                              "requests_waiting": 1})
    monkeypatch.setattr(db_mod, "viz_pool", lambda: stub)
    assert db_mod.viz_pool_statistics() == {
        "pool_available": 3, "pool_max": 20, "pool_size": 4,
        "requests_waiting": 1,
    }


# ---------------------------------------------------------------------------
# F1 follow-ups from review: the remaining count sites get their own
# controls. F2 rides along: a failed cold compute counts miss_error.

def test_warm_skips_a_key_already_claimed_by_a_refresh(monkeypatch):
    """warm() must not compute a key a scheduled refresh already claimed:
    the claim branch counts warm_skip_inflight and runs nothing."""
    c, pool = _make_cache(monkeypatch)
    calls = []
    endpoint = _seeded_endpoint(calls, "warm-inflight")

    endpoint(rng="all")            # seed
    c.invalidate()                 # stale, so the freshness check passes
    key = _key_for(endpoint, {"rng": "all"})
    claim = cache_mod._refreshing  # pylint: disable=protected-access
    claim.add(key)                 # what a queued refresh holds
    try:
        run_before = c.outcomes()["warm_run"]
        cache_mod.warm(endpoint, rng="all")
        pool.run_all()
        outcomes = c.outcomes()
        assert outcomes["warm_skip_inflight"] == 1, (
            "the warm ran into a held claim and counted nothing")
        assert outcomes["warm_run"] - run_before == 0, (
            "the warm computed despite the held claim")
        assert calls == [1], "the warm computed despite the held claim"
    finally:
        claim.discard(key)


def test_failed_refresh_counts_refresh_error(monkeypatch):
    """A refresh whose compute raises counts refresh_error and leaves the
    stale entry in place — better stale than a 500 (issue #371's shape)."""
    c, pool = _make_cache(monkeypatch)

    @cache_response
    def broken(rng: str = "all") -> dict:
        raise RuntimeError("refresh compute failed")

    broken.__qualname__ = "refresh-error-broken"
    broken.__wrapped__.__qualname__ = (  # pylint: disable=protected-access
        "refresh-error-broken")

    # Seed a fresh entry through the cache, so the stale serve below
    # schedules a refresh of the broken compute.
    c.put(_key_for(broken, {"rng": "all"}), {"n": 1})
    c.invalidate()
    assert broken(rng="all") == {"n": 1}, (
        "the stale serve did not return the seeded value")

    pool.run_all()
    assert c.outcomes()["refresh_error"] == 1, (
        "the failed refresh counted no refresh_error outcome")
    entry = c.get_entry(_key_for(broken, {"rng": "all"}))
    assert entry == ({"n": 1}, True), (
        "the failed refresh did not leave the stale entry in place")


def test_failed_cold_compute_counts_miss_error(monkeypatch):
    """A cold compute that raises counts miss_error (issue #641 review
    F2): a burst of failing cold opens must not read as nothing."""
    c, _pool = _make_cache(monkeypatch)

    @cache_response
    def broken(rng: str = "all") -> dict:
        raise RuntimeError("cold compute failed")

    broken.__qualname__ = "miss-error-broken"
    broken.__wrapped__.__qualname__ = (  # pylint: disable=protected-access
        "miss-error-broken")
    try:
        broken(rng="all")
    except RuntimeError:
        pass
    else:
        raise AssertionError("the broken compute did not raise")
    assert c.outcomes()["miss_error"] == 1
