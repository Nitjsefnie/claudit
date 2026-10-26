"""The scan-gate's coverage claims, pinned.

Two layers:

- the synthetic matrix: every spelling a flagged shape's vocabulary can
  carry (plain, split-concat, escape, f-string chunk, folded constant
  container) is visible to ``module_vocab`` — the fail-closed claim the
  gates lean on;
- the tree property: every string constant in every module under
  tests/ is visible to the gate, except operands a fresh compile proves
  swallowed in folded chains or template-rewritten formats. This makes the
  fail-closed claim hold against the REAL tree: an interpreter change
  to constant folding fails here, loudly, instead of silently narrowing
  the gate.

This file walks the whole tree when it runs — that walk is exactly the
cost removed from the PINNED scans, so this file is
UNPINNED on purpose: never add it to suite_bench_files.txt.
"""
from __future__ import annotations

import ast
from collections.abc import Iterable
from pathlib import Path
from types import CodeType

import pytest

from tests import scan_gate
from tests.source_scan_cache import for_source, source as cached_source

TESTS_DIR = Path(__file__).resolve().parent


def _constant_strings(value: object) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, CodeType):
        return _constant_strings(value.co_consts)
    if isinstance(value, (tuple, frozenset)):
        return {string for item in value for string in _constant_strings(item)}
    return set()


def _fold_operands(nodes: Iterable[ast.AST]) -> set[ast.Constant]:
    """Exempt only operand nodes a fresh compile proves the compiler swallows.

    Folded chains and template-rewritten formats alike must lose every
    operand value from the probe's recursive co_consts. Dead-branch
    occurrences of these swallowable shapes are equally invisible and
    deliberately uncovered; interpreter changes fail the real-tree
    property loudly. Exemptions belong to nodes, never same-valued twins.
    A leading None binding reserves co_consts[0], which some interpreters
    retain even when that slot's original operand was folded away.
    """
    found: set[ast.Constant] = set()

    def string_operands(node: ast.AST) -> set[ast.Constant]:
        if isinstance(node, ast.Constant):
            return {node} if isinstance(node.value, str) else set()
        if isinstance(node, ast.BinOp):
            return string_operands(node.left) | string_operands(node.right)
        if isinstance(node, ast.Tuple):
            return {operand for elt in node.elts
                    for operand in string_operands(elt)}
        return set()

    for node in nodes:
        if not isinstance(node, ast.BinOp):
            continue
        operands = string_operands(node)
        probe = compile("_unused = None\nx = " + ast.unparse(node),
                        "<fold-probe>", "exec")
        consts = _constant_strings(probe)
        if operands and all(operand.value not in consts for operand in operands):
            found |= operands
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


@pytest.mark.parametrize(("source", "probe_source", "operands"), [
    ("x = f('never_seen_by_any_gate')", "x = f('never_seen_by_any_gate')",
     {'never_seen_by_any_gate'}),
    ("x = 'a' + 'b'", "x = 'a' + 'b'", {'a', 'b'}),
    ("x = 'cd' * 16", "x = 'cd' * 16", {'cd'}),
    ("x = 'a' + 'b' + name", "x = 'a' + 'b'", {'a', 'b'}),
    ("x = 'a' + 'b' + f('never_folded')", "x = 'a' + 'b'", {'a', 'b'}),
    ("salt_hex = '11' * 16", "salt_hex = '11' * 16", {'11'}),
])
def test_fold_operands_only_exempts_folded_chains(
        source: str, probe_source: str, operands: set[str]) -> None:
    probe = compile("_unused = None\n" + probe_source, "<expected>", "exec")
    expected = operands if operands.isdisjoint(_constant_strings(probe)) else set()
    exempted = _fold_operands(ast.walk(ast.parse(source)))
    assert {node.value for node in exempted} == expected


@pytest.mark.parametrize(("format_literal", "rhs"), [
    ('"type":"%s","input":[{"type":"text","text":"%s"}]', '(typ, text)'),
    ('%s!', 'name'),
    ('%s!', '(name,)'),
    ('%s:%s', "('a', 'b')"),
    ('%d!', '(name,)'),
    ('%10s!', '(name,)'),
    ('%s:%s', '(name,)'),
    ('%r!', '(name,)'),
    ('%a!', '(name,)'),
    ('%s:%r:%a', '(a, b, c)'),
    ('%%:%s:%%', '(name,)'),
    ('%.3s!', '(name,)'),
    ('%.3r!', '(name,)'),
    ('%.3a!', '(name,)'),
    ('%-+ #010.3s!', '(name,)'),
    ('%99s!', '(name,)'),
    ('%100s!', '(name,)'),
    ('%.99s!', '(name,)'),
    ('%.100s!', '(name,)'),
    ('%.001s!', '(name,)'),
    ('%10.s!', '(name,)'),
    ('%*s!', '(width, name)'),
    ('%.*s!', '(precision, name)'),
    ('%(key)s!', '(name,)'),
    ('%s!', '(*names,)'),
    ('%s:%s!', '(a, b, c)'),
    ('%s:%d!', '(a, b)'),
    ('%s:%', '(name,)'),
    ('%s:%q', '(a, b)'),
    ('lit', '(a, b)'),
])
def test_fold_operands_exempts_percent_format_literals(
        format_literal: str, rhs: str) -> None:
    source = f"x = {format_literal!r} % {rhs}"
    operands = {format_literal} | {
        node.value for node in ast.walk(ast.parse(rhs))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    probe = compile("_unused = None\n" + source, "<expected>", "exec")
    expected = operands if operands.isdisjoint(_constant_strings(probe)) else set()
    exempted = _fold_operands(ast.walk(ast.parse(source)))
    assert {node.value for node in exempted} == expected


@pytest.mark.parametrize("statement", [
    "x = '%s!' % name", "x = '%d!' % (name,)",
    "x = '%s:%s' % (name,)", "x = '%s:%s' % ('a', 'b')",
    "x = '%s!' % 'x'", "x = 'lit' * 10000", "x = 'lit' + 1",
])
def test_tree_property_rejects_intact_operands_in_dead_code(
        tmp_path: Path, statement: str) -> None:
    source = f"if False:\n    {statement}\n"
    assert scan_gate.module_vocab(source)[0] == {''}
    (tmp_path / "dead_format.py").write_text(source, encoding="utf-8")
    with pytest.raises(AssertionError, match="gate misses the string constant"):
        _assert_gate_sees_string_constants(tmp_path)


def test_tree_property_accepts_an_intact_format_in_live_code(
        tmp_path: Path) -> None:
    source = "x = '%d!' % (name,)\n"
    probe = compile(source, "<expected>", "exec")
    assert scan_gate.module_vocab(source)[0] == {''} | _constant_strings(probe)
    (tmp_path / "live_format.py").write_text(source, encoding="utf-8")
    _assert_gate_sees_string_constants(tmp_path)


def test_tree_property_accepts_a_rewritten_format(tmp_path: Path) -> None:
    source = "x = '%s-%s' % (a, b)\n"
    probe = compile(source, "<expected>", "exec")
    assert scan_gate.module_vocab(source)[0] == {''} | _constant_strings(probe)
    (tmp_path / "rewritten_format.py").write_text(source, encoding="utf-8")
    _assert_gate_sees_string_constants(tmp_path)


def test_fold_operands_keeps_intact_constant_mod_operands(
        tmp_path: Path) -> None:
    source = "x = '%s!' % 'x'"
    probe = compile("_unused = None\n" + source, "<expected>", "exec")
    operands = {'%s!', 'x'}
    expected = operands if operands.isdisjoint(_constant_strings(probe)) else set()
    exempted = _fold_operands(ast.walk(ast.parse(source)))
    assert {node.value for node in exempted} == expected
    assert scan_gate.module_vocab(source)[0] == {''} | _constant_strings(
        compile(source, "<expected>", "exec"))
    (tmp_path / "constant_mod.py").write_text(source, encoding="utf-8")
    _assert_gate_sees_string_constants(tmp_path)


def test_tree_property_rejects_a_missed_call_argument(tmp_path: Path) -> None:
    source = "if False:\n    x = f('never_seen_by_any_gate')\n"
    consts, _ = scan_gate.module_vocab(source)
    assert 'never_seen_by_any_gate' not in consts
    (tmp_path / "missed.py").write_text(source, encoding="utf-8")
    with pytest.raises(AssertionError, match="gate misses the string constant"):
        _assert_gate_sees_string_constants(tmp_path)


@pytest.mark.parametrize("source", [
    "x = 'a' + 'b'\n",
    "def folded():\n    'docstring'\n    salt_hex = '11' * 16\n",
])
def test_tree_property_accepts_operands_of_a_folded_chain(
        tmp_path: Path, source: str) -> None:
    consts, _ = scan_gate.module_vocab(source)
    assert consts == {''} | _constant_strings(
        compile(source, "<expected>", "exec"))
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
