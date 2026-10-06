"""The disk blob cache (issue #684): (key, etag)-keyed, best-effort on
every failure, size-capped LRU prune.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from backend import blob_cache


@pytest.fixture(name="cache_root")
def _cache_root(monkeypatch, tmp_path):
    root = tmp_path / "blob-cache"
    monkeypatch.setenv("R2_BLOB_CACHE", str(root))
    return root


def test_disabled_when_unset(monkeypatch):
    monkeypatch.delenv("R2_BLOB_CACHE", raising=False)
    assert not blob_cache.enabled()
    assert blob_cache.lookup("claude/k", "e", 1) is None
    assert blob_cache.store("claude/k", "e", b"x") is False
    assert blob_cache.prune() == 0


def test_disabled_when_blank(monkeypatch):
    monkeypatch.setenv("R2_BLOB_CACHE", "   ")
    assert not blob_cache.enabled()


def test_store_then_lookup_roundtrip(cache_root):
    data = b"payload-bytes"
    assert blob_cache.store("claude/p/s/w.jsonl.xz", "etag-a", data)
    assert blob_cache.lookup(
        "claude/p/s/w.jsonl.xz", "etag-a", len(data)) == data


def test_entry_lives_under_a_hex_fanout_dir(cache_root):
    blob_cache.store("claude/k", "e", b"x")
    files = list(cache_root.glob("*/*"))
    assert len(files) == 1
    assert re.fullmatch(r"[0-9a-f]{2}", files[0].parent.name)
    assert re.fullmatch(r"[0-9a-f]{64}", files[0].name)


def test_etag_and_key_isolate_entries(cache_root):
    blob_cache.store("claude/k", "e1", b"one")
    blob_cache.store("claude/k", "e2", b"two")
    blob_cache.store("claude/other", "e1", b"other")
    assert blob_cache.lookup("claude/k", "e1", 3) == b"one"
    assert blob_cache.lookup("claude/k", "e2", 3) == b"two"
    assert blob_cache.lookup("claude/other", "e1", 5) == b"other"


def test_lookup_misses_when_nothing_was_stored(cache_root):
    assert blob_cache.lookup("claude/k", "e", 1) is None


def test_lookup_misses_on_size_mismatch(cache_root):
    blob_cache.store("claude/k", "e", b"abc")
    assert blob_cache.lookup("claude/k", "e", 99) is None


def test_lookup_allows_size_none(cache_root):
    blob_cache.store("claude/k", "e", b"abc")
    assert blob_cache.lookup("claude/k", "e", None) == b"abc"


def test_store_replaces_an_entry(cache_root):
    blob_cache.store("claude/k", "e", b"short")
    blob_cache.store("claude/k", "e", b"a-longer-body")
    assert blob_cache.lookup("claude/k", "e", None) == b"a-longer-body"


def test_store_best_effort_when_root_is_not_creatable(
        monkeypatch, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory")
    monkeypatch.setenv("R2_BLOB_CACHE", str(blocker / "cache"))
    assert blob_cache.store("claude/k", "e", b"x") is False
    assert blob_cache.lookup("claude/k", "e", 1) is None


def test_lookup_best_effort_when_root_is_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("R2_BLOB_CACHE", str(tmp_path / "never-created"))
    assert blob_cache.lookup("claude/k", "e", 1) is None


def test_prune_respects_the_cap(cache_root):
    blob_cache.store("claude/a", "e", b"x" * 10)
    blob_cache.prune(cap_bytes=10 ** 9)
    assert blob_cache.lookup("claude/a", "e", 10) is not None
    removed = blob_cache.prune(cap_bytes=0)
    assert removed == 10
    assert blob_cache.lookup("claude/a", "e", 10) is None


def test_prune_drops_the_oldest_first(cache_root):
    blob_cache.store("claude/old", "e", b"o" * 5)
    os.utime(entry_of(cache_root, "claude/old", "e"), (1000, 1000))
    blob_cache.store("claude/new", "e", b"n" * 5)
    os.utime(entry_of(cache_root, "claude/new", "e"), (2000, 2000))
    blob_cache.prune(cap_bytes=5)
    assert blob_cache.lookup("claude/old", "e", 5) is None
    assert blob_cache.lookup("claude/new", "e", 5) is not None


def entry_of(root: Path, key: str, etag: str) -> Path:
    """The entry path the cache module derives for (key, etag)."""
    import hashlib
    digest = hashlib.sha256(f"{key}\0{etag}".encode()).hexdigest()
    return root / digest[:2] / digest


def test_prune_sweeps_abandoned_temps_too(cache_root):
    """A crashed writer's temp is old, so the mtime LRU drops it first;
    temps count toward the cap like any entry or they would leak."""
    tmp = entry_of(cache_root, "claude/k", "e").parent / ".tmp-xyz"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(b"t" * 4)
    os.utime(tmp, (1000, 1000))
    assert blob_cache.prune(cap_bytes=0) == 4
    assert not tmp.exists()


def test_prune_cap_from_env(cache_root, monkeypatch):
    blob_cache.store("claude/a", "e", b"x" * 10)
    monkeypatch.setenv("R2_BLOB_CACHE_MAX_BYTES", "5")
    blob_cache.prune()
    assert blob_cache.lookup("claude/a", "e", 10) is None


def test_prune_cap_env_garbage_falls_to_default(cache_root, monkeypatch):
    blob_cache.store("claude/a", "e", b"x" * 10)
    monkeypatch.setenv("R2_BLOB_CACHE_MAX_BYTES", "garbage")
    blob_cache.prune()
    assert blob_cache.lookup("claude/a", "e", 10) is not None


def test_prune_negative_cap_env_falls_to_default(cache_root, monkeypatch):
    """A negative R2_BLOB_CACHE_MAX_BYTES would wipe the cache every
    run; it falls back to the default instead of thrashing."""
    blob_cache.store("claude/a", "e", b"x" * 10)
    monkeypatch.setenv("R2_BLOB_CACHE_MAX_BYTES", "-1")
    blob_cache.prune()
    assert blob_cache.lookup("claude/a", "e", 10) is not None
