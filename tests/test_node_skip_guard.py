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

A class-level ``pytestmark`` is the documented skip shape for a class
whose every test drives node, and a per-test skipif the shape for one
test; both are checked (#759), as is a mark inherited through a
same-file base chain or a conditional class-body mark (#776): a node
skip - own, class's, inherited, or conditional - must ride a test that
itself runs node, judged by its transitive same-file call closure
LAUNCHING node through a ``subprocess`` call whose argv names node (a
helper chain resolves; the issue's purely-syntactic trap is the chain
the closure exists to resolve, and #812 the incidental node string in
the body that used to ground it). A mark predicate built from a
module-level variable that itself names node
(``HAS_NODE = shutil.which("node") is not None``) is a node skip too
(#812), wherever the mark sits. Decorators are excluded from every
closure step (a skipif names node and would otherwise trivially
ground), and a function's docstring is excluded (a Python-only test's
prose must not ground its closure). Residuals, disclosed: a helper
outside the ``tests.`` package (except comment-share measurement
calls with literal JavaScript inputs, and the report CLI) or a conftest fixture resolves
nowhere and fails loud as a false offender, the deny-guard's safe
direction; a node launched without a ``subprocess`` call (an
``os.system``, a shelled wrapper) is not seen as an invocation, nor is
one launched through an aliased or directly imported ``subprocess``
(``import subprocess as sp``, ``from subprocess import run``); a base
the walk cannot resolve (out of file, dotted, called) collects no
mark; the chain-walk ORs every same-file base, matching pytest's own
MRO-wide mark accumulation (#776). The argv scan is substring-wide over
each argv element, so a node-named element grounds the call it appears
in, where a comment does not (comments are not AST). The scan proves
presence, not exclusivity.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from tests.node_skip_guard_impl import (
    _is_module_node_importorskip,
    _listed_files,
    _mentions_node,
    _mentions_node_ref,
    _module_level_statements,
    _module_tree,
    _node_mark_lines,
    _node_names,
    _node_skip_offenders,
    _pytestmark_value,
    _skip_without_node_lines,
)
from tests.source_scan_cache import source as cached_source

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_no_node_listed_file_sets_a_module_level_node_skip() -> None:
    """The node skip rides only the tests that run node - never the module."""
    offenders = []
    for rel in _listed_files(REPO_ROOT):
        tree = _module_tree(REPO_ROOT / rel)
        node_names = _node_names(tree)
        for stmt, line in _module_level_statements(tree):
            value = _pytestmark_value(stmt)
            if value is not None and _mentions_node_ref(value, node_names):
                offenders.append(f"{rel}:{line}")
            elif _is_module_node_importorskip(stmt):
                offenders.append(f"{rel}:{line}")
    assert not offenders, (
        "module-level node skip(s) found; the skip must ride only the "
        "tests that run node (issues #743, #746): " + ", ".join(offenders))


def test_every_node_skip_rides_a_test_that_runs_node() -> None:
    """A node skip - a test's own mark or the class mark it inherits -
    rides only a test that itself runs node, wherever it sits (#759)."""
    offenders = _node_skip_offenders(REPO_ROOT)
    assert not offenders, (
        "node skip(s) on tests that never run node (issue #759): "
        + ", ".join(offenders))


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


_MARKED_CLASS = (
    "import shutil\nimport pytest\n"
    "def _node(body):\n"
    "    return subprocess.run(['node'], input=body)\n"
    "class TestNodeDriven:\n"
    "    pytestmark = pytest.mark.skipif(\n"
    '        shutil.which("node") is None,\n'
    '        reason="node not available")\n')


def test_a_python_only_test_in_a_node_marked_class_is_detected() -> None:
    """Shape one (#759): a test in a node-marked class that never runs
    node inherits the class mark and skips node-less, where the old
    module-level-only guard could not see it."""
    mod = ast.parse(_MARKED_CLASS + (
        "    def test_drives_node(self):\n"
        "        assert _node('1+1')\n"
        "    def test_python_only_member(self):\n"
        "        assert sum([1, 2]) == 3\n"))
    assert _skip_without_node_lines(mod) == [
        (11, "test_python_only_member")]


def test_a_per_test_node_skip_on_a_python_only_test_is_detected() -> None:
    """Shape two (#759): a per-test node skipif riding a test that never
    runs node skips it node-less too - one rule, wherever the skip sits."""
    mod = ast.parse(
        "import shutil\nimport pytest\n"
        "@pytest.mark.skipif(shutil.which('node') is None,\n"
        "                    reason='node not available')\n"
        "def test_python_only_with_node_mark():\n"
        "    assert sum([1, 2]) == 3\n")
    assert _skip_without_node_lines(mod) == [
        (5, "test_python_only_with_node_mark")]


def test_node_parameter_data_and_other_decorators_are_not_skips() -> None:
    for decorator in ("pytest.mark.parametrize('text', ['#!/usr/bin/env node'])",
                      "pytest.mark.example('node')", "custom('node')"):
        tree = ast.parse(f"@{decorator}\ndef test_python():\n    assert 1 + 1 == 2\n")
        assert not _skip_without_node_lines(tree)


def test_comment_measurement_entry_points_ground_real_node_skips() -> None:
    for imports, body in (
        ("from scripts.ci import comment_share as counter\n", "counter.measure_files(Path('.'), {'a.js': 'javascript'})"),
        ("import scripts.ci.comment_share as counter\n", "counter.measure_files(Path('.'), {'a.js': 'javascript'})"),
        ("from scripts.ci.comment_share import measure_files as measure\n", "measure(Path('.'), {'a.js': 'javascript'})"),
        ("from scripts.ci import comment_share as counter\n", "counter.javascript_comments(Path('.'), ['a.mjs'])"),
        ("from scripts.ci.comment_share import javascript_comments as measure\n", "measure(root=Path('.'), paths=('a.cjs',))"),
        ("from scripts.ci import comment_share as counter\n", "counter.measure_files(sources={'a.jsx': 'javascript'})"),
        ("import subprocess\n", "subprocess.run(['python3', 'scripts/ci/comment_share.py', '--report'])"),
        ("import subprocess\n", "subprocess.run([sys.executable, ROOT / 'scripts/ci/comment_share.py', '--report'])"),
        ("import subprocess\n", "subprocess.run(['python3', 'scripts/ci/comment_share.py', '--report', '--root', '.'])"),
        ("import subprocess\n", "subprocess.run(['python3', 'scripts/ci/comment_share.py', '--root', '.', '--report'])"),
    ):
        tree = ast.parse(imports + "@pytest.mark.skipif(shutil.which('node') is None)\n"
                         + f"def test_measure():\n    {body}\n")
        assert not _skip_without_node_lines(tree)


@pytest.mark.parametrize('body', [
    "counter.python_comments('x = 1')",
    "counter.main(['--help'])",
    "counter.javascript_comments(Path('.'), [])",
    "counter.measure_files(Path('.'), {})",
    "counter.measure_files()",
    "counter.javascript_comments(Path('.'), paths)",
    "counter.measure_files(Path('.'), {'a.py': 'python'})",
    "counter.measure_files(Path('.'), {'a.js': 'python'})",
    "counter.measure_files(Path('.'), sources)",
    "counter.measure_files(Path('.'), sources=None)",
    "counter.javascript_comments(Path('.'), ['a.js'], **options)",
    "subprocess.run(['echo', 'scripts/ci/comment_share.py'])",
    "subprocess.run(['python3', 'other.py', 'scripts/ci/comment_share.py'])",
    "subprocess.run(['python3', 'scripts/ci/comment_share.py', '--help'])",
    "subprocess.run(['python3', 'scripts/ci/comment_share.py', '-h'])",
    "subprocess.run(['python3', 'other-scripts/ci/comment_share.py', '--report'])",
    "subprocess.run(['python3', OTHER / 'scripts/ci/comment_share.py', '--report'])",
    "subprocess.run(['python3', 'scripts/ci/comment_share.py'])",
    "subprocess.run(['python3', 'scripts/ci/comment_share.py', '--report', '--help'])",
    "subprocess.run(['python3', 'scripts/ci/comment_share.py', 'src/parser.js'])",
    "subprocess.run(['python3', 'scripts/ci/comment_share.py', '--report', 'src/parser.js'])"
])
def test_measurement_names_without_an_entry_point_do_not_ground_skips(body) -> None:
    tree = ast.parse("from scripts.ci import comment_share as counter\nimport subprocess\n"
                     "@pytest.mark.skipif(shutil.which('node') is None)\n"
                     + f"def test_python():\n    {body}\n")
    assert _skip_without_node_lines(tree) == [(4, 'test_python')]


def test_a_node_running_class_member_is_left_alone() -> None:
    """The control for shape one: a class whose every member grounds
    through the helper keeps its class mark."""
    mod = ast.parse(_MARKED_CLASS + (
        "    def test_drives_node(self):\n"
        "        assert _node('1+1')\n"
        "    def test_also_drives_node(self):\n"
        "        assert _node('2+2')\n"))
    assert not _skip_without_node_lines(mod)


def test_an_unmarked_python_only_test_is_left_alone() -> None:
    """No node skip, no opinion: an unmarked test that runs node-less
    fails loud on its own if it needed node."""
    mod = ast.parse(
        "def test_python_only():\n"
        "    assert sum([1, 2]) == 3\n")
    assert not _skip_without_node_lines(mod)


def test_the_closure_resolves_a_helper_chain() -> None:
    """The issue's named trap: a test calling a module-level helper that
    calls node mentions node nowhere in its own body; the closure
    resolves the chain to the same grounding a direct call gives."""
    mod = ast.parse(
        "import shutil\nimport pytest\n"
        "def _run(body):\n"
        "    return _node(body)\n"
        "def _node(body):\n"
        "    return subprocess.run(['node'], input=body)\n"
        "@pytest.mark.skipif(shutil.which('node') is None,\n"
        "                    reason='node not available')\n"
        "def test_via_chain():\n"
        "    assert _run('1+1')\n")
    assert not _skip_without_node_lines(mod)


def test_a_renamed_fixture_grounds_its_users() -> None:
    """A test argument resolves to a same-file fixture by its DECLARED
    name: @pytest.fixture(name="js") def _js_fixture() runs node, and
    test_x(js) grounds through it."""
    mod = ast.parse(
        "import shutil\nimport pytest\n"
        "def _node(body):\n"
        "    return subprocess.run(['node'], input=body)\n"
        '@pytest.fixture(name="geometry")\n'
        "def _geometry_fixture():\n"
        "    return _node('1+1')\n"
        "@pytest.mark.skipif(shutil.which('node') is None,\n"
        "                    reason='node not available')\n"
        "def test_uses_fixture(geometry):\n"
        "    assert geometry\n")
    assert not _skip_without_node_lines(mod)


def test_a_python_only_member_is_not_saved_by_its_docstring() -> None:
    """The docstring exclusion has teeth: a marked member whose ONLY node
    mention is its leading docstring is still flagged - prose grounds
    nothing."""
    mod = ast.parse(_MARKED_CLASS + (
        "    def test_docstring_mentions_node(self):\n"
        "        \"\"\"Drives the node subprocess.\"\"\"\n"
        "        assert sum([1, 2]) == 3\n"))
    assert _skip_without_node_lines(mod) == [
        (9, "test_docstring_mentions_node")]


def test_a_node_string_in_a_python_only_member_does_not_ground() -> None:
    """Shape one of #812: a marked member whose only node mention is an
    incidental string (a path, an assert message) never runs node, so it
    is flagged - a string constant no longer grounds a closure."""
    mod = ast.parse(_MARKED_CLASS + (
        "    def test_pins_node_modules(self):\n"
        "        assert \"node_modules/picker.js\" in SRC\n"))
    assert _skip_without_node_lines(mod) == [(9, "test_pins_node_modules")]


def test_a_locally_bound_node_command_still_grounds() -> None:
    """The #812 control: a member that binds the node path and launches it
    through subprocess still grounds (the picker-fit shape)."""
    mod = ast.parse(_MARKED_CLASS + (
        "    def test_binds_then_runs(self):\n"
        "        node = shutil.which(\"node\")\n"
        "        assert subprocess.run([node], input='1+1')\n"))
    assert not _skip_without_node_lines(mod)


def test_a_mark_predicated_on_a_module_node_variable_is_detected() -> None:
    """Shape two of #812: a node skip spelled through a module-level
    availability variable, with a reason that does not name node, is
    still a node skip on a test that never runs node."""
    mod = ast.parse(
        "import shutil\nimport pytest\n"
        "HAS_NODE = shutil.which(\"node\") is not None\n"
        "@pytest.mark.skipif(not HAS_NODE, reason=\"runtime unavailable\")\n"
        "def test_python_only():\n"
        "    assert sum([1, 2]) == 3\n")
    assert _skip_without_node_lines(mod) == [(5, "test_python_only")]


def test_a_module_mark_predicated_on_a_module_node_variable_is_detected(
) -> None:
    """The module-level spelling of shape two (#812)."""
    mod = ast.parse(
        "import shutil\nimport pytest\n"
        "HAS_NODE = shutil.which(\"node\") is not None\n"
        "pytestmark = pytest.mark.skipif(not HAS_NODE, reason=\"no runtime\")\n")
    assert len(_node_mark_lines(mod)) == 1


def test_a_mark_on_a_non_node_module_variable_is_left_alone() -> None:
    """The isolating negative for shape two: a module variable that names
    no node leaves the mark a non-node skip."""
    mod = ast.parse(
        "import pytest\n"
        "HAS_DB = True\n"
        "pytestmark = pytest.mark.skipif(not HAS_DB, reason=\"no database\")\n")
    assert not _node_mark_lines(mod)


def test_a_self_call_to_a_node_method_grounds() -> None:
    """The self/cls edge: a marked member grounding only through a
    self-call to a same-class node method is not flagged."""
    mod = ast.parse(_MARKED_CLASS + (
        "    def _run_node(self, body):\n"
        "        return _node(body)\n"
        "\n"
        "    def test_via_self_call(self):\n"
        "        assert self._run_node('1+1')\n"))
    assert not _skip_without_node_lines(mod)


def test_a_self_call_to_a_plain_method_does_not_ground() -> None:
    """The isolating negative for the self/cls edge: the same call shape
    against a method with no node grounding is flagged."""
    mod = ast.parse(_MARKED_CLASS + (
        "    def _plain(self, body):\n"
        "        return body\n"
        "\n"
        "    def test_via_self_call(self):\n"
        "        assert self._plain('1+1')\n"))
    assert _skip_without_node_lines(mod) == [
        (12, "test_via_self_call")]


_INHERITED_MARK = ("import shutil\nimport pytest\n"
                   "class TestNodeBase:\n    pytestmark = pytest.mark.skipif(\n"
                   '        shutil.which("node") is None,\n'
                   '        reason="node not available")\n')


def test_an_inherited_node_mark_reaches_the_derived_class() -> None:
    """Shape three (#776): the mark rides the MRO - a base-class mark skips a derived Python-only member."""
    direct = ast.parse(_INHERITED_MARK + "class TestNodeChild(TestNodeBase):\n    def test_python_only_child(self):\n        assert sum([1, 2]) == 3\n")
    chain = ast.parse(_INHERITED_MARK + "class TestMid(TestNodeBase):\n    pass\nclass TestLeaf(TestMid):\n    def test_python_only_leaf(self):\n        assert sum([1, 2]) == 3\n")
    assert _skip_without_node_lines(direct) == [(8, "test_python_only_child")]
    assert _skip_without_node_lines(chain) == [(10, "test_python_only_leaf")]


def test_an_inherited_mark_member_that_runs_node_is_left_alone() -> None:
    """The inherited shape's control: grounding holds through the MRO too."""
    mod = ast.parse(_INHERITED_MARK + "def _node(body):\n    return subprocess.run(['node'], input=body)\nclass TestNodeChild(TestNodeBase):\n    def test_drives_node(self):\n        assert _node('1+1')\n")
    assert not _skip_without_node_lines(mod)


def test_a_base_mark_the_guard_cannot_see_leaves_the_child_alone() -> None:
    """The inherited shape's disclosed fail-open and its non-node sibling: a
    base outside the file resolves to no mark; a non-node mark has none to inherit."""
    unknown = ast.parse("class TestChild(TestExternal):\n    def test_python_only(self):\n        assert sum([1, 2]) == 3\n")
    plain = ast.parse("import pytest\nclass TestBase:\n    pytestmark = pytest.mark.db\nclass TestChild(TestBase):\n    def test_python_only(self):\n        assert sum([1, 2]) == 3\n")
    assert not _skip_without_node_lines(unknown)
    assert not _skip_without_node_lines(plain)


def test_marks_nested_in_class_body_conditionals_are_detected() -> None:
    """Shape four (#776): if/try at class level hide a pytestmark no better
    than module-level ones - both wrapped classes get checked."""
    mod = ast.parse(
        "import shutil\nimport pytest\nclass TestIfWrap:\n    if True:\n        pytestmark = pytest.mark.skipif(\n"
        '            shutil.which("node") is None,\n            reason="node not available")\n'
        "    def test_python_only_if(self):\n        assert sum([1, 2]) == 3\n"
        "class TestTryWrap:\n    try:\n        pytestmark = pytest.mark.skipif(\n"
        '            shutil.which("node") is None,\n            reason="node not available")\n'
        "    except NameError:\n        pytestmark = None\n"
        "    def test_python_only_try(self):\n        assert sum([1, 2]) == 3\n")
    assert _skip_without_node_lines(mod) == [(8, "test_python_only_if"), (17, "test_python_only_try")]


def test_the_mro_walk_descends_the_base_s_own_conditionals() -> None:
    """The MRO walk descends the base's own conditionals too; a conditional
    mark that never mentions node checks nothing."""
    base = ("import shutil\nimport pytest\nclass TestBase:\n    if True:\n"
            "        pytestmark = pytest.mark.skipif(\n"
            '            shutil.which("node") is None,\n            reason="node not available")\n')
    inherited = ast.parse(base + "class TestChild(TestBase):\n    def test_python_only(self):\n        assert sum([1, 2]) == 3\n")
    plain = ast.parse("import pytest\nclass TestBase:\n    if True:\n        pytestmark = pytest.mark.db\n    def test_python_only(self):\n        assert sum([1, 2]) == 3\n")
    assert _skip_without_node_lines(inherited) == [(9, "test_python_only")]
    assert not _skip_without_node_lines(plain)


def test_an_imported_helper_grounds_across_files(tmp_path) -> None:
    """A helper imported from a sibling tests module grounds the same as
    a local one (test_parser_js_dedup_merge's shape)."""
    (tmp_path / "scripts" / "ci").mkdir(parents=True)
    (tmp_path / "scripts" / "ci" / "node_test_files.txt").write_text(
        "tests/test_user.py\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "tests/test_helpers.py").write_text(
        "def _node_helper():\n"
        "    return subprocess.run(['node'])\n", encoding="utf-8")
    (tmp_path / "tests/test_user.py").write_text(
        "import shutil\nimport pytest\n"
        "from tests.test_helpers import _node_helper\n"
        "@pytest.mark.skipif(shutil.which('node') is None,\n"
        "                    reason='node not available')\n"
        "def test_via_import():\n"
        "    assert _node_helper()\n",
        encoding="utf-8")
    assert not _node_skip_offenders(tmp_path)


def test_the_full_guard_fails_a_seeded_python_only_member(tmp_path) -> None:
    """End to end, on copies of every listed file: the clean tree is
    green, and seeding one Python-only test into a node-marked class in
    a disposable copy of a real listed file fails the guard naming it."""
    list_rel = "scripts/ci/node_test_files.txt"
    (tmp_path / list_rel).parent.mkdir(parents=True)
    (tmp_path / list_rel).write_text(
        (REPO_ROOT / list_rel).read_text(encoding="utf-8"),
        encoding="utf-8")
    for rel in _listed_files(REPO_ROOT):
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(cached_source(REPO_ROOT / rel), encoding="utf-8")
    assert not _node_skip_offenders(tmp_path)
    victim_rel = "tests/test_parser_js_mirror.py"
    victim = tmp_path / victim_rel
    victim.write_text(victim.read_text(encoding="utf-8") + (
        "\n"
        "class TestSeededPythonOnly:\n"
        "    pytestmark = pytest.mark.skipif(\n"
        '        shutil.which("node") is None,\n'
        '        reason="node not available")\n'
        "\n"
        "    def test_seeded_python_only(self):\n"
        "        assert sum([1, 2]) == 3\n"),
        encoding="utf-8")
    offenders = _node_skip_offenders(tmp_path)
    assert len(offenders) == 1
    assert victim_rel in offenders[0]
    assert "test_seeded_python_only" in offenders[0]


@pytest.mark.parametrize('imports, mark', [
    ("from pytest import mark\n", "mark.skipif"),
    ("from pytest import mark as pm\n", "pm.skipif"),
    ("import pytest as pt\n", "pt.mark.skipif"),
    ("import pytest as pt\nm = pt.mark\n", "m.skipif"),
    ("from pytest import mark\nnode_skip = mark.skipif\n", "node_skip"),
    ("import pytest\nfirst = pytest.mark\nsecond = first\n", "second.skip"),
    ("", "unknown.skipif"),
])
def test_aliased_node_skip_marks_refuse_python_only_and_accept_node(imports, mark) -> None:
    source = imports + f"@{mark}(shutil.which('node') is None, reason='node absent')\n"
    source += "def test_subject():\n    "
    tree = ast.parse(source + "assert 1 + 1 == 2\n")
    assert _skip_without_node_lines(tree) == [(len(imports.splitlines()) + 2, 'test_subject')]
    tree = ast.parse(source + "subprocess.run(['node', '-e', '1+1'])\n")
    assert not _skip_without_node_lines(tree)
