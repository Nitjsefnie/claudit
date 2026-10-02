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
import functools
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
# with the reason. Every entry names a test that EXISTS: an entry for a
# name no test carries excuses nothing, and reads as cover while the real
# offender is left to be found.
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
    "test_version.py:test_health_last_ingest_carries_newer":
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


@functools.cache
def _modules(
        directory: Path | None = None) -> tuple[tuple[str, ast.Module, str], ...]:
    """(module name, AST, source) for every Python file in `directory`.

    Defaults to tests/ itself. Cached per directory: both scanners below
    derive from the same tree, and reading and parsing every test module
    once per scanner was most of this file's cost (issue #496).

    The cache is keyed on the directory, and that is load-bearing rather
    than tidy. A test that re-points this at a directory of its own and
    leaves the result cached would hand every later reader — including
    the marking guard below, which runs after it by definition order —
    that directory instead of the real tree. The guard would then find
    nothing to flag and pass, which reads exactly like a clean file.
    """
    root = TESTS_DIR if directory is None else directory
    modules = []
    for path in sorted(root.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        modules.append(
            (path.stem, ast.parse(source, filename=str(path)), source))
    return tuple(modules)


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


def _derive_from_modules(modules) -> set[str]:
    """The DB-rooted fixture names, re-derived from `modules`.

    A fixture (or the helper it calls) is DB-rooted when its body
    mentions a server call, or when it requests — as a parameter — a
    fixture already known rooted. Iterated to a fixpoint across every
    module, because fixture parameters refer to registered names that
    another module may define.

    Takes its modules as an argument so a seeded tree can be run through
    exactly this code, which is what makes the optimisation's parity
    testable against the full-sweep form rather than against a copy.
    """
    funcs = {name: _functions(tree) for name, tree, _ in modules}
    fixtures = _registered_fixtures(modules)
    rooted = {name: _mention_roots(funcs[name], source)
              for name, _, source in modules}

    # The names a function's PARAMETERS can root, as an incremental
    # union rather than a fresh sweep of every module's fixture map per
    # function per round. `rooted` is the sweep's only moving input and
    # it only ever grows by one defining function in one module, so the
    # union gains exactly that module's names for that function: the
    # value here is `_rooted_fixture_names(fixtures, rooted)` at every
    # point of use, at a thousandth of the visits (issue #496).
    rooted_names = _rooted_fixture_names(fixtures, rooted)
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
                rooted_params = _arg_names(fn) & rooted_names
                if calls & rooted[mod] or rooted_params:
                    rooted[mod].add(fname)
                    rooted_names |= {reg for reg, owner
                                     in fixtures[mod].items()
                                     if owner == fname}
                    changed = True
    return _rooted_fixture_names(fixtures, rooted)


def _derive_db_fixtures() -> set[str]:
    """The DB-rooted fixture names, re-derived from tests/*.py."""
    return _derive_from_modules(_modules())


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


def _derive_by_sweep(modules) -> set[str]:
    """The derivation exactly as it read before the incremental union.

    The control the optimisation is measured against: it asks
    `_rooted_fixture_names` for a full sweep of every module's fixture
    map at every call, which is what `_derive_from_modules` used to do.
    """
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


def _seeded_modules() -> tuple:
    """Three modules chained by parameter name, as (name, AST, source).

    `seeded_root` mentions a server call, so it is a root from the first
    round. `late_root` roots only once `seeded_root` is a rooted NAME --
    the step where the union gains something the initial sweep could not
    carry. `consumer` then roots through `late_root`, and it is
    REGISTERED, so a union that dropped the delta would leave it
    unrooted: the seeded violation this parity pair has to catch.
    """
    # The server token is spelled in two halves ON PURPOSE: the seeded
    # AST still contains it, so `seeded_root` is still a mention root
    # and the chain still starts here, while this file's own text never
    # contains the token and the marking guard below sees no server call
    # to flag. Nothing here reaches a server; the string is only parsed.
    # pylint: disable-next=implicit-str-concat
    server = 'viz_' 'conn'
    sources = {
        "seeded": 'import pytest\n\n\n@pytest.fixture\n'
                  f'def seeded_root():\n    return {server}()\n',
        "late": 'import pytest\n\n\n@pytest.fixture\n'
                'def late_root(seeded_root):\n    return None\n',
        "consumer": 'import pytest\n\n\n@pytest.fixture\n'
                    'def consumer(late_root):\n    return None\n',
    }
    return tuple((name, ast.parse(src), src) for name, src in sources.items())


def test_the_incremental_derivation_equals_the_full_sweep_on_real_source():
    # Parity on REAL sources, both forms over the same slice: a union
    # that dropped or double-counted a name would move a fixture in or
    # out of the registry this module guards. The slice is the largest
    # few real test modules, not the whole tree -- running the sweep
    # form over all of it is the very cost this change removes, and a
    # guard that costs more than the thing it guards is not a guard.
    biggest = sorted(_modules(), key=lambda m: -len(m[2]))[:4]
    assert len(biggest) == 4, biggest
    assert _derive_from_modules(biggest) == _derive_by_sweep(biggest)


def test_the_incremental_derivation_equals_the_full_sweep_on_seeded_modules():
    # The same pair on a seeded tree whose transitive chain runs THROUGH
    # the union's delta, and which ends in a registered fixture that must
    # be rooted. Both forms are the production code path: `_sweep` is the
    # pre-optimisation control, `_derive_from_modules` is what ships, so
    # breaking the union fails here rather than passing vacuously.
    modules = _seeded_modules()
    derived = _derive_from_modules(modules)
    assert derived == _derive_by_sweep(modules)
    assert derived == {"seeded_root", "late_root", "consumer"}


def test_scanning_another_directory_cannot_displace_the_real_tree(tmp_path):
    # The leak this pins: a cache keyed on nothing let a caller that
    # scanned a directory of its own hand that directory to every later
    # reader — including the marking guard below, which runs after it by
    # definition order. The guard then found nothing to flag and passed,
    # which reads exactly like a clean file. `_modules` keys on the
    # directory, so this holds no matter what the caller does.
    (tmp_path / "test_seeded_scan.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8")
    assert [name for name, _, _ in _modules(tmp_path)] == [
        "test_seeded_scan"]
    (tmp_path / "test_second_scan.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8")
    # The cached entry for THAT directory still stands ...
    assert [name for name, _, _ in _modules(tmp_path)] == [
        "test_seeded_scan"]
    # ... it takes a clear to see the second file ...
    _modules.cache_clear()
    assert sorted(name for name, _, _ in _modules(tmp_path)) == [
        "test_second_scan", "test_seeded_scan"]
    # ... and the real tree was never displaced at any point above.
    assert "test_db_marker" in {name for name, _, _ in _modules()}


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
