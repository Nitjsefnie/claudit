"""Fail-closed vocabulary gate for the tests/-tree scans.

Three files the suite-cost fixture pins scan every module under
tests/, so the suite_cost ``run`` phase re-prices whenever any test
file is added — the walk and tokenize loops are the cost, not
``ast.parse``/``compile``. Each scanner asks this module first
whether a source can hold anything its scan could flag; a file that
cannot skips the walk and the tokenizer entirely: an ordinary test
file costs one vocabulary check.

The gate is FAIL-CLOSED BY CONSTRUCTION: every clause admits a strict
superset of what the scanner can flag, so a skip can only save work,
never hide a violation. The load-bearing claims, each pinned by a test:

- every ``ast.Constant`` string value in a module's AST is visible in
  ``module_vocab``'s consts — the compiler folds implicit concats and
  resolves escapes (and folds constant containers), so a value no
  plain-text grep can see is still there; 3.15 loads ``''``/``None``
  via LOAD_COMMON_CONSTANT (built-in table, not ``co_consts``), so
  ``''`` can sit out ``co_consts`` — the vocab admits it
  unconditionally. Pinned over the real tree by test_scan_gate's
  property test, so an interpreter folding change fails loudly;
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

from tests.source_scan_index import def_vocab, vocab

__all__ = ["module_vocab"]


def module_vocab(source: str) -> tuple[frozenset[str], frozenset[str]]:
    """(string-constant values, used identifiers) for one module's source.

    ``compile()`` retires no bytecode and the const walk visits code
    objects, not AST nodes, so this costs a small constant per module
    regardless of module size. Containers fold: ``frozenset({...})`` of
    constants becomes ONE frozenset constant, so the walk descends into
    string-bearing containers — load-bearing: db_marker's DB_FIXTURES
    lives in exactly such a fold.
    """
    return vocab(source)


def module_def_vocab(source: str) -> tuple[frozenset[str], bool]:
    """(every name the source may spell, whether the source may carry a
    ``pytest.fixture`` decorator application) for one module's source.

    The db-marker derivation's def-site index: the first set unions,
    over every code object, ``co_names``, ``co_varnames`` (a fixture
    REQUEST binds its name only as a parameter — invisible to
    ``co_names``/``co_consts``) and each ``co_name``, plus every string
    constant. The second set answers over the whole module: the
    derivation recognises only the dotted ``pytest.fixture`` spelling,
    whose evidence is ``pytest`` among identifiers and ``fixture``
    among identifiers or constants at any nesting level (a nested
    fixture def binds its name in the parent's co_varnames — hence the
    union, not the module level).

    Both answers are over-inclusive by construction — an import, local
    or prose string also admits — so a skip can only save the
    downstream DB-facts traversal, never hide a fixture def or chain
    member OF LIVE CODE: a def spelled only inside a dead branch
    (``if 0:``) carries its parameter names in no surviving code
    object. Such a def never executes or registers at runtime, the
    precondition the derivation's parity claim rests on ( review).
    """
    return def_vocab(source)
