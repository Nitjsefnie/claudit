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
from collections.abc import Iterable
from pathlib import Path
import re

import pytest

from tests import scan_gate
from tests.source_scan_cache import for_source, source as cached_source

TESTS_DIR = Path(__file__).resolve().parent


def _rewrites_percent_format(node: ast.BinOp) -> bool:
    if not (isinstance(node.op, ast.Mod)
            and isinstance(node.left, ast.Constant)
            and isinstance(node.left.value, str)
            and isinstance(node.right, ast.Tuple)):
        return False
    if any(isinstance(elt, ast.Starred) for elt in node.right.elts):
        return False
    field = re.compile(r"%%|%[-+ #0]*[0-9]{0,2}(?:\.[0-9]{0,2})?[sra]")
    fmt = node.left.value
    if ("%" in field.sub("", fmt)
            or sum(token != "%%" for token in field.findall(fmt))
            != len(node.right.elts)):
        return False
    # Optimize a fresh RHS copy only: constant tuples bypass format rewriting.
    rhs = ast.parse(ast.unparse(node.right), mode="eval", optimize=1).body
    return not isinstance(rhs, ast.Constant)


def _fold_operands(nodes: Iterable[ast.AST]) -> set[ast.Constant]:
    """String operand nodes the compiler swallows or rewrites, hidden in co_consts.

    Exempt constant Add/Mult/Mod chains by node identity. An interpreter
    that desugars template-convertible %-formats shatters the fmt literal
    into per-slot fragments. A split/escaped intact value reaches neither
    co_consts nor plain text: deliberately uncovered, same residual class
    as the gate's dead-branch clause. The exemption encodes the probed
    rewrite eligibility; a future interpreter change fails the real-tree
    property loudly.
    """
    found: set[ast.Constant] = set()

    def strings_if_folds(node: ast.AST) -> set[ast.Constant] | None:
        """String operands if this subtree contains only constants and allowed ops."""
        if isinstance(node, ast.Constant):
            return {node} if isinstance(node.value, str) else set()
        if (isinstance(node, ast.BinOp)
                and isinstance(node.op, (ast.Add, ast.Mult, ast.Mod))):
            left = strings_if_folds(node.left)
            right = strings_if_folds(node.right)
            if left is not None and right is not None:
                return left | right
        return None

    for node in nodes:
        if not isinstance(node, ast.BinOp):
            continue
        if isinstance(node.left, ast.Constant) and _rewrites_percent_format(node):
            found.add(node.left)
        folded = strings_if_folds(node)
        if folded is not None:
            found |= folded
    return found


def _assert_gate_sees_string_constants(directory: Path) -> None:
    for path in sorted(directory.glob("*.py")):
        source = cached_source(path)
        consts, _ = scan_gate.module_vocab(source)
        artifacts = for_source(source)
        exempted = _fold_operands(artifacts.nodes())
        for node in artifacts.nodes():
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
                assert (value in consts or node in exempted), (
                    f"{path.name}: gate misses the string constant "
                    f"{value[:80]!r} — the fail-closed claim is "
                    "broken; extend module_vocab before narrowing any "
                    "gate clause")


def test_the_gate_sees_every_string_constant_in_the_tree() -> None:
    _assert_gate_sees_string_constants(TESTS_DIR)


@pytest.mark.parametrize(("source", "expected"), [
    ("x = f('never_seen_by_any_gate')", set()),
    ("x = 'a' + 'b'", {'a', 'b'}),
    ("x = 'cd' * 16", {'cd'}),
    ("x = 'a' + 'b' + name", {'a', 'b'}),
    ("x = 'a' + 'b' + f('never_folded')", {'a', 'b'}),
])
def test_fold_operands_only_exempts_folded_chains(
        source: str, expected: set[str]) -> None:
    exempted = _fold_operands(ast.walk(ast.parse(source)))
    assert {node.value for node in exempted} == expected


@pytest.mark.parametrize(("format_literal", "rhs", "rewritten"), [
    ('"type":"%s","input":[{"type":"text","text":"%s"}]', '(typ, text)', True),
    ('%s!', 'name', False),
    ('%s!', '(name,)', True),
    ('%s:%s', "('a', 'b')", False),
    ('%d!', '(name,)', False),
    ('%10s!', '(name,)', True),
    ('%s:%s', '(name,)', False),
    ('%r!', '(name,)', True),
    ('%a!', '(name,)', True),
    ('%s:%r:%a', '(a, b, c)', True),
    ('%%:%s:%%', '(name,)', True),
    ('%.3s!', '(name,)', True),
    ('%.3r!', '(name,)', True),
    ('%.3a!', '(name,)', True),
    ('%-+ #010.3s!', '(name,)', True),
    ('%99s!', '(name,)', True),
    ('%100s!', '(name,)', False),
    ('%.99s!', '(name,)', True),
    ('%.100s!', '(name,)', False),
    ('%.001s!', '(name,)', False),
    ('%10.s!', '(name,)', True),
    ('%*s!', '(width, name)', False),
    ('%.*s!', '(precision, name)', False),
    ('%(key)s!', '(name,)', False),
    ('%s!', '(*names,)', False),
    ('%s:%s!', '(a, b, c)', False),
    ('%s:%d!', '(a, b)', False),
    ('%s:%', '(name,)', False),
    ('%s:%q', '(a, b)', False),
    ('lit', '(a, b)', False),
])
def test_fold_operands_exempts_percent_format_literals(
        format_literal: str, rhs: str, rewritten: bool) -> None:
    source = f"x = {format_literal!r} % {rhs}"
    exempted = _fold_operands(ast.walk(ast.parse(source)))
    assert {node.value for node in exempted} == (
        {format_literal} if rewritten else set())


@pytest.mark.parametrize("statement", [
    "x = '%s!' % name", "x = '%d!' % (name,)",
    "x = '%s:%s' % (name,)", "x = '%s:%s' % ('a', 'b')",
])
def test_tree_property_rejects_an_intact_format_in_dead_code(
        tmp_path: Path, statement: str) -> None:
    source = f"if False:\n    {statement}\n"
    assert not scan_gate.module_vocab(source)[0]
    (tmp_path / "dead_format.py").write_text(source, encoding="utf-8")
    with pytest.raises(AssertionError, match="gate misses the string constant"):
        _assert_gate_sees_string_constants(tmp_path)


def test_tree_property_accepts_an_intact_format_in_live_code(
        tmp_path: Path) -> None:
    source = "x = '%d!' % (name,)\n"
    assert '%d!' in scan_gate.module_vocab(source)[0]
    (tmp_path / "live_format.py").write_text(source, encoding="utf-8")
    _assert_gate_sees_string_constants(tmp_path)


def test_tree_property_accepts_a_rewritten_format(tmp_path: Path) -> None:
    source = "x = '%s-%s' % (a, b)\n"
    assert '%s-%s' not in scan_gate.module_vocab(source)[0]
    (tmp_path / "rewritten_format.py").write_text(source, encoding="utf-8")
    _assert_gate_sees_string_constants(tmp_path)


def test_fold_operands_exempts_constant_mod_operands() -> None:
    source = "x = '%s!' % 'x'"
    exempted = _fold_operands(ast.walk(ast.parse(source)))
    assert {node.value for node in exempted} == {'%s!', 'x'}


def test_tree_property_rejects_a_missed_call_argument(tmp_path: Path) -> None:
    source = "if False:\n    x = f('never_seen_by_any_gate')\n"
    consts, _ = scan_gate.module_vocab(source)
    assert 'never_seen_by_any_gate' not in consts
    (tmp_path / "missed.py").write_text(source, encoding="utf-8")
    with pytest.raises(AssertionError, match="gate misses the string constant"):
        _assert_gate_sees_string_constants(tmp_path)


def test_tree_property_accepts_operands_of_a_folded_chain(tmp_path: Path) -> None:
    source = "x = 'a' + 'b'\n"
    consts, _ = scan_gate.module_vocab(source)
    assert {'a', 'b'}.isdisjoint(consts)
    assert 'ab' in consts
    (tmp_path / "folded.py").write_text(source, encoding="utf-8")
    _assert_gate_sees_string_constants(tmp_path)


@pytest.mark.parametrize("exempting_statement", [
    "x = 'lit%s' % (name,)", "x = 'lit%s' + 'suffix'",
])
def test_tree_property_rejects_a_shared_value_in_an_unexempted_call(
        tmp_path: Path, exempting_statement: str) -> None:
    # Catches a same-value exemption hiding an unexempted call.
    source = f"if False:\n    {exempting_statement}\n    y = f('lit%s')\n"
    assert 'lit%s' not in scan_gate.module_vocab(source)[0]
    (tmp_path / "shared.py").write_text(source, encoding="utf-8")
    with pytest.raises(AssertionError, match="string constant 'lit%s'"):
        _assert_gate_sees_string_constants(tmp_path)


def test_tree_property_rejects_the_call_without_an_exempted_twin(
        tmp_path: Path) -> None:
    # Catches a missed call literal even without any exemption.
    source = "if False:\n    y = f('lit')\n"
    assert 'lit' not in scan_gate.module_vocab(source)[0]
    (tmp_path / "plain.py").write_text(source, encoding="utf-8")
    with pytest.raises(AssertionError, match="string constant 'lit'"):
        _assert_gate_sees_string_constants(tmp_path)


@pytest.mark.parametrize("exempting_statement", [
    "x = 'lit%s' % (name,)", "x = 'lit%s' + 'suffix'",
])
def test_tree_property_accepts_a_shared_value_in_the_vocabulary(
        tmp_path: Path, exempting_statement: str) -> None:
    # Catches rejecting a call literal whose value is visible to the gate.
    source = ("visible = 'lit%s'\nif False:\n"
              f"    {exempting_statement}\n    y = f('lit%s')\n")
    assert 'lit%s' in scan_gate.module_vocab(source)[0]
    (tmp_path / "visible.py").write_text(source, encoding="utf-8")
    _assert_gate_sees_string_constants(tmp_path)


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
