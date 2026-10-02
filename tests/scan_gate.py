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
