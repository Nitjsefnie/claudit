"""The disk blob cache: (key, etag)-keyed, best-effort on
every failure, size-capped LRU prune.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from pathlib import Path

import pytest

from backend import blob_cache


@pytest.fixture(name="cache_root")
def _cache_root(monkeypatch, tmp_path):
    root = tmp_path / "blob-cache"
    monkeypatch.setenv("CLAUDIT_R2_BLOB_CACHE", str(root))
    return root


@pytest.mark.parametrize("root", [None, "   "], ids=["unset", "blank"])
def test_disabled_without_a_root(monkeypatch, root):
    if root is None:
        monkeypatch.delenv("CLAUDIT_R2_BLOB_CACHE", raising=False)
    else:
        monkeypatch.setenv("CLAUDIT_R2_BLOB_CACHE", root)
    assert not blob_cache.enabled()
    assert blob_cache.lookup("claude/k", "e", 1) is None
    assert blob_cache.store("claude/k", "e", b"x") is False
    assert blob_cache.prune() == 0


@pytest.mark.parametrize("key,etag,data,size", [
    ("claude/p/s/w.jsonl.xz", "etag-a", b"payload-bytes", 13),
    ("claude/k", "e", b"abc", None),
], ids=["matching-size", "unspecified-size"])
def test_store_then_lookup_roundtrip(cache_root, key, etag, data, size):
    assert blob_cache.store(key, etag, data)
    assert blob_cache.lookup(key, etag, size) == data


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


def test_store_replaces_an_entry(cache_root):
    blob_cache.store("claude/k", "e", b"short")
    blob_cache.store("claude/k", "e", b"a-longer-body")
    assert blob_cache.lookup("claude/k", "e", None) == b"a-longer-body"


def test_store_best_effort_when_root_is_not_creatable(
        monkeypatch, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory")
    monkeypatch.setenv("CLAUDIT_R2_BLOB_CACHE", str(blocker / "cache"))
    assert blob_cache.store("claude/k", "e", b"x") is False
    assert blob_cache.lookup("claude/k", "e", 1) is None


def test_prune_drops_the_oldest_first(cache_root):
    blob_cache.store("claude/old", "e", b"o" * 5)
    os.utime(entry_of(cache_root, "claude/old", "e"), (1000, 1000))
    blob_cache.store("claude/new", "e", b"n" * 5)
    os.utime(entry_of(cache_root, "claude/new", "e"), (2000, 2000))
    blob_cache.prune(cap_bytes=5)
    assert blob_cache.lookup("claude/old", "e", 5) is None
    assert blob_cache.lookup("claude/new", "e", 5) is not None


def entry_of(root: Path, key: str, etag: str) -> Path:
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


@pytest.mark.parametrize("cap,evicted", [
    ("5", True), ("garbage", False), ("-1", False),
], ids=["valid-cap", "garbage-default", "negative-default"])
def test_prune_cap_from_env(cache_root, monkeypatch, cap, evicted):
    blob_cache.store("claude/a", "e", b"x" * 10)
    monkeypatch.setenv("CLAUDIT_R2_BLOB_CACHE_MAX_BYTES", cap)
    blob_cache.prune()
    result = blob_cache.lookup("claude/a", "e", 10)
    assert (result is None) is evicted


def _is_control(ch: str) -> bool:
    """A character _log_safe must drop: C0, DEL, C1."""
    code = ord(ch)
    return code < 0x20 or 0x7F <= code <= 0x9F


def _store_failure_record(caplog, key):
    with caplog.at_level(logging.DEBUG, logger="claudit.blob_cache"):
        assert blob_cache.store(key, "e", b"x") is False
    records = [
        r for r in caplog.records if r.name == "claudit.blob_cache"]
    assert len(records) == 1, f"expected one record, got {len(records)}"
    return records[0]


def test_store_failure_log_carries_no_control_characters(
        monkeypatch, tmp_path, caplog):
    """A store() key can come from a user query param, so
    CR/LF and other control characters must not reach the log record:
    one record, no newline, nothing that could forge a second line."""
    blocker = tmp_path / "blocker"
    blocker.write_text("a file, not a directory")
    monkeypatch.setenv("CLAUDIT_R2_BLOB_CACHE", str(blocker / "cache"))
    key = ("claude/p/s/w.jsonl.xz\r\ninjected\x07\x1bline\x00end"
           "\x7f\x85nel")
    record = _store_failure_record(caplog, key)
    message = record.getMessage()
    assert "\n" not in message and "\r" not in message, message
    assert not [ch for ch in message if _is_control(ch)], message


def test_store_failure_log_scrubs_the_exception_text(
        cache_root, monkeypatch, caplog):
    """The exception text store() logs is tainted by the same route as
    the key. OSError escapes unprintables in a filename, so
    a real ENOTDIR can never seed it — force a failure whose message
    carries the tainted text raw instead, with a key that carries none,
    so only the exc limb can put a control char in the record."""
    raised = []

    def boom(*_args, **_kwargs):
        exc = ValueError("write failed: path\r\ninjected\x07\x7f\x85end")
        raised.append(exc)
        raise exc

    monkeypatch.setattr(blob_cache.os, "replace", boom)
    record = _store_failure_record(caplog, "claude/k")
    # Seed proof: the exception text really carries control characters
    # before _log_safe ever sees it, so this is no vacuous assertion.
    assert any(_is_control(ch) for ch in str(raised[0])), str(raised[0])
    message = record.getMessage()
    assert "\n" not in message and "\r" not in message, message
    assert not [ch for ch in message if _is_control(ch)], message
