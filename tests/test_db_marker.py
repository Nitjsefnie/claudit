"""Guards for the mechanical db/portable test split (issue #27).

tests/db_marker.py registers every fixture name whose invocation hands
the test a PostgreSQL-backed database, and the conftest hook applies the
`db` marker to every collected test requesting one — transitively,
because pytest's item.fixturenames is the transitive fixture closure
(a test taking `app_with_data`, imported from test_api into another
module, marks through it without db_marker knowing that module exists).

Tests that reach a server WITHOUT any DB fixture carry @pytest.mark.db
explicitly. The split must stay mechanical, so this module derives what
the registry and the explicit marks SHOULD be, straight from tests/*.py
source, and fails when the two disagree in either direction:

- the registry must EQUAL the fixtures the source roots on server
  calls, transitively over fixture parameters and helper calls — a
  forgotten registration and a stale entry both fail;
- every test whose body reaches the server (directly, through a helper
  it calls, or inside a command spelled into a subprocess string) must
  request a DB fixture or carry the mark, unless it is allowlisted here
  with a reason.

Server reach is matched as exact identifiers — the scratch_db module's
own API, plus backend's pooled connection — so a test that merely sets
DATABASE_URL or touches the db module's pool OBJECT (test_db_pool_
stale_checkout's fake-connection tests) stays unflagged: neither
connects on its own.
"""
from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from tests import db_marker

TESTS_DIR = Path(__file__).resolve().parent

# Calls that open a server connection or create/drop/lease/sweep
# databases on one: scratch_db's API plus backend.db's pooled
# connection context manager. Matched as exact identifiers.
SERVER_CALLS = frozenset({
    "admin_connection",
    "create_database",
    "create_empty_database",
    "drop_database",
    "drop_run_databases",
    "hold_run_lease",
    "scratch_viz_database",
    "sweep_stale_databases",
    "viz_conn",
})

# Either flavour of a function definition; the AST scan never cares
# which spelling a test module used.
_AnyFn = ast.FunctionDef | ast.AsyncFunctionDef

# Tests the scan flags whose bodies provably never reach a server, each
# with the reason. Empty today: every flagged test either requests a DB
# fixture or carries the explicit mark.
MARK_ALLOWLIST: dict[str, str] = {
    "test_version.py:test_health_error_branch_reports_version":
        "monkeypatches db.viz_conn with a function that raises, so the "
        "health endpoint's error branch runs with no server at all",
    "test_version.py:test_health_error_branch_answers_503":
        "monkeypatches db.viz_conn with a function that raises, so the "
        "health endpoint's error branch runs with no server at all",
    "test_version.py:test_health_ok_branch_reports_version":
        "monkeypatches db.viz_conn with a fake connection, so the "
        "health endpoint's ok branch runs with no server at all",
    "test_schema_autoapply.py:test_apply_schema_unlock_failure_does_not_mask_the_ddl_error":
        "monkeypatches db.viz_conn with a fake connection, so "
        "apply_schema's unlock guard runs with no server at all",
    "test_schema_autoapply.py:test_apply_schema_swallows_unlock_failure_after_success":
        "monkeypatches db.viz_conn with a fake connection, so "
        "apply_schema's unlock guard runs with no server at all",
}


class _StubItem:
    """What mark_db_items needs of a pytest item, no more."""

    def __init__(self, fixturenames: list[str]) -> None:
        self.fixturenames = fixturenames
        self.marks: list[str] = []

    def add_marker(self, marker: Any) -> None:
        self.marks.append(marker.name)


def _modules() -> Iterator[tuple[str, ast.Module, str]]:
    """(module name, AST, source) for every Python file in tests/."""
    for path in sorted(TESTS_DIR.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        yield path.stem, ast.parse(source, filename=str(path)), source


def _dotted(node: ast.expr) -> str:
    """Dotted spelling of a Name/Attribute chain; '' for anything else."""
    if isinstance(node, ast.Call):
        return _dotted(node.func)
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return ""


def _functions(tree: ast.Module) -> dict[str, _AnyFn]:
    return {node.name: node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _arg_names(fn: _AnyFn) -> set[str]:
    a = fn.args
    return {x.arg for x in [*a.posonlyargs, *a.args, *a.kwonlyargs]}


def _fixture_registered_name(dec: ast.expr, default: str) -> str:
    """The name a fixture is registered under: the decorator's `name=`
    keyword when it names one, else the function's own name."""
    if isinstance(dec, ast.Call):
        for kw in dec.keywords:
            if kw.arg == "name":
                val = ast.literal_eval(kw.value)
                if isinstance(val, str):
                    return val
    return default


def _fixture_defs(tree: ast.Module) -> dict[str, str]:
    """Registered fixture name -> defining function name, one module."""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if _dotted(dec) == "pytest.fixture":
                out[_fixture_registered_name(dec, node.name)] = node.name
    return out


def _mentions_tokens(fn: ast.AST) -> bool:
    return any(isinstance(node, (ast.Name, ast.Attribute))
               and (node.id if isinstance(node, ast.Name) else node.attr)
               in SERVER_CALLS
               for node in ast.walk(fn))


def _mentions_text(source: str) -> bool:
    """The same match on raw text, so a server call spelled inside a
    subprocess command string is seen too."""
    return any(token in source for token in SERVER_CALLS)


def _mention_roots(funcs: dict[str, _AnyFn],
                   source: str) -> set[str]:
    """Functions whose own body mentions a server call, by name."""
    return {name for name, fn in funcs.items()
            if _mentions_tokens(fn)
            or _mentions_text(_segment(source, fn))}


def _segment(source: str, fn: _AnyFn) -> str:
    seg = ast.get_source_segment(source, fn)
    return seg if seg is not None else ""


def _registered_fixtures(modules) -> dict[str, dict[str, str]]:
    return {name: _fixture_defs(tree) for name, tree, _ in modules}


def _rooted_fixture_names(fixtures: dict[str, dict[str, str]],
                          rooted: dict[str, set[str]]) -> set[str]:
    """Registered fixture names whose defining function is rooted."""
    return {reg for mod, mapping in fixtures.items()
            for reg, fname in mapping.items() if fname in rooted[mod]}


def _derive_db_fixtures() -> set[str]:
    """The DB-rooted fixture names, re-derived from tests/*.py.

    A fixture (or the helper it calls) is DB-rooted when its body
    mentions a server call, or when it requests — as a parameter — a
    fixture already known rooted. Iterated to a fixpoint across every
    module, because fixture parameters refer to registered names that
    another module may define.
    """
    modules = list(_modules())
    funcs = {name: _functions(tree) for name, tree, _ in modules}
    fixtures = _registered_fixtures(modules)
    rooted = {name: _mention_roots(funcs[name], source)
              for name, _, source in modules}

    changed = True
    while changed:
        changed = False
        for mod, fns in funcs.items():
            for fname, fn in fns.items():
                if fname in rooted[mod]:
                    continue
                calls = {n.func.id for n in ast.walk(fn)
                         if isinstance(n, ast.Call)
                         and isinstance(n.func, ast.Name)}
                rooted_params = (_arg_names(fn)
                                 & _rooted_fixture_names(fixtures, rooted))
                if calls & rooted[mod] or rooted_params:
                    rooted[mod].add(fname)
                    changed = True
    return _rooted_fixture_names(fixtures, rooted)


def _marked_db(fn: _AnyFn) -> bool:
    return any(_dotted(dec) == "pytest.mark.db"
               for dec in fn.decorator_list)


def test_a_test_requesting_a_db_fixture_is_marked():
    assert _mark_applied(["fresh_db"]) is True


def test_a_test_requesting_no_db_fixture_is_not_marked():
    assert _mark_applied(["monkeypatch", "tmp_path"]) is False


def test_an_imported_db_fixture_marks_through_the_closure():
    # app_with_data is defined in test_api and re-exported by
    # test_schema_autoapply / test_api_token_types; the requesting test
    # lives in the importing module. item.fixturenames carries it all
    # the same.
    assert _mark_applied(["app_with_data"]) is True


def test_the_mark_is_added_once_even_with_several_db_fixtures():
    item = _StubItem(["fresh_db", "app_with_data"])
    db_marker.mark_db_items([item])
    assert item.marks == ["db"]


def _mark_applied(fixturenames: list[str]) -> bool:
    item = _StubItem(fixturenames)
    db_marker.mark_db_items([item])
    return item.marks == ["db"]


def test_the_registry_equals_what_the_source_derives():
    derived = _derive_db_fixtures()
    unregistered = derived - db_marker.DB_FIXTURES
    stale = db_marker.DB_FIXTURES - derived
    assert not unregistered and not stale, (
        "DB_FIXTURES disagrees with tests/*.py source — a test that "
        "needs a database would go unmarked (portable CI would try to "
        f"run it) or a stale entry would over-mark. Missing: "
        f"{sorted(unregistered)}; stale: {sorted(stale)}. Register the "
        "fixture in tests/db_marker.py, or unroot the fixture.")
    assert "fresh_db" in derived, (
        "fresh_db — the canonical DB root fixture — no longer derives "
        "as DB-rooted; the split has silently gone empty.")


def test_every_server_touching_test_requests_a_db_fixture_or_carries_the_mark():
    offenders = []
    for mod_name, tree, source in _modules():
        funcs = _functions(tree)
        rooted = _mention_roots(funcs, source)
        # The same helper-call closure the registry derivation uses:
        # a test that reaches the server through a helper it calls is
        # a DB test whatever its own body spells. SAME-MODULE ONLY:
        # a reach through a helper defined in a DIFFERENT test module
        # is missed here and caught only by the portable CI cell, which
        # runs -m "not db" with no server to answer it.
        changed = True
        while changed:
            changed = False
            for fname, fn in funcs.items():
                if fname in rooted:
                    continue
                calls = {n.func.id for n in ast.walk(fn)
                         if isinstance(n, ast.Call)
                         and isinstance(n.func, ast.Name)}
                if calls & rooted:
                    rooted.add(fname)
                    changed = True
        for fname, fn in funcs.items():
            if not fname.startswith("test_") or fname not in rooted:
                continue
            if _arg_names(fn) & db_marker.DB_FIXTURES:
                continue
            if _marked_db(fn):
                continue
            entry = f"{mod_name}.py:{fname}"
            if MARK_ALLOWLIST.get(entry):
                continue
            offenders.append(entry)
    assert not offenders, (
        "tests that reach a PostgreSQL server through a scratch_db call "
        "with neither a DB fixture in their parameter list nor "
        "@pytest.mark.db — the portable CI matrix would run them and "
        "fail on the missing server. Add the mark, request a DB "
        "fixture, or allowlist the entry here with a reason: "
        + ", ".join(offenders))
