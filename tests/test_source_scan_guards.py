"""Shared preprocessing and mutation contracts for tree-scanning guards."""
from __future__ import annotations

import ast
import builtins
from collections import Counter
from collections.abc import Callable
import io
from pathlib import Path
import time
import tokenize
import types
from typing import Any

import pytest

from tests import source_scan_cache, source_scan_index
from tests.test_db_marker import (_Facts, _derive_narrow, _marking_offenders,
                                  _module_facts, _modules)
from tests import test_db_marker as db
from tests import test_no_pinned_version_literals as version
from tests.test_no_pinned_version_literals import _tree_problems
from tests import test_scratch_db as scratch


def _seed(label: str, *, clean: bool = False) -> str:
    server = "viz_" + "conn"
    statement = "CREATE " + "DATABASE seeded"
    mark = "@pytest.mark.db\n" if clean else ""
    allow = "  # sv-test-data: allow (synthetic)" if clean else ""
    sql = f"# {statement}" if clean else f"sql = {statement!r}"
    fixture_body = "None" if clean else f"{server}()"
    test_body = f"{server}()" if clean else "seeded()"
    return (f"label = {label!r}\nimport pytest\n"
            "@pytest.fixture\ndef seeded():\n"
            f"    return {fixture_body}\n"
            f"{mark}def test_server():\n    return {test_body}\n"
            f"constants.PARSER_VERSION = 'synthetic'{allow}\n{sql}\n")


def _stable_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    # Seeded files are outside the racy-entry margin at first validation.
    now = time.time_ns() + 10_000_000_000
    monkeypatch.setattr(source_scan_cache, "_now_ns", lambda: now)


def _wanted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(version, "_DEPLOYED_WANTED",
                        (frozenset({"synthetic-model"}), frozenset()))


def _guards(root: Path) -> tuple[Callable[[], Any], ...]:
    return (lambda: _derive_narrow(root),
            lambda: _marking_offenders(root),
            lambda: _tree_problems(root),
            lambda: scratch.offending_files(root))


def _counts(path: Path, text: str, monkeypatch: pytest.MonkeyPatch
            ) -> Counter[str]:
    counts: Counter[str] = Counter()
    originals = types.SimpleNamespace(
        open=Path.open, parse=ast.parse, tokens=tokenize.generate_tokens,
        compile=builtins.compile, build=getattr(source_scan_index, "_build"),
        walk=ast.walk)
    trees: list[ast.AST] = []

    def opened(file: Path, *args: Any, **kwargs: Any) -> Any:
        if file.resolve() == path.resolve():
            counts["read"] += 1
        return originals.open(file, *args, **kwargs)

    def parsed(source: Any, *args: Any, **kwargs: Any) -> ast.AST:
        tree = originals.parse(source, *args, **kwargs)
        if source == text:
            counts["parse"] += 1
            trees.append(tree)
        return tree

    def tokens(readline: Callable[[], str]) -> Any:
        reader = getattr(readline, "__self__", None)
        if isinstance(reader, io.StringIO) and reader.getvalue() == text:
            counts["tokens"] += 1
        return originals.tokens(readline)

    def compiled(source: Any, filename: str, *args: Any,
                 **kwargs: Any) -> Any:
        if filename == "<scan_gate>" and (
                source == text or any(source is tree for tree in trees)):
            counts["compile"] += 1
        return originals.compile(source, filename, *args, **kwargs)

    def built(code: Any) -> Any:
        if path.name in code.co_consts:
            counts["index"] += 1
        return originals.build(code)

    def walked(tree: ast.AST) -> Any:
        if any(tree is item for item in trees):
            counts["walk"] += 1
        return originals.walk(tree)

    monkeypatch.setattr(Path, "open", opened)
    monkeypatch.setattr(ast, "parse", parsed)
    monkeypatch.setattr(tokenize, "generate_tokens", tokens)
    monkeypatch.setattr(builtins, "compile", compiled)
    monkeypatch.setattr(source_scan_index, "_build", built)
    monkeypatch.setattr(ast, "walk", walked)
    return counts


@pytest.mark.parametrize("reverse", [False, True])
def test_pinned_guards_share_one_preprocessing_pass(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reverse: bool) -> None:
    path = tmp_path / f"test_{tmp_path.name}.py"
    text = _seed(path.name)
    path.write_text(text, encoding="utf-8")
    _stable_clock(monkeypatch)
    _wanted(monkeypatch)
    counts = _counts(path, text, monkeypatch)
    guards = _guards(tmp_path)
    if reverse:
        guards = guards[::-1]
    first = tuple(guard() for guard in guards)
    assert tuple(guard() for guard in guards) == first
    assert _derive_narrow(tmp_path) == {"seeded"}
    assert _marking_offenders(tmp_path) == [f"{path.name}:test_server"]
    assert len(_tree_problems(tmp_path)) == 1
    assert scratch.offending_files(tmp_path) == [path.name]
    assert counts == {"read": 1, "parse": 1, "tokens": 1,
                      "compile": 1, "index": 1, "walk": 1}


def _facts_state(facts: _Facts) -> object:
    return (dict(facts.functions), dict(facts.fixtures), dict(facts.mentions),
            {name: set(calls) for name, calls in facts.calls.items()})


def test_same_path_mutation_updates_all_guards_without_clearing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "test_mutation.py"
    _wanted(monkeypatch)
    path.write_text(_seed(tmp_path.name, clean=True), encoding="utf-8")
    guards = _guards(tmp_path)
    assert tuple(guard() for guard in guards) == (set(), [], [], [])
    path.write_text(_seed(tmp_path.name), encoding="utf-8")
    derived, marking, versions, scratches = (guard() for guard in guards)
    assert derived == {"seeded"}
    assert marking == ["test_mutation.py:test_server"]
    assert len(versions) == 1 and "PARSER_VERSION" in versions[0]
    assert scratches == ["test_mutation.py"]
    _, tree, text = _modules(tmp_path)[0]
    facts = _module_facts(tree, text)
    tree_before, facts_before = ast.dump(tree), _facts_state(facts)
    assert tuple(guard() for guard in guards[::-1]) == (
        scratches, versions, marking, derived)
    assert _modules(tmp_path)[0][1] is tree
    assert _module_facts(tree, text) is facts
    assert ast.dump(tree) == tree_before
    assert _facts_state(facts) == facts_before


def test_admission_skips_unneeded_tokens_and_db_facts(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "test_plain.py"
    text = f"label = {tmp_path.name!r}\ndef test_plain():\n    pass\n"
    path.write_text(text, encoding="utf-8")
    _stable_clock(monkeypatch)
    _wanted(monkeypatch)
    counts = _counts(path, text, monkeypatch)

    def unexpected_facts(*_args: Any) -> Any:
        raise AssertionError("Excluded module must not collect DB facts")

    monkeypatch.setattr(db, "_module_facts", unexpected_facts)
    assert tuple(guard() for guard in _guards(tmp_path)) == (set(), [], [], [])
    assert counts["tokens"] == 0
    assert counts["walk"] == 0


def test_version_admitted_without_sites_or_markers_skips_tokens(
        monkeypatch: pytest.MonkeyPatch) -> None:
    text = "constants.PARSER_VERSION = derived_value\n"

    def unexpected_tokens(_readline: Any) -> Any:
        raise AssertionError("Site-free, marker-free source must not tokenize")

    monkeypatch.setattr(tokenize, "generate_tokens", unexpected_tokens)
    assert version.check(text) == []
