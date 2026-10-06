"""Pins for the db-marker scanners' compile-level admission gate.

Split out of tests/test_db_marker.py to hold the issue-#679 gate
pins without pushing that pinned, bench-measured module over the
test-file size cap: the size ratchet moves code into a new module
rather than raising an entry. The guard surface and its older pins
stay in tests/test_db_marker.py, which the suite-cost bench pins.
"""
from __future__ import annotations

from tests.test_db_marker import (_derive_db_fixtures, _marking_offenders,
                                  _module_facts, _modules)


# ---- the compile-level fixture half of the gate (issue #679) --------

def test_a_prose_fixture_mention_is_not_scanned(tmp_path):
    # The gate's fixture half admits on compile-level vocabulary —
    # ``fixture`` as an identifier or a whole string constant — not on
    # raw text: a comment or docstring mentioning fixtures can
    # register nothing, and the marking guard's own per-module check
    # is text-server-based and already skipped it, so walking it was
    # pure cost (issue #679). The decorator spelling's admission is
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


def test_the_marking_guard_shares_the_derive_s_modules_cache_entry():
    # Keyed on the caller's argument tuple, _modules() (the derive's
    # call) and _modules(None) (the guard's) were TWO entries: the
    # guard re-parsed every admitted module and re-walked the server
    # modules' facts. The guard now reuses the derive's entry (issue
    # #679), so one derive+mark pass leaves exactly one entry.
    _modules.cache_clear()
    _module_facts.cache_clear()
    _derive_db_fixtures()
    _marking_offenders()
    assert _modules.cache_info().currsize == 1
