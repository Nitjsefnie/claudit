"""The node-skip guard's AST machinery (tests/test_node_skip_guard.py).

Split out of the test module to stay under the tests/ size ceiling
(scripts/ci/size_baseline.py); the tests import the entry points from
here. A node skip rides a test that itself runs node, judged by its
transitive same-file call closure LAUNCHING node through a subprocess
call - never by an incidental string constant in the body (#812) - and
a mark predicate built from a module-level variable that names node is
a node skip too (#812). The comment-share measurement function and Python
CLI are trusted node entry points; parameter data is never a skip mark.
"""
from __future__ import annotations

import ast
from pathlib import Path

from tests.source_scan_cache import for_source, source as cached_source


def _listed_files(root: Path) -> list[str]:
    list_file = root / "scripts" / "ci" / "node_test_files.txt"
    return sorted(
        line.strip()
        for line in list_file.read_text(encoding="utf-8").splitlines()
        if line.strip())


def _module_tree(path: Path) -> ast.Module:
    """The shared exec-mode AST of a UTF-8 source file."""
    tree = for_source(cached_source(path)).tree()
    assert isinstance(tree, ast.Module)
    return tree


def _node_skip_offenders(root: Path) -> list[str]:
    """The #759 branch: file:line (test) for every node-skipped test that
    never runs node, across every listed file."""
    offenders = []
    cache: dict[str, dict] = {}
    for rel in _listed_files(root):
        ctx = _context_from_tree(_module_tree(root / rel), root, cache)
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
        "node_names": _node_names(tree),
        "measurement_imports": _measurement_imports(tree),
    }


def _test_offenders_in_ctx(ctx: dict) -> list[tuple[int, str]]:
    """Every test carrying a node skip - its own mark or the class mark
    it inherits - whose transitive call closure never launches node. The
    closure resolves a bare call to a module-level function
    (this file or an imported tests module), a self/cls call to the
    enclosing class's methods, and a test argument to a same-file
    fixture by its declared name; decorators are excluded at every
    closure step (a skipif decorator is not part of the body the
    closure walks), and so is each step's docstring (prose must not
    ground a closure). A call the closure cannot resolve - a conftest
    fixture, a helper outside the tests package apart from the known
    comment-share measurement entry points - grounds nothing and
    fails loud, the deny-guard's safe direction."""
    offenders: list[tuple[int, str]] = []
    for cls in ctx["classes"].values():
        methods = {s.name: s for s in cls.body
                   if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))}
        class_mark = _class_mark_mentions_node(cls, ctx)
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
            _module_tree(path), ctx["root"], ctx["cache"])
        ctx["cache"][rel] = target
    return target, orig


def _skip_without_node(func, ctx, class_methods,
                       class_mark: bool) -> list[tuple[int, str]]:
    own_mark = any(_skip_mark_mentions_node(d, ctx["node_names"])
                   for d in func.decorator_list)
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
    if _launches_node(flat) or _measurement_launch(flat, ctx["measurement_imports"]):
        return True
    targets = _call_targets(flat, func, ctx, class_methods)
    return any(_grounds_in_node(t, tctx, tcls, visited)
               for t, tctx, tcls in targets)


_SUBPROCESS_LAUNCHERS = frozenset(
    {"run", "Popen", "call", "check_call", "check_output"})


def _launches_node(nodes: list[ast.AST]) -> bool:
    """True when the nodes launch node: a ``subprocess``-family call whose
    argv names node. The INVOCATION grounds a marked test, never an
    incidental node string in its body (issue #812)."""
    return any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "subprocess"
        and n.func.attr in _SUBPROCESS_LAUNCHERS
        and n.args and _argv_names_node(n.args[0])
        for n in nodes)


def _argv_names_node(arg: ast.expr) -> bool:
    """The launch's command names node: a list/tuple with a node element,
    or a single node-named expression (``[node]`` off a local binding)."""
    elts = arg.elts if isinstance(arg, (ast.List, ast.Tuple)) else [arg]
    return any(_names_node(e) for e in elts)


def _names_node(expr: ast.expr) -> bool:
    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
        return "node" in expr.value
    if isinstance(expr, ast.Name):
        return "node" in expr.id.lower()
    return False


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


def _class_mark_mentions_node(cls: ast.ClassDef, ctx: dict) -> bool:
    """cls's pytestmark or a same-file base's, conditionals descended (#776)."""
    seen: set[int] = set()
    stack = [cls]
    while stack:
        c = stack.pop()
        if id(c) in seen:
            continue
        seen.add(id(c))
        if any(_skip_mark_mentions_node(v, ctx["node_names"])
               for s in _conditionals_descended(c.body)
               if (v := _pytestmark_value(s)) is not None):
            return True
        stack += [ctx["classes"][b.id] for b in c.bases if isinstance(b, ast.Name) and b.id in ctx["classes"]]
    return False


def _conditionals_descended(stmts: list[ast.stmt]) -> list[ast.stmt]:
    """``stmts`` with ``if``/``try``/``with``/``for``/``while`` bodies and else/finally/handler arms descended."""
    out: list[ast.stmt] = []
    stack = list(stmts)
    while stack:
        stmt = stack.pop(0)
        out.append(stmt)
        if not isinstance(stmt, (ast.If, ast.Try, ast.With, ast.AsyncWith, ast.For, ast.AsyncFor, ast.While)):
            continue
        kids = (list(getattr(stmt, "body", [])) + list(getattr(stmt, "orelse", []))
                + list(getattr(stmt, "finalbody", [])) + list(getattr(stmt, "handlers", [])))
        stack += [s for k in kids for s in (k.body if isinstance(k, ast.ExceptHandler) else [k])]
    return out


def _module_level_statements(tree: ast.Module) -> list[tuple[ast.stmt, int]]:
    """Every module-level statement, conditionals descended (see _conditionals_descended)."""
    return [(s, s.lineno) for s in _conditionals_descended(tree.body)]


def _node_mark_lines(tree: ast.Module) -> list[int]:
    node_names = _node_names(tree)
    lines = []
    for stmt, line in _module_level_statements(tree):
        value = _pytestmark_value(stmt)
        if (value is not None and _mentions_node_ref(value, node_names)) or (
                _is_module_node_importorskip(stmt)):
            lines.append(line)
    return lines


def _node_names(tree: ast.Module) -> dict[str, ast.expr]:
    """Module-level names bound to a value that names node - the
    ``HAS_NODE = shutil.which("node") is not None`` spelling a mark
    predicate may be built from (issue #812)."""
    out: dict[str, ast.expr] = {}
    for stmt, _line in _module_level_statements(tree):
        if not isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            continue
        if stmt.value is None or not _mentions_node(stmt.value):
            continue
        targets = (stmt.targets if isinstance(stmt, ast.Assign)
                   else [stmt.target])
        for target in targets:
            if isinstance(target, ast.Name):
                out[target.id] = stmt.value
    return out


def _mentions_node_ref(value: ast.expr, node_names: dict) -> bool:
    """A mark names node when its own text does OR it references a
    module-level name that names node (#812)."""
    return _mentions_node(value) or any(
        isinstance(n, ast.Name) and n.id in node_names
        for n in ast.walk(value))


def _skip_mark_mentions_node(value: ast.expr, names: dict) -> bool:
    if isinstance(value, (ast.List, ast.Tuple)):
        return any(_skip_mark_mentions_node(item, names) for item in value.elts)
    if isinstance(value, ast.Name) and value.id in names:
        return _skip_mark_mentions_node(names[value.id], {})
    return (isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute)
            and ast.unparse(value.func) in ('pytest.mark.skip', 'pytest.mark.skipif')
            and _mentions_node_ref(value, names))


def _measurement_imports(tree: ast.Module) -> dict[str, str]:
    imported = {}
    for stmt, _line in _module_level_statements(tree):
        if isinstance(stmt, ast.ImportFrom) and stmt.module in ('scripts.ci', 'scripts.ci.comment_share'):
            for alias in stmt.names:
                imported[alias.asname or alias.name] = f'{stmt.module}.{alias.name}'
        elif isinstance(stmt, ast.Import):
            for alias in stmt.names:
                if alias.name == 'scripts.ci.comment_share':
                    imported[alias.asname or alias.name] = alias.name
    return imported


def _measurement_launch(nodes: list[ast.AST], imported: dict[str, str]) -> bool:
    for node in nodes:
        if not isinstance(node, ast.Call):
            continue
        name = ast.unparse(node.func)
        for local, qualified in imported.items():
            target = qualified + name[len(local):] if name == local or name.startswith(local + '.') else ''
            if target in ('scripts.ci.comment_share.measure_files',
                          'scripts.ci.comment_share.javascript_comments', 'scripts.ci.comment_share.main'):
                return True
        if _measurement_cli(node):
            return True
    return False


def _measurement_cli(node: ast.Call) -> bool:
    if not (isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
            and node.func.value.id == 'subprocess' and node.func.attr in _SUBPROCESS_LAUNCHERS):
        return False
    if not node.args or not isinstance(node.args[0], (ast.List, ast.Tuple)):
        return False
    args = node.args[0].elts
    if len(args) < 2:
        return False
    executable = ast.unparse(args[0])
    if executable != 'sys.executable' and not (
            isinstance(args[0], ast.Constant) and isinstance(args[0].value, str)
            and Path(args[0].value).name.startswith('python')):
        return False
    return any(isinstance(part, ast.Constant) and isinstance(part.value, str)
               and part.value.endswith('scripts/ci/comment_share.py') for part in ast.walk(args[1]))


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
