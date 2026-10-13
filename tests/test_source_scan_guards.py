"""Shared preprocessing and mutation contracts for tree-scanning guards."""
from __future__ import annotations

import ast
import builtins
from collections import Counter, defaultdict
from collections.abc import Callable
import io
import re
from pathlib import Path
import time
import tokenize
import types
from typing import Any

import pytest

from tests import source_scan_cache, source_scan_index
from tests.test_db_marker import (_Facts, _derive_narrow, _marking_offenders,
                                  _module_facts, _modules, _relevant_sources)
from tests import test_db_marker as db
from tests import test_no_pinned_version_literals as version
from tests.test_no_pinned_version_literals import _tree_problems
from tests import test_scratch_db as scratch
from tests import test_js_toolchain as js_toolchain
from tests import test_node_skip_guard as node_guard
from tests import test_parser_js_lanes_load as js_lanes
from tests import test_scan_gate as scan_property
from tests.node_skip_guard_impl import _node_skip_offenders


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


def _file_counts(texts: dict[Path, str], monkeypatch: pytest.MonkeyPatch
                 ) -> defaultdict[str, Counter[str]]:
    """Per-file reads, parses, tokenizations, compiles, index builds and
    whole-tree walks; each seed carries its own file name as a constant."""
    counts: defaultdict[str, Counter[str]] = defaultdict(Counter)
    paths = {path.resolve(): path.name for path in texts}
    names = {text: path.name for path, text in texts.items()}
    originals = types.SimpleNamespace(
        open=Path.open, parse=ast.parse, tokens=tokenize.generate_tokens,
        compile=builtins.compile, build=getattr(source_scan_index, "_build"),
        walk=ast.walk)
    trees: list[tuple[ast.AST, str]] = []

    def opened(file: Path, *args: Any, **kwargs: Any) -> Any:
        if (name := paths.get(file.resolve())) is not None:
            counts[name]["read"] += 1
        return originals.open(file, *args, **kwargs)

    def parsed(source: Any, *args: Any, **kwargs: Any) -> ast.AST:
        tree = originals.parse(source, *args, **kwargs)
        if isinstance(source, str) and source in names:
            counts[names[source]]["parse"] += 1
            trees.append((tree, names[source]))
        return tree

    def tree_name(tree: object) -> str | None:
        return next((name for item, name in trees if item is tree), None)

    def tokens(readline: Callable[[], str]) -> Any:
        reader = getattr(readline, "__self__", None)
        if isinstance(reader, io.StringIO) and reader.getvalue() in names:
            counts[names[reader.getvalue()]]["tokens"] += 1
        return originals.tokens(readline)

    def compiled(source: Any, filename: str, *args: Any,
                 **kwargs: Any) -> Any:
        name = (names.get(source) if isinstance(source, str)
                else tree_name(source))
        if filename == "<scan_gate>" and name is not None:
            counts[name]["compile"] += 1
        return originals.compile(source, filename, *args, **kwargs)

    def built(code: Any) -> Any:
        for name in set(names.values()) & set(code.co_consts):
            counts[name]["index"] += 1
        return originals.build(code)

    def walked(tree: ast.AST) -> Any:
        if (name := tree_name(tree)) is not None:
            counts[name]["walk"] += 1
        return originals.walk(tree)

    monkeypatch.setattr(Path, "open", opened)
    monkeypatch.setattr(ast, "parse", parsed)
    monkeypatch.setattr(tokenize, "generate_tokens", tokens)
    monkeypatch.setattr(builtins, "compile", compiled)
    monkeypatch.setattr(source_scan_index, "_build", built)
    monkeypatch.setattr(ast, "walk", walked)
    return counts


def _counts(path: Path, text: str, monkeypatch: pytest.MonkeyPatch
            ) -> Counter[str]:
    return _file_counts({path: text}, monkeypatch)[path.name]


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
    # The real clock keeps each rewrite inside the racy margin, so this
    # drives the re-read-and-rehash path, not the stat fast path.
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
    assert tuple(guard() for guard in guards[::-1]) == (
        scratches, versions, marking, derived)
    assert _modules(tmp_path)[0][1] is tree
    assert _module_facts(tree, text) is facts
    # Fresh references, so a mutation made during either pass shows.
    assert ast.dump(tree) == ast.dump(ast.parse(text))
    assert _facts_state(facts) == _facts_state(
        _module_facts.__wrapped__(tree, text))


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
    assert not _modules(tmp_path) and not _relevant_sources(tmp_path)
    # Admission compiles the shared AST once; nothing else touches it.
    assert counts == {"read": 1, "parse": 1, "compile": 1}


def test_version_admitted_without_sites_or_markers_skips_tokens(
        monkeypatch: pytest.MonkeyPatch) -> None:
    text = "constants.PARSER_VERSION = derived_value\n"

    def unexpected_tokens(_readline: Any) -> Any:
        raise AssertionError("Site-free, marker-free source must not tokenize")

    monkeypatch.setattr(tokenize, "generate_tokens", unexpected_tokens)
    assert version.check(text) == []


# ---- Node, scan-property, coverage-list and lane guards -----------------

_LIST = "scripts/ci/node_test_files.txt"
_WINDOW = "global." + "window"
_SKIP = ('pytest.mark.skipif(shutil.which("node") is None, '
         'reason="node not available")')
_HEADER = ("import shutil\nimport subprocess\nimport pytest\n"
           "from tests.helper_node import launch\n")
_HELPER = ("import subprocess\ndef launch():\n"
           '    return subprocess.run(("node", "-e", "1"), check=True)\n')


def _plant(root: Path, files: dict[str, str], listed: list[str]) -> None:
    for rel, text in {_LIST: "".join(f"{rel}\n" for rel in listed),
                      **files}.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")


def _failure(call: Callable[[], object]) -> str | None:
    try:
        call()
    except AssertionError as error:
        return str(error)
    return None


def _tree_guards(root: Path, monkeypatch: pytest.MonkeyPatch
                 ) -> tuple[Callable[[], Any], ...]:
    """The five converted guards, each re-rooted at a planted tree."""
    monkeypatch.setattr(scan_property, "TESTS_DIR", root / "tests")
    monkeypatch.setattr(js_toolchain, "REPO_ROOT", root)
    monkeypatch.setattr(js_lanes, "ROOT", root)
    monkeypatch.setattr(node_guard, "REPO_ROOT", root)
    lanes = js_lanes.TestNodeHarnessesLoadLanes()
    return (
        lambda: _failure(
            scan_property.test_the_gate_sees_every_string_constant_in_the_tree),
        lambda: _failure(
            js_toolchain.test_js_coverage_measured_set_matches_its_derivation),
        lambda: _failure(
            lanes.test_harnesses_driving_the_real_parser_load_lanes),
        lambda: _failure(
            node_guard.test_no_node_listed_file_sets_a_module_level_node_skip),
        lambda: _node_skip_offenders(root))


def _node_seed(name: str, uid: str) -> str:
    argv = '["' + 'node", "-e", "1"]'
    harness = (f'"""\n{_WINDOW} = {{}};\nrequire(str(LANES_JS)!r);\n'
               'require(str(PARSER_JS)!r);\n"""')
    return (f"label = {name!r}\nuid = {uid!r}\n{_HEADER}"
            f"NODE = {argv}\nHARNESS = {harness}\n"
            f"class TestNode:\n    pytestmark = {_SKIP}\n"
            "    def test_imported(self):\n        assert launch()\n"
            "    def test_direct(self):\n"
            "        subprocess.run(NODE, check=True)\n")


@pytest.mark.parametrize("reverse", [False, True])
def test_tree_guards_share_one_read_and_parse_per_file(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reverse: bool) -> None:
    uid = tmp_path.name
    texts = {"tests/test_node_a.py": _node_seed("test_node_a.py", uid),
             "tests/test_node_b.py": _node_seed("test_node_b.py", uid),
             "tests/helper_node.py": (f"label = 'helper_node.py'\n"
                                      f"uid = {uid!r}\n{_HELPER}"),
             "tests/test_pinned.py": _seed("test_pinned.py")
             + f"uid = {uid!r}\n"}
    _plant(tmp_path, texts, ["tests/test_node_a.py", "tests/test_node_b.py"])
    _stable_clock(monkeypatch)
    _wanted(monkeypatch)
    guards = _tree_guards(tmp_path, monkeypatch) + _guards(tmp_path / "tests")
    counts = _file_counts({tmp_path / rel: text for rel, text in texts.items()},
                          monkeypatch)
    if reverse:
        guards = guards[::-1]
    verdicts = tuple(guard() for guard in guards)
    if reverse:
        verdicts = verdicts[::-1]
    assert verdicts[:5] == (None, None, None, None, [])
    assert verdicts[5:7] == ({"seeded"}, ["test_pinned.py:test_server"])
    assert len(verdicts[7]) == 1 and verdicts[8] == ["test_pinned.py"]
    # The Node and lane guards reuse the shared AST without tokenizing it;
    # only the scratch/version-admitted seed is tokenized, once. The one
    # whole-tree walk is the shared node list, which _fold_operands reads.
    plain = {"read": 1, "parse": 1, "compile": 1, "index": 1, "walk": 1}
    assert counts == {"test_node_a.py": plain, "test_node_b.py": plain,
                      "helper_node.py": plain,
                      "test_pinned.py": {**plain, "tokens": 1}}


def test_one_modified_node_copy_changes_only_its_own_verdict(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seed = _node_seed("copy", tmp_path.name)
    copies = ["tests/test_copy_a.py", "tests/test_copy_b.py"]
    _plant(tmp_path, {"tests/helper_node.py": _HELPER,
                      **dict.fromkeys(copies, seed)}, copies)
    guards = _tree_guards(tmp_path, monkeypatch)
    assert guards[3]() is None and not guards[4]()
    lines = seed.count("\n")
    (tmp_path / copies[1]).write_text(
        seed + f"pytestmark = {_SKIP}\nclass TestSeeded:\n"
        f"    pytestmark = {_SKIP}\n"
        "    def test_python_only(self):\n        assert True\n",
        encoding="utf-8")
    module_mark = guards[3]()
    assert module_mark is not None
    assert f"tests/test_copy_b.py:{lines + 1}" in module_mark
    assert "test_copy_a" not in module_mark
    assert guards[4]() == [
        f"tests/test_copy_b.py:{lines + 4} (test_python_only)"]


_GROUNDED = "subprocess.run(['node'])"
_GLOBAL = 'HAS_NODE = shutil.which("node") is not None\n'
_GLOBAL_SKIP = 'pytest.mark.skipif(not HAS_NODE, reason="no runtime")'
_PY_TEST = "def test_py():\n    assert True\n"
_PY_METHOD = "    def test_py(self):\n        assert True\n"


@pytest.mark.parametrize("body, helper, module_lines, skipped", [
    pytest.param(f"pytestmark = {_SKIP}\n", True, [5], [], id="module"),
    pytest.param(f"{_GLOBAL}pytestmark = {_GLOBAL_SKIP}\n", True, [6], [],
                 id="module-global"),
    pytest.param(f"if True:\n    pytestmark = {_SKIP}\n", True, [6], [],
                 id="module-conditional"),
    pytest.param('pytest.importorskip("node")\n', True, [5], [],
                 id="module-importorskip"),
    pytest.param("pytestmark = pytest.mark.db\n", True, [], [],
                 id="module-other-mark"),
    pytest.param(f"@{_SKIP}\n{_PY_TEST}", True, [], [(6, "test_py")],
                 id="own"),
    pytest.param(f"{_GLOBAL}@{_GLOBAL_SKIP}\n{_PY_TEST}", True, [],
                 [(7, "test_py")], id="own-global"),
    pytest.param(f"@{_SKIP}\ndef test_py():\n    assert 'node' in 'node'\n",
                 True, [], [(6, "test_py")], id="own-incidental-string"),
    pytest.param(f"class TestC:\n    pytestmark = {_SKIP}\n{_PY_METHOD}",
                 True, [], [(7, "test_py")], id="class"),
    pytest.param(f"class TestBase:\n    pytestmark = {_SKIP}\n"
                 f"class TestChild(TestBase):\n{_PY_METHOD}", True, [],
                 [(8, "test_py")], id="inherited"),
    pytest.param(f"class TestC:\n    if True:\n        pytestmark = {_SKIP}\n"
                 f"{_PY_METHOD}", True, [], [(8, "test_py")],
                 id="class-conditional"),
    pytest.param(f"def _helper():\n    return {_GROUNDED}\n@{_SKIP}\n"
                 "def test_ok():\n    _helper()\n", True, [], [],
                 id="helper-grounded"),
    pytest.param(f"@pytest.fixture\ndef runner():\n    return {_GROUNDED}\n"
                 f"@{_SKIP}\ndef test_ok(runner):\n    assert runner\n", True,
                 [], [], id="fixture-grounded"),
    pytest.param(f"class TestC:\n    pytestmark = {_SKIP}\n"
                 f"    def _run(self):\n        return {_GROUNDED}\n"
                 "    def test_ok(self):\n        self._run()\n", True, [], [],
                 id="method-grounded"),
    pytest.param(f"@{_SKIP}\ndef test_ok():\n    launch()\n", True, [], [],
                 id="cross-file-grounded"),
    pytest.param(f"@{_SKIP}\ndef test_ok():\n    launch()\n", False, [],
                 [(6, "test_ok")], id="cross-file-missing-target"),
])
def test_node_guards_keep_their_mark_and_grounding_branches(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str,
        helper: bool, module_lines: list[int],
        skipped: list[tuple[int, str]]) -> None:
    rel = "tests/test_case.py"
    files = {rel: _HEADER + body}
    if helper:
        files["tests/helper_node.py"] = _HELPER
    _plant(tmp_path, files, [rel])
    guards = _tree_guards(tmp_path, monkeypatch)
    message = guards[3]()
    found = re.findall(r"tests/test_case\.py:(\d+)",
                       (message or "").split("\n", maxsplit=1)[0])
    assert [int(line) for line in found] == module_lines
    assert guards[4]() == [f"{rel}:{line} ({name})" for line, name in skipped]


@pytest.mark.parametrize("listed, planted", [
    pytest.param([], ["tests/test_x.py"], id="missing-entry"),
    pytest.param(["tests/test_x.py"], [], id="extra-entry"),
    pytest.param(["tests/test_y.py", "tests/test_x.py"],
                 ["tests/test_x.py", "tests/test_y.py"], id="unsorted"),
    pytest.param(["tests/test_x.py", "tests/test_y.py"],
                 ["tests/test_x.py", "tests/test_y.py"], id="equal"),
])
def test_coverage_list_guard_keeps_ordered_list_equality(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        listed: list[str], planted: list[str]) -> None:
    argv = '["' + 'node", "-e", "1"]'
    files = {"tests/test_x.py": "X = 1\n", "tests/test_y.py": "Y = 2\n"}
    files.update(dict.fromkeys(planted, f"NODE = {argv}\n"))
    _plant(tmp_path, files, listed)
    message = _tree_guards(tmp_path, monkeypatch)[1]()
    if listed == planted:
        assert message is None
    else:
        assert message is not None
        assert "node-executing test file was added or removed" in message


def _harness(*requires: str) -> str:
    lines = "".join(f"require({name});\n" for name in requires)
    return f'X = """\n{_WINDOW} = {{}};\n{lines}"""\n'


def test_lane_guard_keeps_both_branches_chunks_and_enumeration(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parser, rates = "str(PARSER_JS)!r", "str(RATES_JS)!r"
    lanes = "str(LANES_JS)!r"
    _plant(tmp_path, {
        "tests/test_lane_p.py": _harness(parser),
        "tests/test_lane_r.py": _harness(rates),
        "tests/test_lane_ok.py": _harness(lanes, parser) + _harness(
            rates, lanes),
        "tests/test_lane_m.py": _harness(parser) + _harness(lanes, rates)
        + _harness(rates),
        "tests/helper_lane.py": _harness(parser)}, [])
    message = _tree_guards(tmp_path, monkeypatch)[2]()
    assert message is not None
    assert message.split("\n", maxsplit=1)[0].endswith(
        "first: ['test_lane_m.py', 'test_lane_m.py', 'test_lane_p.py', "
        "'test_lane_r.py']")
    for rel in ("tests/test_lane_p.py", "tests/test_lane_r.py",
                "tests/test_lane_m.py", "tests/helper_lane.py"):
        (tmp_path / rel).unlink()
    assert _tree_guards(tmp_path, monkeypatch)[2]() is None


@pytest.mark.parametrize("body, offends", [
    pytest.param("if False:\n    DOC = '''\n    {}\n    '''\n", True,
                 id="multiline-dead-branch"),
    pytest.param("if False:\n    DOC = '{}'\n", False,
                 id="single-line-dead-branch"),
    pytest.param("DOC = '''\n    {}\n    '''\n", False, id="multiline-live"),
])
def test_scan_property_keeps_its_multiline_and_folded_branches(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str,
        offends: bool) -> None:
    _plant(tmp_path, {"tests/test_dead.py": body.format(
        "pr" + "icing.json")}, [])
    message = _tree_guards(tmp_path, monkeypatch)[0]()
    if offends:
        assert message is not None and message.startswith(
            "test_dead.py: gate misses 'pr' 'icing.json' inside a "
            "multiline constant")
    else:
        assert message is None
