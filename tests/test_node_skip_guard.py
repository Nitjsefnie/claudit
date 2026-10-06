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
test; both are checked (#759): a node skip - own mark or inherited
class mark - must ride a test that itself runs node, judged by its
transitive same-file call closure grounding in a node string constant
(a helper chain resolves; the issue's purely-syntactic trap is the
chain the closure exists to resolve). Decorators are excluded from
every closure step (a skipif names node and would otherwise trivially
ground), and a function's docstring is excluded (a Python-only test's
prose must not ground its closure). Residuals, disclosed: a helper
outside the ``tests.`` package (or a conftest fixture) resolves
nowhere and fails loud as a false offender, the deny-guard's safe
direction; a mark predicate built without a node string (a module
variable) is invisible to the scan; and the grounding scan itself is
substring-wide over the whole body, so an incidental node string in a
marked test's body (an assert message, a payload variable) grounds
that test, where a comment does not (comments are not AST). The scan
proves presence, not exclusivity.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_no_node_listed_file_sets_a_module_level_node_skip() -> None:
    """The node skip rides only the tests that run node - never the module."""
    offenders = []
    for rel in _listed_files(REPO_ROOT):
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
        target.write_text(
            (REPO_ROOT / rel).read_text(encoding="utf-8"),
            encoding="utf-8")
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


def _listed_files(root: Path) -> list[str]:
    list_file = root / "scripts" / "ci" / "node_test_files.txt"
    return sorted(
        line.strip()
        for line in list_file.read_text(encoding="utf-8").splitlines()
        if line.strip())


def _node_skip_offenders(root: Path) -> list[str]:
    """The #759 branch: file:line (test) for every node-skipped test that
    never runs node, across every listed file."""
    offenders = []
    cache: dict[str, dict] = {}
    for rel in _listed_files(root):
        tree = ast.parse((root / rel).read_text(encoding="utf-8"))
        ctx = _context_from_tree(tree, root, cache)
        for line, name in _test_offenders_in_ctx(ctx):
            offenders.append(f"{rel}:{line} ({name})")
    return offenders


def _skip_without_node_lines(
        tree: ast.Module) -> list[tuple[int, str]]:
    """The #759 branch over one parsed tree (synthetic modules, tests)."""
    return _test_offenders_in_ctx(_context_from_tree(tree, None, {}))


def _context_from_tree(tree: ast.Module, root: Path | None,
                       cache: dict) -> dict:
    """The tables the closure walks: same-file functions and classes,
    fixtures keyed by their declared name, and the tests-module names
    imported from sibling files (cache shared across the guard run, so a
    helper file parses once)."""
    module_funcs, classes = _scope_tables(tree)
    return {
        "root": root,
        "cache": cache,
        "module_funcs": module_funcs,
        "classes": classes,
        "fixtures": _fixture_table(module_funcs),
        "imported": _imported_names(tree),
    }


def _test_offenders_in_ctx(ctx: dict) -> list[tuple[int, str]]:
    """Every test carrying a node skip - its own mark or the class mark
    it inherits - whose transitive call closure never grounds in a node
    string. The closure resolves a bare call to a module-level function
    (this file or an imported tests module), a self/cls call to the
    enclosing class's methods, and a test argument to a same-file
    fixture by its declared name; decorators are excluded at every
    closure step (the test's own skipif names node and would trivially
    ground), and so is each step's docstring (prose must not ground a
    closure). A call the closure cannot resolve - a conftest fixture, a
    helper outside the tests package - grounds nothing and fails loud,
    the deny-guard's safe direction."""
    offenders: list[tuple[int, str]] = []
    for cls in ctx["classes"].values():
        methods = {s.name: s for s in cls.body
                   if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))}
        class_mark = any(
            _mentions_node(v)
            for s in cls.body
            if (v := _pytestmark_value(s)) is not None)
        for func in methods.values():
            if func.name.startswith("test_"):
                offenders += _skip_without_node(
                    func, ctx, methods, class_mark)
    for func in ctx["module_funcs"].values():
        if func.name.startswith("test_"):
            offenders += _skip_without_node(func, ctx, None, False)
    return offenders


def _scope_tables(
        tree: ast.Module,
) -> tuple[dict[str, ast.FunctionDef | ast.AsyncFunctionDef],
           dict[str, ast.ClassDef]]:
    """Module-level functions and classes, conditionals descended (the
    same hiding places the module-level check refuses)."""
    module_funcs: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    classes: dict[str, ast.ClassDef] = {}
    for stmt, _line in _module_level_statements(tree):
        if isinstance(stmt, ast.ClassDef):
            classes[stmt.name] = stmt
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            module_funcs[stmt.name] = stmt
    return module_funcs, classes


def _fixture_table(module_funcs) -> dict:
    """Same-file fixtures keyed by declared name: the function's own
    name, or the ``name=`` a ``@pytest.fixture(...)`` decoration gives."""
    out: dict = {}
    for func in module_funcs.values():
        decos = [d.func if isinstance(d, ast.Call) else d
                 for d in func.decorator_list]
        if not any(_is_bare_fixture(d) for d in decos):
            continue
        key = func.name
        for d in func.decorator_list:
            if isinstance(d, ast.Call):
                for kw in d.keywords:
                    if (kw.arg == "name"
                            and isinstance(kw.value, ast.Constant)
                            and isinstance(kw.value.value, str)):
                        key = kw.value.value
        out[key] = func
    return out


def _is_bare_fixture(deco: ast.expr) -> bool:
    return (isinstance(deco, ast.Attribute) and deco.attr == "fixture"
            and isinstance(deco.value, ast.Name)
            and deco.value.id == "pytest")


def _imported_names(tree: ast.Module) -> dict[str, tuple[str, str]]:
    """local name -> (module, original name) for ``from tests.<mod>
    import <name>`` - the corpus's one cross-file helper idiom."""
    out: dict[str, tuple[str, str]] = {}
    for stmt, _line in _module_level_statements(tree):
        if (isinstance(stmt, ast.ImportFrom) and stmt.module
                and stmt.module.startswith("tests.")):
            for alias in stmt.names:
                if alias.name != "*":
                    out[alias.asname or alias.name] = (
                        stmt.module, alias.name)
    return out


def _cross_file_target(ctx: dict, name: str):
    """(target ctx, original name) when name is imported from a sibling
    tests module that exists under root, else None."""
    hit = ctx["imported"].get(name)
    if hit is None or ctx["root"] is None:
        return None
    module, orig = hit
    rel = module.replace(".", "/") + ".py"
    target = ctx["cache"].get(rel)
    if target is None:
        path = ctx["root"] / rel
        if not path.exists():
            return None
        target = _context_from_tree(
            ast.parse(path.read_text(encoding="utf-8")), ctx["root"],
            ctx["cache"])
        ctx["cache"][rel] = target
    return target, orig


def _skip_without_node(func, ctx, class_methods,
                       class_mark: bool) -> list[tuple[int, str]]:
    own_mark = any(_mentions_node(d) for d in func.decorator_list)
    if not (own_mark or class_mark):
        return []
    if _grounds_in_node(func, ctx, class_methods, set()):
        return []
    return [(func.lineno, func.name)]


def _grounds_in_node(func, ctx, class_methods,
                     visited: set[int]) -> bool:
    if id(func) in visited:
        return False
    visited.add(id(func))
    flat = [n for stmt in _closure_stmts(func) for n in ast.walk(stmt)]
    if any(isinstance(n, ast.Constant) and isinstance(n.value, str)
           and "node" in n.value for n in flat):
        return True
    targets = _call_targets(flat, func, ctx, class_methods)
    return any(_grounds_in_node(t, tctx, tcls, visited)
               for t, tctx, tcls in targets)


def _closure_stmts(func) -> list[ast.stmt]:
    """The body walked for grounding: everything but a leading docstring
    (prose must not ground a closure)."""
    stmts = list(func.body)
    if (stmts and isinstance(stmts[0], ast.Expr)
            and isinstance(stmts[0].value, ast.Constant)
            and isinstance(stmts[0].value.value, str)):
        return stmts[1:]
    return stmts


def _call_targets(flat, func, ctx, class_methods) -> list:
    """(callee, its ctx, its class methods) for every edge the closure
    follows: fixture arguments by declared name, plus each call's
    targets."""
    targets = []
    for n in flat:
        if isinstance(n, ast.Call):
            targets += _named_targets(n.func, ctx, class_methods)
    for arg in (func.args.posonlyargs + func.args.args
                + func.args.kwonlyargs):
        same = ctx["module_funcs"].get(arg.arg) or ctx["fixtures"].get(
            arg.arg)
        if same is not None:
            targets.append((same, ctx, None))
    return targets


def _named_targets(func_expr, ctx, class_methods) -> list:
    """What one call site may dispatch to: a bare name to a module-level
    function here or in an imported tests module, a self/cls call to the
    enclosing class's methods."""
    if isinstance(func_expr, ast.Name):
        same = ctx["module_funcs"].get(func_expr.id)
        if same is not None:
            return [(same, ctx, None)]
        cross = _cross_file_target(ctx, func_expr.id)
        if cross is not None:
            target_ctx, orig = cross
            same = target_ctx["module_funcs"].get(orig)
            if same is not None:
                return [(same, target_ctx, None)]
    elif (isinstance(func_expr, ast.Attribute)
            and isinstance(func_expr.value, ast.Name)
            and func_expr.value.id in ("self", "cls")
            and class_methods is not None):
        same = class_methods.get(func_expr.attr)
        if same is not None:
            return [(same, ctx, class_methods)]
    return []


def _module_level_statements(tree: ast.Module) -> list[tuple[ast.stmt, int]]:
    """Every module-level statement, descending into module-level
    ``if``/``try``/``with``/``for`` bodies (a conditional wrapper hides
    nothing) but never into function or class bodies (a class-level
    ``pytestmark`` is the documented skip shape; whether its class's
    tests really run node is the #759 branch's own check)."""
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
