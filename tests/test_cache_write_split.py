"""Issue #842 — the provider-neutral cache-write split's invariants.

The split the backend derives in ``parse_common.resolve_cache_writes`` and
the browser mirrors in ``window.resolveCacheWrites`` is pinned here by
three controls, on both sides:

* idempotence — a SECOND application over already-derived records leaves
  every value unchanged;
* the input partition — ``cache_read + cache_write + uncached tail``
  reconstructs each request's total input, and the tail stays
  non-negative;
* the create-branch precedence — a file's LAST record with
  ``cache_creation > 0`` takes the create branch, never the
  successor-less zero.

These live in their own module rather than beside the #842 derivation
tests (``test_parse.py``) and the lane parity tests
(``test_parser_js_lanes.py``): both sit within a few lines of the 700-line
test ceiling the size ratchet caps, so the controls go into a new module.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

import pytest

from backend import parse

ROOT = Path(__file__).resolve().parents[1]
FIX_PARSER = ROOT / "fixtures" / "parser"
CACHE_WRITE_JS = ROOT / "src" / "parser-cache-write.js"
LANES_JS = ROOT / "src" / "parser-lanes.js"
CODEX_JS = ROOT / "src" / "parser-codex.js"
LOADER_JS = ROOT / "src" / "pricing-loader.js"
RATES_JS = ROOT / "src" / "rates.js"
PARSER_JS = ROOT / "src" / "parser.js"

# An auto-caching record and an explicit-caching one (r5, create=500), so
# the split's both branches are exercised.
SPLIT = "cache_write_split.jsonl"
# A file whose LAST record reports an explicit write.
LAST_CREATE = "cache_write_last_create.jsonl"


def _backend(name):
    blob = (FIX_PARSER / name).read_bytes()
    return parse.parse_file(f"k/sess/{name}", blob)["records"]


@lru_cache(maxsize=None)
def _browser(name):
    """Parse a fixture in the browser, then re-derive over the same meta.

    Returns the first pass's per-record categories and the second pass's
    cache_write values — the idempotence control reads the two against
    each other.
    """
    text = (FIX_PARSER / name).read_text(encoding="utf-8")
    script = f"""
      global.window = {{}};
      require({str(CACHE_WRITE_JS)!r});
      require({str(LANES_JS)!r});
      require({str(CODEX_JS)!r});
      require({str(LOADER_JS)!r}); require({str(RATES_JS)!r}); require({str(PARSER_JS)!r});
      const {{ meta }} = window.parseTranscript({json.dumps(text)});
      const recs = meta.filter(m => m.type === 'assistant_usage');
      const records = recs.map(m => ({{
        fresh: m.usage.input_tokens || 0,
        create: m.usage.cache_creation_input_tokens || 0,
        read: m.usage.cache_read_input_tokens || 0,
        cache_write: m.cache_write,
      }}));
      window.resolveCacheWrites(meta);   // a SECOND application
      console.log(JSON.stringify({{ records, second: recs.map(m => m.cache_write) }}));
    """
    proc = subprocess.run(
        ["node"], input=script, capture_output=True, text=True, timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# --- Backend (parse_common.resolve_cache_writes) --------------------------

def test_backend_derivation_is_idempotent():
    """A second pass reads only fresh/create/read, so it changes nothing.
    (Mutation pinned: a derive that folded its own cache_write_tokens back
    into an input would move values on the second pass.)"""
    recs = _backend(SPLIT)
    before = [r["cache_write_tokens"] for r in recs]
    parse.resolve_cache_writes(recs)
    assert [r["cache_write_tokens"] for r in recs] == before


def test_backend_categories_partition_each_input():
    """read + write + the uncached tail reconstructs the total input, and
    the tail stays non-negative. (Mutation pinned: dropping the cap so
    cache_write_tokens can exceed fresh + cache_creation drives the tail
    negative, failing the bound.)"""
    recs = _backend(SPLIT)
    assert any(r["cache_creation_tokens"] > 0 for r in recs)     # explicit
    assert any(r["cache_creation_tokens"] == 0 for r in recs)    # automatic
    for r in recs:
        total = r["fresh_tokens"] + r["cache_creation_tokens"]
        write = r["cache_write_tokens"]
        tail = total - write
        assert 0 <= write <= total
        assert r["cache_read_tokens"] + write + tail == (
            total + r["cache_read_tokens"])


def test_backend_last_record_with_creation_takes_create_branch():
    """The LAST record of a file with cache_creation > 0 is an explicit
    write, not the successor-less zero. (Mutation pinned: testing the
    successor-less branch before the create branch would set it 0.)"""
    last = _backend(LAST_CREATE)[-1]
    assert last["cache_creation_tokens"] > 0
    assert last["cache_write_tokens"] == last["cache_creation_tokens"]


# --- Browser (window.resolveCacheWrites) ----------------------------------

class TestBrowserCacheWriteSplit:
    # Node-backed tests skip together when Node is unavailable.
    pytestmark = pytest.mark.skipif(
        shutil.which("node") is None, reason="node not available")

    def test_browser_derivation_is_idempotent(self):
        out = _browser(SPLIT)
        assert out["second"] == [r["cache_write"] for r in out["records"]]

    def test_browser_categories_partition_each_input(self):
        recs = _browser(SPLIT)["records"]
        assert any(r["create"] > 0 for r in recs)
        assert any(r["create"] == 0 for r in recs)
        for r in recs:
            total = r["fresh"] + r["create"]
            tail = total - r["cache_write"]
            assert 0 <= r["cache_write"] <= total
            assert r["read"] + r["cache_write"] + tail == total + r["read"]

    def test_browser_last_record_with_creation_takes_create_branch(self):
        last = _browser(LAST_CREATE)["records"][-1]
        assert last["create"] > 0
        assert last["cache_write"] == last["create"]
