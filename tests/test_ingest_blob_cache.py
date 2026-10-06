"""The disk blob cache through the ingest's fetch path (issue #684).

The cache unit's own tests live in test_blob_cache.py; these pin the
wiring: the listing identity reaching the fetch callable, a hit
answering without the GET, and the run's end-of-walk prune.
"""
from __future__ import annotations

import types

# The fixtures register on import; pylint only sees names nobody calls.
from test_ingest import (  # pylint: disable=unused-import
    _fresh_db_fixture, _mini_r2_env_fixture,
)

from backend import blob_cache, ingest, ingest_fetch


# --------------------------------------- the disk blob cache (#684)

def test_fetch_and_parse_forwards_the_listing_identity_only_when_cached(
        monkeypatch, tmp_path):
    """The etag/size pair travels to the fetch callable only when the
    deploy enabled the blob cache: with it off (the suite's default), the
    original fetch(key) shape is spoken and a patched single-arg fetch
    callable keeps working."""
    calls: list[tuple] = []

    def fetch(key, *rest):
        calls.append((key, rest))
        return b"payload"

    def parse_file(key, data):
        return {"agent_type_in_band": True, "data": data}

    ingest_fetch.fetch_and_parse("k", None, fetch, parse_file,
                                 etag="e1", size=8)
    assert calls == [("k", ())]

    monkeypatch.setenv("R2_BLOB_CACHE", str(tmp_path / "cache"))
    ingest_fetch.fetch_and_parse("k", None, fetch, parse_file,
                                 etag="e1", size=8)
    assert calls[-1] == ("k", ("e1", 8))


def test_fetch_and_parse_answers_a_cache_hit_without_a_fetch(
        monkeypatch, tmp_path):
    """The money path: a cached (key, etag) parses identically with the
    object's file gone from the mirror — the GET never happened."""
    mirror = tmp_path / "claude" / "p" / "s"
    mirror.mkdir(parents=True)
    body = b"x\n"
    (mirror / "w.jsonl").write_bytes(body)
    monkeypatch.setenv("R2_ENDPOINT", f"{(tmp_path).as_uri()}/")
    monkeypatch.setenv("R2_BUCKET", "claude")
    monkeypatch.setenv("R2_BLOB_CACHE", str(tmp_path / "cache"))
    key = "claude/p/s/w.jsonl"
    # The real fetch callable, no spy: the consult sits below it, inside
    # r2.get_object, so "no second fetch" is proven by the second call
    # succeeding with the object's file GONE — only a cache hit can.

    def parse_file(key, data):
        return {"agent_type_in_band": True, "data": data}

    first = ingest_fetch.fetch_and_parse(
        key, None, ingest_fetch.fetch_with_retry, parse_file,
        etag="e1", size=len(body))
    (mirror / "w.jsonl").unlink()
    second = ingest_fetch.fetch_and_parse(
        key, None, ingest_fetch.fetch_with_retry, parse_file,
        etag="e1", size=len(body))
    assert second == first


def test_parse_wire_passes_the_listing_identity():
    """The pool child's unit carries the item's etag/size to the parse
    seam, whatever the seam does with them."""
    recorded: list[tuple] = []

    def parse_call(key, sidecar_key, etag=None, size=None):
        recorded.append((key, sidecar_key, etag, size))
        return {}

    item = (types.SimpleNamespace(key="claude/p/s/k.jsonl",
                                  sidecar_key=None, etag="e9", size=7),
            None, None)
    ingest_fetch.parse_wire(item, parse_call, False)
    assert recorded == [("claude/p/s/k.jsonl", None, "e9", 7)]


def test_ingest_run_populates_and_prunes_the_blob_cache(
        fresh_db, mini_r2_env, monkeypatch, tmp_path):
    """The run wiring: fetches store entries and the run ends with a
    prune, so the cache on a live meter stays under its cap without a
    separate sweeper."""
    cache_dir = tmp_path / "cache"
    monkeypatch.setenv("R2_BLOB_CACHE", str(cache_dir))
    pruned: list[int] = []
    monkeypatch.setattr(blob_cache, "prune",
                        lambda *a, **k: pruned.append(1) or 0)
    result = ingest.run_ingest(trigger="manual")
    assert result["failed"] == 0
    assert pruned == [1]
    assert any(cache_dir.rglob("*")), "the run's fetches stored nothing"


def test_parse_wire_passes_the_listing_identity_when_timed():
    """The timed child's unit carries the identity too: a CLAUDIT_TIMING
    run must not silently re-download the bucket the cache exists to
    answer (the uncontrolled limb the #684 review caught)."""
    recorded: list[tuple] = []

    def parse_call(key, sidecar_key, etag=None, size=None):
        recorded.append((key, sidecar_key, etag, size))
        return {}

    item = (types.SimpleNamespace(key="claude/p/s/k.jsonl",
                                  sidecar_key=None, etag="e9", size=7),
            None, None)
    parsed, stages = ingest_fetch.parse_wire(item, parse_call, True)
    assert recorded == [("claude/p/s/k.jsonl", None, "e9", 7)]
    assert parsed == {}
    assert stages == {}
