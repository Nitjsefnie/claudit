"""No node-listed test file carries a module-level node skip (#743, #746).

A module-wide ``pytestmark`` node skip silently skips the file's
Python-only tests on a runner without node: #743 found it in one file,
#746 in seven more, and the module mark is exactly the shape that comes
back when nobody is looking. The guard reads
scripts/ci/node_test_files.txt and fails naming file and line when any
listed file assigns a module-level ``pytestmark`` whose value mentions
node. The skip must ride only the tests that run node — a per-test
skipif, or a class-level ``pytestmark`` on a class whose every test
drives node.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LIST_FILE = REPO_ROOT / "scripts" / "ci" / "node_test_files.txt"


def test_no_node_listed_file_sets_a_module_level_node_skip() -> None:
    """The node skip rides only the tests that run node — never the module."""
    offenders = []
    for rel in _listed_files():
        tree = ast.parse(
            (REPO_ROOT / rel).read_text(encoding="utf-8"))
        for stmt in tree.body:
            value = _pytestmark_value(stmt)
            if value is not None and _mentions_node(value):
                offenders.append(f"{rel}:{stmt.lineno}")
    assert not offenders, (
        "module-level node skip(s) found; the skip must ride only the "
        "tests that run node (issues #743, #746): " + ", ".join(offenders))


def _listed_files() -> list[str]:
    return sorted(
        line.strip()
        for line in LIST_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip())


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


def _mentions_node(value: ast.expr) -> bool:
    return any(
        isinstance(n, ast.Constant) and isinstance(n.value, str)
        and "node" in n.value
        for n in ast.walk(value))
