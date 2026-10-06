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
from typing import Any, NamedTuple

from tests import db_marker, scan_gate

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


class _StubItem:
    """What mark_db_items needs of a pytest item, no more."""

    def __init__(self, fixturenames: list[str]) -> None:
        self.fixturenames = fixturenames
        self.marks: list[str] = []

    def add_marker(self, marker: Any) -> None:
        self.marks.append(marker.name)


def _scan_relevant(source: str) -> bool:
    """Whether a test module can contribute to either scanner below.

    The vocabulary gate that keeps this file's cost from scaling with
    the size of unrelated test files (issue #510): a module that names
    no fixture and reaches no server is invisible to both the registry
    derivation and the marking guard, and is neither read into the AST
    nor walked. The fixture half admits on compile-level vocabulary
    (tests/scan_gate) — a decorator or a ``getattr(pytest, "fixture")``
    spelling carries ``fixture`` in ``co_names``/``co_consts`` — so a
    file whose only ``fixture`` is prose (a comment or a docstring
    sentence) is skipped. The server half stays text-based: a reach may
    be spelled inside a string literal (a subprocess command, say),
    which co_names does not carry. Fail-closed: a fixture def's
    decorator carries ``fixture`` as an identifier or a whole string
    constant, and a server reach is a Name/Attribute id or raw segment
    text, each spelled verbatim in the source — so a skip can only save
    the walk, never hide a violation. A source that does not compile
    raises out of the gate: loud by construction, and collection fails
    on the same file anyway. Pinned by the plant tests below and by
    the vocabulary-free-module test.
    """
    if any(tok in source for tok in SERVER_CALLS):
        return True
    consts, names = scan_gate.module_vocab(source)
    return "fixture" in consts | names


@functools.cache
def _modules(
        directory: Path | None = None) -> tuple[tuple[str, ast.Module, str], ...]:
    """(module name, AST, source) for every relevant Python file in
    `directory`.

    Defaults to tests/ itself. Cached per directory: both scanners below
    derive from the same tree, and reading and parsing every test module
    once per scanner was most of this file's cost (issue #496).

    Modules the vocabulary gate (``_scan_relevant``) excludes are not
    here: a module that names no fixture and reaches no server cannot
    move either scanner's verdict. The cache is keyed on the directory,
    and that is load-bearing rather than tidy. A test that re-points
    this at a directory of its own and leaves the result cached would
    hand every later reader — including the marking guard below, which
    runs after it by definition order — that directory instead of the
    real tree. The guard would then find nothing to flag and pass,
    which reads exactly like a clean file.
    """
    root = TESTS_DIR if directory is None else directory
    modules = []
    for path in sorted(root.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        if not _scan_relevant(source):
            continue
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


class _Facts(NamedTuple):
    """Per-function facts one module walk collected (see _module_facts).

    The cached instance is SHARED between the registry derivation and
    the marking guard: callers read its fields and build their own
    sets from them, and must never mutate the dicts and sets inside —
    a mutation would poison the other scanner's verdict and read
    exactly like a clean scan.
    """

    functions: dict[str, _AnyFn]
    fixtures: dict[str, str]
    mentions: dict[str, bool]
    calls: dict[str, set[str]]


def _attribute_chain(child: ast.AST, chain: tuple[str, ...],
                     mentions: dict[str, bool],
                     calls: dict[str, set[str]]) -> None:
    """Attribute one node to every function whose body encloses it.

    A server token spelled as a Name/Attribute id roots every enclosing
    function, the way ast.walk(fn) saw it from inside each one; a plain
    call name lands in the call set the fixpoint reads. Module-level
    nodes (an empty chain) belong to no function and update nothing.
    """
    if isinstance(child, ast.Name):
        if child.id in SERVER_CALLS:
            for fname in chain:
                mentions[fname] = True
    elif isinstance(child, ast.Attribute):
        if child.attr in SERVER_CALLS:
            for fname in chain:
                mentions[fname] = True
    elif isinstance(child, ast.Call):
        func = child.func
        if isinstance(func, ast.Name):
            for fname in chain:
                calls[fname].add(func.id)


@functools.cache
def _module_facts(tree: ast.Module, source: str) -> _Facts:
    """Everything both scanners derive, from ONE walk of the tree.

    The single pass that replaced four-plus walks per module (issue
    #675): `_functions`, `_fixture_defs`, `_mention_roots` and each
    fixpoint's per-function call sets each walked the tree separately,
    and the fixpoints re-walked every function per round, so a
    module's cost scaled with its line count several times over. The
    cache shares one facts pass between the registry derivation and
    the marking guard, which read the same modules.

    Attribution is by ENCLOSURE: a node updates every function whose
    body encloses it — the same facts the old per-function walks
    produced, because ast.walk(fn) re-visits nested defs. The
    source-segment mention check stays: a server-call token spelled
    inside a string is not a Name node; it runs per function only
    where the module's own text carries a server token at all, the
    same short-circuit the old mention pass's `or` evaluated.
    """
    functions: dict[str, _AnyFn] = {}
    fixtures: dict[str, str] = {}
    mentions: dict[str, bool] = {}
    calls: dict[str, set[str]] = {}

    def visit(node: ast.AST, chain: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, _AnyFn):
                name = child.name
                functions[name] = child
                mentions.setdefault(name, False)
                calls.setdefault(name, set())
                for dec in child.decorator_list:
                    if _dotted(dec) == "pytest.fixture":
                        fixtures[_fixture_registered_name(dec, name)] = name
                visit(child, chain + (name,))
                continue
            _attribute_chain(child, chain, mentions, calls)
            visit(child, chain)

    visit(tree, ())
    if _mentions_text(source):
        # One split per module (C), then a C-side line slice + join per
        # function: ast.get_source_segment re-split the whole source for
        # every function it measured, which was this module's largest
        # CPU sink after the walk itself (issue #679).
        lines = source.splitlines()
        for name, node in functions.items():
            if not mentions[name] and _mentions_text("\n".join(
                    lines[node.lineno - 1:node.end_lineno])):
                mentions[name] = True
    return _Facts(functions, fixtures, mentions, calls)


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

    Facts come from ONE walk per module (see _module_facts), and the
    fixpoint's rounds move sets only: the old form re-walked every
    not-yet-rooted function per round for call sets that never changed
    between rounds (issue #675).
    """
    facts_of = {name: _module_facts(tree, source)
                for name, tree, source in modules}
    fixtures = {name: facts.fixtures for name, facts in facts_of.items()}
    rooted = {name: {fname for fname, hit in facts.mentions.items() if hit}
              for name, facts in facts_of.items()}
    params_of = {name: {fname: _arg_names(fn)
                        for fname, fn in facts.functions.items()}
                 for name, facts in facts_of.items()}

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
        for mod, facts in facts_of.items():
            mod_calls = facts.calls
            mod_params = params_of[mod]
            mod_rooted = rooted[mod]
            for fname in facts.functions:
                if fname in mod_rooted:
                    continue
                rooted_params = mod_params[fname] & rooted_names
                if mod_calls[fname] & mod_rooted or rooted_params:
                    mod_rooted.add(fname)
                    rooted_names |= {reg for reg, owner
                                     in fixtures[mod].items()
                                     if owner == fname}
                    changed = True
    return _rooted_fixture_names(fixtures, rooted)


@functools.cache
def _relevant_sources(
        directory: Path | None = None) -> tuple[tuple[str, str], ...]:
    """(module name, source) for every relevant module, UNPARSED.

    The admission half of _modules, split out so the narrow derivation
    (issue #715) can walk a subset: reading and admitting is C-side
    cost, while ast.parse and the facts walk are where a module's line
    count is paid. The cache is keyed on the directory for the same
    reason _modules' is: a caller pointing this at a directory of its
    own must never displace the real tree for later readers.
    """
    root = TESTS_DIR if directory is None else directory
    out = []
    for path in sorted(root.glob("*.py")):
        source = path.read_text(encoding="utf-8")
        if _scan_relevant(source):
            out.append((path.stem, source))
    return tuple(out)


def _derive_narrow(directory: Path | None = None) -> set[str]:
    """The DB-rooted fixture names, from the tree on demand (issue #715).

    The mention roots live in server-text modules by construction: a
    function roots when its body mentions a server call, and the reach
    is an identifier or a segment string spelled verbatim in the
    source, so the module carrying it names a server call in its text.
    The fixpoint then follows chains through the REMAINING fixture
    modules on demand: each round resolves the registry names so far
    through the vocabulary index (tests/scan_gate.module_def_vocab),
    parses only the modules that may define or request one of them,
    and re-runs the fixpoint. Over-inclusive by construction — the
    index admits on any identifier, parameter, local or string
    constant equal to a registry name — so a skip can only save the
    walk, never hide a LIVE chain member: a def spelled only inside a
    dead branch carries its parameters in no surviving code object
    and is invisible to the index, but it also never executes and
    never registers at runtime, so narrow's answer matches the
    registry the guard asserts against (see
    scan_gate.module_def_vocab). The registry test below and the
    planted-tree pins (tests/test_db_marker_narrow.py) prove the
    narrowed path equals the full sweep.
    """
    sources = dict(_relevant_sources(directory))
    chosen = {n for n, s in sources.items()
              if any(tok in s for tok in SERVER_CALLS)}
    unchosen = set(sources) - chosen
    may: dict[str, frozenset[str]] = {}
    suspect: dict[str, bool] = {}
    for n in unchosen:
        may[n], suspect[n] = scan_gate.module_def_vocab(sources[n])
    parsed: dict[str, ast.Module] = {}
    while True:
        modules = []
        for n in sorted(chosen):
            if n not in parsed:
                parsed[n] = ast.parse(sources[n])
            modules.append((n, parsed[n], sources[n]))
        derived = _derive_from_modules(modules)
        hits = {n for n in unchosen - chosen
                if suspect[n] and may[n] & derived}
        if not hits:
            return derived
        chosen |= hits


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
    derived = _derive_narrow()
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


def test_scanning_another_directory_cannot_displace_the_real_tree(tmp_path):
    # The leak this pins: a cache keyed on nothing let a caller that
    # scanned a directory of its own hand that directory to every later
    # reader — including the marking guard below, which runs after it by
    # definition order. The guard then found nothing to flag and passed,
    # which reads exactly like a clean file. `_modules` keys on the
    # directory, so this holds no matter what the caller does.
    (tmp_path / "test_seeded_scan.py").write_text(
        "import pytest\n\n\n@pytest.fixture\ndef seeded():\n"
        "    return None\n", encoding="utf-8")
    assert [name for name, _, _ in _modules(tmp_path)] == [
        "test_seeded_scan"]
    (tmp_path / "test_second_scan.py").write_text(
        "import pytest\n\n\n@pytest.fixture\ndef other():\n"
        "    return None\n", encoding="utf-8")
    # The cached entry for THAT directory still stands ...
    assert [name for name, _, _ in _modules(tmp_path)] == [
        "test_seeded_scan"]
    # ... it takes a clear to see the second file ...
    _modules.cache_clear()
    assert sorted(name for name, _, _ in _modules(tmp_path)) == [
        "test_second_scan", "test_seeded_scan"]
    # ... and the real tree was never displaced at any point above.
    assert "test_db_marker" in {name for name, _, _ in _modules()}


def _marking_offenders(directory: Path | None = None) -> list[str]:
    """Tests reaching a server with neither a DB fixture nor the mark.

    Scans only modules whose text names a server call: a module without
    one has no rooted test (a root is a Name/Attribute id or raw
    segment text, both verbatim in the source), so scanning it can add
    no offender and is pure walk cost (issue #510).

    Each module's facts come from ONE walk (_module_facts, issue
    #675): the old form re-walked every function per fixpoint round
    for call sets that never changed between rounds. The two callers
    share the ONE _modules cache entry the derive builds: keyed on the
    caller's argument tuple, _modules() and _modules(None) were two
    entries and two full parse passes, and the guard re-walked the
    server modules' facts (issue #679).
    """
    offenders = []
    for mod_name, tree, source in (_modules() if directory is None
                                   else _modules(directory)):
        if not any(tok in source for tok in SERVER_CALLS):
            continue
        facts = _module_facts(tree, source)
        rooted = {fname for fname, hit in facts.mentions.items() if hit}
        # The same helper-call closure the registry derivation uses:
        # a test that reaches the server through a helper it calls is
        # a DB test whatever its own body spells. SAME-MODULE ONLY:
        # a reach through a helper defined in a DIFFERENT test module
        # is missed here and caught only by the portable CI cell, which
        # runs -m "not db" with no server to answer it.
        changed = True
        while changed:
            changed = False
            for fname in facts.functions:
                if fname in rooted:
                    continue
                if facts.calls[fname] & rooted:
                    rooted.add(fname)
                    changed = True
        for fname, fn in facts.functions.items():
            if not fname.startswith("test_") or fname not in rooted:
                continue
            if _arg_names(fn) & db_marker.DB_FIXTURES:
                continue
            if _marked_db(fn):
                continue
            entry = f"{mod_name}.py:{fname}"
            if db_marker.MARK_ALLOWLIST.get(entry):
                continue
            offenders.append(entry)
    return offenders


def test_every_server_touching_test_requests_a_db_fixture_or_carries_the_mark():
    assert not _marking_offenders(), (
        "tests that reach a PostgreSQL server through a scratch_db call "
        "with neither a DB fixture in their parameter list nor "
        "@pytest.mark.db — the portable CI matrix would run them and "
        "fail on the missing server. Add the mark, request a DB "
        "fixture, or allowlist the entry here with a reason: "
        + ", ".join(_marking_offenders()))


# ---- the vocabulary gate's fail-closed pins (issue #510) ------------

# The server token, split: this file's own text must never contain it,
# so the marking guard never sees this file as rooted (the same trick
# _seeded_modules uses).
# pylint: disable-next=implicit-str-concat
SERVER = 'viz_' 'conn'


def test_a_seeded_server_call_is_flagged_without_the_mark(tmp_path):
    # Mutation proof for the gate: a fresh file whose only server
    # reach is spelled inside it must still be flagged.
    (tmp_path / "test_planted.py").write_text(
        "def test_reaches_the_server():\n"
        f"    return {SERVER}()\n", encoding="utf-8")
    assert _marking_offenders(tmp_path) == [
        "test_planted.py:test_reaches_the_server"]


def test_a_seeded_server_call_with_a_db_fixture_is_clean(tmp_path):
    # The complement: the same reach with a registered DB fixture in
    # the parameter list is not an offender — the guard flags the
    # missing registration, not the reach.
    (tmp_path / "test_planted.py").write_text(
        "def test_reaches_the_server(fresh_db):\n"
        f"    return {SERVER}()\n", encoding="utf-8")
    assert not _marking_offenders(tmp_path)


def test_a_seeded_rooted_fixture_arrives_in_the_derived_registry(tmp_path):
    # The registry scan's plant: a fresh fixture-defining file whose
    # fixture is rooted must arrive in the derived set — the registry
    # guard would then fail until it is registered in db_marker.
    # Spelled with odd decorator spacing on purpose: the gate admits
    # on the `fixture` substring, so spacing cannot dodge it.
    (tmp_path / "test_planted.py").write_text(
        "import pytest\n\n\n@pytest .fixture\n"
        f"def grown():\n    return {SERVER}()\n", encoding="utf-8")
    assert "grown" in _derive_from_modules(_modules(tmp_path))


def test_a_vocabulary_free_module_is_not_scanned(tmp_path):
    # The structural cost pin (issue #510): a module that names no
    # fixture and reaches no server never enters the module list the
    # scanners walk, so adding one re-prices the suite by nothing but
    # the gate's own substring check.
    (tmp_path / "test_plain.py").write_text(
        "import os\n\n\ndef test_ok():\n    assert os.sep == '/'\n",
        encoding="utf-8")
    assert not _modules(tmp_path)
