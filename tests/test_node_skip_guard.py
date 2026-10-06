"""No node-listed test file carries a module-level node skip (#743, #746).

A module-wide ``pytestmark`` node skip silently skips the file's
Python-only tests on a runner without node: #743 found it in one file,
#746 in seven more, and the module mark is exactly the shape that comes
back when nobody is looking. The guard reads
scripts/ci/node_test_files.txt and fails naming file and line when any
listed file skips node at module level: a ``pytestmark`` assignment
mentioning node (bare, annotated, list-wrapped, or nested in a
module-level ``if``/``try``), or a module-level importorskip of node.
The skip must otherwise ride only the tests that run node.

A class-level ``pytestmark`` is accepted as is: the guard does not read
into class bodies, so it cannot tell whether every test in such a class
really drives node - a Python-only test added to a node-marked class
would skip node-less unseen by this guard. That residual is disclosed
here, not blessed; its enforcement is tracked as a follow-up issue.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LIST_FILE = REPO_ROOT / "scripts" / "ci" / "node_test_files.txt"


def test_no_node_listed_file_sets_a_module_level_node_skip() -> None:
    """The node skip rides only the tests that run node - never the module."""
    offenders = []
    for rel in _listed_files():
        tree = ast.parse(
            (REPO_ROOT / rel).read_text(encoding="utf-8"))
        for stmt, line in _module_level_statements(tree):
            value = _pytestmark_value(stmt)
            if value is not None and _mentions_node(value):
                offenders.append(f"{rel}:{line}")
            elif _is_module_node_importorskip(stmt):
                offenders.append(f"{rel}:{line}")
    assert not offenders, (
        "module-level node skip(s) found; the skip must ride only the "
        "tests that run node (issues #743, #746): " + ", ".join(offenders))


def test_the_guard_detects_a_module_level_node_mark() -> None:
    """A module-wide node skipif is the shape the guard exists for."""
    mod = ast.parse(
        "import shutil\nimport pytest\n"
        "pytestmark = pytest.mark.skipif(\n"
        '    shutil.which("node") is None, reason="node not available")\n')
    values = [v for v in
              (_pytestmark_value(s) for s in mod.body) if v is not None]
    assert len(values) == 1
    assert _mentions_node(values[0])


def test_the_guard_detects_an_annotated_module_level_node_mark() -> None:
    """An annotated binding (pytestmark: object = ...) is the same shape."""
    mod = ast.parse(
        "import shutil\nimport pytest\n"
        "pytestmark: object = pytest.mark.skipif(\n"
        '    shutil.which("node") is None, reason="node not available")\n')
    values = [v for v in
              (_pytestmark_value(s) for s in mod.body) if v is not None]
    assert len(values) == 1
    assert _mentions_node(values[0])


def test_the_guard_leaves_a_non_node_module_mark_alone() -> None:
    """A module mark that does not mention node is out of scope here."""
    mod = ast.parse("import pytest\npytestmark = pytest.mark.db\n")
    values = [v for v in
              (_pytestmark_value(s) for s in mod.body) if v is not None]
    assert len(values) == 1
    assert not _mentions_node(values[0])


def test_the_guard_detects_marks_nested_in_module_level_conditionals() -> None:
    """An if/try at module level does not hide a node skip from the scan."""
    wrapped = ast.parse(
        "import shutil\nimport pytest\n"
        "if True:\n"
        "    pytestmark = pytest.mark.skipif(\n"
        '        shutil.which("node") is None, reason="node not available")\n'
        "try:\n"
        "    pytestmark = pytest.mark.skipif(\n"
        '        shutil.which("node") is None, reason="node not available")\n'
        "except ImportError:\n"
        "    pytestmark = None\n")
    assert len(_node_mark_lines(wrapped)) == 2
    skipped = ast.parse(
        "import pytest\n"
        "if True:\n"
        "    pytest.importorskip('node')\n")
    assert len(_node_mark_lines(skipped)) == 1


def test_the_guard_detects_a_module_level_node_importorskip() -> None:
    """importorskip skips the whole module for node - the same shape."""
    mod = ast.parse("import pytest\npytest.importorskip('node')\n")
    assert len(_node_mark_lines(mod)) == 1


def test_the_guard_leaves_a_non_pytestmark_module_assign_alone() -> None:
    """The target must be pytestmark: any other module assignment naming
    node (a resolved path, a flag) is not a skip and is not flagged."""
    mod = ast.parse(
        "import shutil\n"
        'NODE_BIN = shutil.which("node")\n')
    assert not _node_mark_lines(mod)


def test_the_guard_leaves_an_unrelated_string_mark_alone() -> None:
    """A mark whose only strings never mention node is not a node skip."""
    mod = ast.parse(
        "import sys\nimport pytest\n"
        'pytestmark = pytest.mark.skipif(sys.platform.startswith("win"),\n'
        '                                reason="the simulation runs bash")\n')
    assert not _node_mark_lines(mod)


def _listed_files() -> list[str]:
    return sorted(
        line.strip()
        for line in LIST_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip())


def _module_level_statements(tree: ast.Module) -> list[tuple[ast.stmt, int]]:
    """Every module-level statement, descending into module-level
    ``if``/``try``/``with``/``for`` bodies (a conditional wrapper hides
    nothing) but never into function or class bodies (a class-level
    ``pytestmark`` is the documented, accepted shape)."""
    out: list[tuple[ast.stmt, int]] = []
    stack: list[ast.stmt] = list(tree.body)
    while stack:
        stmt = stack.pop(0)
        out.append((stmt, stmt.lineno))
        if not isinstance(stmt, (ast.If, ast.Try, ast.With, ast.AsyncWith,
                                 ast.For, ast.AsyncFor, ast.While)):
            continue
        for child in (list(getattr(stmt, "body", []))
                      + list(getattr(stmt, "orelse", []))
                      + list(getattr(stmt, "finalbody", []))
                      + list(getattr(stmt, "handlers", []))):
            if isinstance(child, ast.ExceptHandler):
                stack.extend(child.body)
            else:
                stack.append(child)
    return out


def _node_mark_lines(tree: ast.Module) -> list[int]:
    lines = []
    for stmt, line in _module_level_statements(tree):
        value = _pytestmark_value(stmt)
        if (value is not None and _mentions_node(value)) or (
                _is_module_node_importorskip(stmt)):
            lines.append(line)
    return lines


def _pytestmark_value(stmt: ast.stmt) -> ast.expr | None:
    """The assigned value when stmt binds ``pytestmark``, else None."""
    if isinstance(stmt, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "pytestmark"
            for t in stmt.targets):
        return stmt.value
    if isinstance(stmt, ast.AnnAssign) and isinstance(
            stmt.target, ast.Name) and stmt.target.id == "pytestmark":
        return stmt.value
    return None


def _is_module_node_importorskip(stmt: ast.stmt) -> bool:
    """True for a module-level pytest.importorskip of node, or a bare
    pytest.skip whose reason names node - both skip the whole module."""
    if not isinstance(stmt, ast.Expr) or not isinstance(
            stmt.value, ast.Call):
        return False
    call = stmt.value
    func = call.func
    if not (isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "pytest"
            and func.attr in ("importorskip", "skip")):
        return False
    if func.attr == "importorskip":
        return bool(call.args) and isinstance(
            call.args[0], ast.Constant) and call.args[0].value == "node"
    return _mentions_node_stmt(call)


def _mentions_node(value: ast.expr) -> bool:
    # Deliberately substring-wide: a deny-guard biasing to false positives
    # is the safe direction; a non-node mark whose reason merely contains
    # "node" fails loud, not silent.
    return _mentions_node_stmt(value)


def _mentions_node_stmt(node: ast.AST) -> bool:
    return any(
        isinstance(n, ast.Constant) and isinstance(n.value, str)
        and "node" in n.value
        for n in ast.walk(node))
