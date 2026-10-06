"""Fail-closed vocabulary gate for the tests/-tree scans (issue #510).

Three files the suite-cost fixture pins (tests/test_db_marker.py,
tests/test_no_pinned_version_literals.py, tests/test_scratch_db.py)
scan every module under tests/, so the suite_cost ``run`` phase
re-prices whenever any test file is added — the walk and tokenize
loops are the cost, not ``ast.parse``/``compile``, which retire no
Python bytecode. Each scanner therefore asks this module, first,
whether a source can hold anything its scan could flag. A file that
cannot skips the walk and the tokenizer entirely: adding an ordinary
test file costs one vocabulary check instead of a full walk.

The gate is FAIL-CLOSED BY CONSTRUCTION: every clause admits a strict
superset of what the scanner can flag, so a skip can only save work,
never hide a violation. The load-bearing claims, each pinned by a test:

- every ``ast.Constant`` string value in a module's AST is visible in
  ``module_vocab``'s consts — the compiler folds implicit concats and
  resolves escapes (and folds constant containers), so a value no
  plain-text grep can see is still there. Pinned over the real tree by
  test_scan_gate's property test, so an interpreter change that
  alters folding fails loudly instead of silently narrowing the gate;
- every identifier a scanner matches (fixture decorators, server-call
  names, rate-call names, attribute targets) appears verbatim in
  ``co_names`` or the source text — identifiers cannot be escaped or
  split.

KNOWN RESIDUAL, deliberately not covered: a violation spelled only
inside a dead branch (``if 0:``) whose literals are also split or
escaped — the compiler dead-code-eliminates the branch, so the value
is in neither ``co_consts`` nor plain text. No real mistake spells a
violation that way; the scanners guard real mistakes, not steganog-
raphy, and covering it would put a walk back on every file.
"""
from __future__ import annotations

import types

__all__ = ["module_vocab"]


def module_vocab(source: str) -> tuple[frozenset[str], frozenset[str]]:
    """(string-constant values, used identifiers) for one module's source.

    ``compile()`` retires no Python bytecode, and the const walk visits
    code objects, not AST nodes, so this costs a small constant per
    module regardless of the module's size in statements. Containers
    fold: ``frozenset({...})`` of constants becomes ONE frozenset
    constant, so the walk descends into string-bearing containers —
    that descent is load-bearing (db_marker's DB_FIXTURES lives in
    exactly such a fold).
    """
    consts: set[str] = set()
    names: set[str] = set()
    stack = [compile(source, "<scan_gate>", "exec")]
    while stack:
        code = stack.pop()
        for value in code.co_consts:
            if isinstance(value, str):
                consts.add(value)
            elif isinstance(value, (tuple, frozenset, set, list)):
                consts.update(_container_strings(value))
            elif isinstance(value, types.CodeType):
                stack.append(value)
        names.update(code.co_names)
    return frozenset(consts), frozenset(names)


def _container_strings(value) -> set[str]:
    """Every string inside a folded constant container, recursively."""
    found: set[str] = set()
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            found.add(item)
        elif isinstance(item, (tuple, frozenset, set, list)):
            stack.extend(item)
    return found


def module_def_vocab(source: str) -> tuple[frozenset[str], bool]:
    """(every name the source may spell, whether the source may carry a
    ``pytest.fixture`` decorator application) for one module's source.

    The def-site index for the db-marker derivation (issue #715): the
    first set unions, over every code object, ``co_names``,
    ``co_varnames`` (a fixture REQUEST binds its name only as a
    parameter — invisible to ``co_names`` and ``co_consts``) and each
    code object's own ``co_name``, plus every string constant. The
    second set answers over the whole module: the derivation recognises
    only the dotted ``pytest.fixture`` spelling, whose evidence is
    ``pytest`` among identifiers and ``fixture`` among identifiers or
    constants at any nesting level (a nested fixture def binds its name
    in the parent's co_varnames, which neither co_names nor consts
    carry — hence the union, not the module level).

    Both answers are over-inclusive by construction — an import, a
    local, or a prose string also admits — so a skip can only save a
    parse, never hide a fixture def or a chain member OF LIVE CODE:
    a def spelled only inside a dead branch (``if 0:``) carries its
    parameter names in no surviving code object, so the index cannot
    see it. Such a def never executes and never registers at
    runtime, which is the precondition the derivation's parity claim
    rests on (issue #715 review, PR #730).
    """
    consts: set[str] = set()
    names: set[str] = set()
    stack = [compile(source, "<scan_gate>", "exec")]
    while stack:
        code = stack.pop()
        names.add(code.co_name)
        names.update(code.co_varnames)
        names.update(code.co_names)
        for value in code.co_consts:
            if isinstance(value, str):
                consts.add(value)
            elif isinstance(value, (tuple, frozenset, set, list)):
                consts.update(_container_strings(value))
            elif isinstance(value, types.CodeType):
                stack.append(value)
    names.update(consts)
    may_define_fixture = "pytest" in names and "fixture" in names
    return frozenset(names), may_define_fixture
