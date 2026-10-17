"""Pins for the db-marker scanners' compile-level admission gate.

Split out of tests/test_db_marker.py to hold the gate
pins without pushing that pinned, bench-measured module over the
test-file size cap: the size ratchet moves code into a new module
rather than raising an entry. The guard surface and its older pins
stay in tests/test_db_marker.py, which the suite-cost bench pins.
"""
from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path
import time
from typing import Any

import pytest

from tests import source_scan_cache, source_scan_index
from tests.test_db_marker import (_derive_narrow, _marking_offenders,
                                  _module_facts, _modules)


def test_a_prose_fixture_mention_is_not_scanned(tmp_path):
    # The gate's fixture half admits on compile-level vocabulary —
    # ``fixture`` as an identifier or a whole string constant — not on
    # raw text: a comment or docstring mentioning fixtures can
    # register nothing, and the marking guard's own per-module check
    # is text-server-based and already skipped it, so walking it was
    # pure cost. The decorator spelling's admission is
    # pinned by test_a_seeded_rooted_fixture_arrives_in_the_derived_
    # registry; the getattr spelling's by the test after this one.
    (tmp_path / "test_prose.py").write_text(
        "def test_ok():\n"
        "    assert True  # the fixture list lives elsewhere\n",
        encoding="utf-8")
    assert not _modules(tmp_path)


def test_a_getattr_fixture_decorator_is_scanned(tmp_path):
    # The consts half of the fixture half: getattr(pytest, "fixture")
    # registers a fixture at def time without the plain decorator
    # spelling, and the decorator's argument is a string constant, so
    # the gate must admit it.
    (tmp_path / "test_indirect.py").write_text(
        "import pytest\n\n\n"
        "@getattr(pytest, 'fixture')\n"
        "def indirect():\n    return None\n", encoding="utf-8")
    assert _modules(tmp_path)


def test_the_marking_guard_shares_the_derive_s_source_and_facts(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(source_scan_cache, "_current", {})
    monkeypatch.setattr(source_scan_cache, "_history", {})
    monkeypatch.setattr(source_scan_cache, "_artifacts", {})
    monkeypatch.setattr(source_scan_index, "_indexes", {})
    now = time.time_ns() + 10_000_000_000
    monkeypatch.setattr(source_scan_cache, "_now_ns", lambda: now)
    reads: Counter[Path] = Counter()
    parses: Counter[str] = Counter()
    original_open, original_parse = Path.open, ast.parse

    def opened(path: Path, *args: Any, **kwargs: Any) -> Any:
        reads[path.resolve()] += 1
        return original_open(path, *args, **kwargs)

    def parsed(source: str, *args: Any, **kwargs: Any) -> ast.AST:
        parses[source] += 1
        return original_parse(source, *args, **kwargs)

    monkeypatch.setattr(Path, "open", opened)
    monkeypatch.setattr(ast, "parse", parsed)
    _module_facts.cache_clear()
    _derive_narrow()
    first_reads, first_parses = reads.copy(), parses.copy()
    misses = _module_facts.cache_info().misses
    assert misses > 0
    _marking_offenders()
    assert reads == first_reads and parses == first_parses
    assert reads and all(count == 1 for count in reads.values())
    assert parses and all(count == 1 for count in parses.values())
    assert _module_facts.cache_info().misses == misses
    assert _module_facts.cache_info().hits > 0
    for _, tree, source in _modules():
        assert tree is source_scan_cache.for_source(source).tree()
