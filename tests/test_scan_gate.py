"""The scan-gate's coverage claims, pinned.

Two layers:

- the synthetic matrix: every spelling a flagged shape's vocabulary can
  carry (plain, split-concat, escape, f-string chunk, folded constant
  container) is visible to ``module_vocab`` — the fail-closed claim the
  gates lean on;
- the tree property: every string constant in every module under
  tests/ is visible to the gate. This is the pin that makes the
  fail-closed claim hold against the REAL tree: an interpreter change
  to constant folding fails here, loudly, instead of silently narrowing
  the gate.

This file walks the whole tree when it runs — that walk is exactly the
cost issue #510 removed from the PINNED scans, so this file is
UNPINNED on purpose: never add it to suite_bench_files.txt.
"""
from __future__ import annotations

import ast
from pathlib import Path

from tests import scan_gate

TESTS_DIR = Path(__file__).resolve().parent


def _fold_operands(tree) -> set[str]:
    """Values the compiler swallows into a folded RESULT constant.

    ``'a' + 'b'`` and ``'cd' * 16`` fold, and the fold is CHAINED: the
    whole of ``x + y * 30 + z`` becomes one constant, swallowing every
    string operand inside the foldable chain. They are excluded from
    the property because they are invisible to the scanners too — a
    flagged shape reads a DIRECT ``ast.Constant`` argument, and a
    value inside a folded chain is not one.
    """
    found: set[str] = set()

    def strings_if_folds(node) -> set[str] | None:
        """The string operands if this subtree folds to one constant."""
        if isinstance(node, ast.Constant):
            return {node.value} if isinstance(node.value, str) else set()
        if (isinstance(node, ast.BinOp)
                and isinstance(node.op, (ast.Add, ast.Mult, ast.Mod))):
            left = strings_if_folds(node.left)
            right = strings_if_folds(node.right)
            if left is not None and right is not None:
                return left | right
        return None

    for node in ast.walk(tree):
        folded = strings_if_folds(node)
        if folded is not None:
            found |= folded
    return found


def test_the_gate_sees_every_string_constant_in_the_tree():
    for path in sorted(TESTS_DIR.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        consts, _ = scan_gate.module_vocab(source)
        tree = ast.parse(source)
        folded = _fold_operands(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant)
                    and isinstance(node.value, str)):
                continue
            value = node.value
            if "\n" in value:
                # A multiline literal's value is DEDENTED in co_consts
                # (the compiler strips continuation-line indentation),
                # so exact membership cannot hold for it. Every gate
                # clause over multiline values is substring- or
                # prefix-based, and dedent only removes whitespace
                # after newlines, so a no-newline substring or a
                # from-the-start prefix survives. Pin exactly that.
                if 'pr' 'icing.json' in value:
                    assert any('pr' 'icing.json' in c for c in consts), (
                        f"{path.name}: gate misses 'pr' 'icing.json' "
                        "inside a multiline constant")
            else:
                assert (value in consts or value in folded), (
                    f"{path.name}: gate misses the string constant "
                    f"{value[:80]!r} — the fail-closed claim is "
                    "broken; extend module_vocab before narrowing any "
                    "gate clause")


def test_constants_survive_implicit_concat():
    consts, _ = scan_gate.module_vocab(
        "X = 'pr' 'icing.example'\n")
    assert 'pr' 'icing.example' in consts


def test_constants_survive_escapes():
    consts, _ = scan_gate.module_vocab("X = 'pr\\x69cing.example'\n")
    assert 'pr' 'icing.example' in consts


def test_f_string_chunks_are_the_constants_the_ast_sees():
    consts, _ = scan_gate.module_vocab("X = f'pr{i}cing.example'\n")
    assert {'pr', 'cing.example'} <= consts


def test_constants_survive_folded_containers():
    consts, _ = scan_gate.module_vocab(
        "X = frozenset({'pr' 'icing.example', 'y'})\n")
    assert 'pr' 'icing.example' in consts


def test_identifiers_reach_names():
    _, names = scan_gate.module_vocab(
        "import pytest\n\n\n@pytest.fixture\n"
        "def one():\n    return srv_call()\n")
    assert {"pytest", "fixture", "srv_call"} <= names


def test_a_marker_comment_is_not_a_constant_but_sits_in_the_text():
    consts, names = scan_gate.module_vocab(
        "X = 1  # a marker comment\n")
    assert 'a marker comment' not in consts
    assert 'a marker comment' not in names
