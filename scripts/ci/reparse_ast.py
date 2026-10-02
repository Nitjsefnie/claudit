#!/usr/bin/env python3
"""Which functions a reparse pass can reach, read off the source AST.

The reparse bench (``reparse_bench.py``) measures the CPU of one pass over
the committed fixture mirror. Its corpus is the instrument's whole world:
a regression in a parse path no fixture reaches moves no number in either
direction. ``fixtures/r2_mini`` held only Claude-layout transcripts, so
every Codex and Kimi path, the lane key layout and the lane role mapping
were invisible to the gate (issue #503). This module is the first half of
that fix — it names what the pass CAN reach, so the second half
(``reparse_surface``) can insist the corpus DID.

- THE SURFACE IS AN AST WALK, NOT A LIST. Roots are ``parse.parse_file``,
  ``parse_lanes.sniff_format`` and ``agent_sidecar.apply_agent_sidecar`` —
  the three callables the pass is made of — and the walk follows every
  call it can resolve, transitively, into ``PARSE_SURFACE_MODULES``.
  Hand-maintaining that list is what let the gap open in the first place.

- A FUNCTION IS THE UNIT, not a line. A function the corpus reaches is
  covered in whatever proportion its own lines are; one it never reaches is
  covered at zero, and zero is the whole claim the gate makes. A nested
  ``def`` belongs to the function it is written inside, so an unexercised
  nested helper cannot fail on its own.

- FAIL CLOSED. An unreadable module, a root the walk cannot find, or a
  call expression it cannot resolve raises rather than returning a short
  list: a surface with a hole in it is a surface that passes.

- WHAT THE WALK STILL DOES NOT SEE, said here rather than implied. A
  method call on a receiver the module did not import — a foreign
  object's, or a local instance reached through something other than
  ``self`` — is not resolved, because the answer is a type question this
  walk does not ask. Adding that would mean resolving types; until then
  this sentence is the bound.

- EVERY OTHER FORM IS REFUSED, not dropped. ``reparse_bindings.unresolved``
  names a callee the reader cannot resolve and the walk raises rather than
  returning a shorter surface: an unbound bare name, a name that is a
  function PARAMETER (the target is whatever the caller passed), a call of
  a call's result, and a subscript whose base is neither a Name nor an
  Attribute. Those four are the forms a reader can recognise as
  unresolvable. The receiver-shape form above is the one it cannot, so it
  is written down here instead — a hole the walk knows it has is a bound,
  and one it does not know about is the failure mode this gate exists to
  prevent.
"""
from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path
from typing import TypeGuard

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
# pylint: disable-next=wrong-import-position
from reparse_bindings import unresolved  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The modules the walk may enter. Anything reachable from a root OUTSIDE
# this set is outside the surface by construction: the walk stops at the
# boundary rather than guessing past it, so the gate's scope is one list a
# reviewer can read rather than the whole backend.
#
# The bash_* and target_paths modules are here because the Claude path
# reaches them (the mirror's Bash tool_use does); what the walk then finds
# reachable in them is what the corpus has to exercise. pricing,
# turn_flags, tool_errors, prompt_gate and json_shape are deliberately NOT
# here: they are helpers the pass calls, not parse paths, and admitting
# them would make the surface the backend rather than the parse.
PARSE_SURFACE_MODULES = (
    'agent_sidecar',
    'bash_argv',
    'bash_churn',
    'bash_dash_c',
    'bash_literals',
    'bash_loops',
    'bash_reads',
    'kimi_content',
    'key_layout',
    'parse',
    'parse_codex',
    'parse_common',
    'parse_kimi',
    'parse_lanes',
    'target_paths',
)

# The pass is made of these three callables (reparse_phases' partition
# names them), so they are where reachability starts. Each is named in the
# module that DEFINES it, not the one that re-exports it: parse.py imports
# sniff_format from parse_lanes, and the code object the pass executes is
# the defining module's either way.
ROOTS = (
    ('parse', 'parse_file'),
    ('parse_lanes', 'sniff_format'),
    ('agent_sidecar', 'apply_agent_sidecar'),
)


def _qualname(prefix: str, name: str) -> str:
    return f'{prefix}.{name}' if prefix else name


def _is_def(node: ast.AST) -> TypeGuard[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Whether `node` is a function definition.

    A TypeGuard so a caller that has just asked reaches a NARROWED node:
    `ast.AST` carries no `.name`, and every use here is a definition's.
    """
    return isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))


# --- per-module facts, each a pure read of one file's AST --------------------

def _trees(root: Path) -> dict:
    """Every surface module's parsed AST. A missing or unreadable module
    raises here: a surface with a hole in it is a surface that passes."""
    if not PARSE_SURFACE_MODULES:
        raise ValueError('the surface module list is empty')
    trees = {}
    for name in PARSE_SURFACE_MODULES:
        path = root / 'backend' / f'{name}.py'
        if not path.exists():
            raise ValueError(f'the surface module is missing: {path}')
        trees[name] = ast.parse(path.read_text(encoding='utf-8'),
                                filename=str(path))
    return trees


def _defines(tree: ast.Module) -> set:
    """The top-level names this module defines: its functions, its classes,
    and `Class.method` for each method.

    A nested `def` is not listed, because it runs or does not with the
    function it is written inside. A class IS listed: instantiating one
    reaches its whole body, so a class the pass never instantiates is a
    parse path the corpus never exercises.
    """
    names = set()
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            names.add(node.name)
            names.update(_qualname(node.name, item.name)
                         for item in node.body if _is_def(item))
        elif _is_def(node):
            names.add(node.name)
    return names


def _methods(tree: ast.Module) -> dict:
    """Class name -> the qualnames of its methods."""
    return {node.name: [_qualname(node.name, item.name)
                        for item in node.body if _is_def(item)]
            for node in tree.body if isinstance(node, ast.ClassDef)}


def _module_aliases(tree: ast.Module) -> dict:
    """Local name -> surface module, for the imports that bind a MODULE."""
    aliases = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == 'backend':
            for alias in node.names:
                if alias.name in PARSE_SURFACE_MODULES:
                    aliases[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith('backend.'):
                    target = alias.name.split('.', 1)[1]
                    if target in PARSE_SURFACE_MODULES:
                        aliases[alias.asname or alias.name] = target
    return aliases


def _reference(node: ast.AST, aliases: dict):
    """(module, qualname) for a bare `mod.func` reference, else None."""
    if not isinstance(node, ast.Attribute):
        return None
    if not isinstance(node.value, ast.Name):
        return None
    target = aliases.get(node.value.id)
    return None if target is None else (target, node.attr)


def _item_reference(item: ast.AST, aliases: dict, module: str,
                    defined: set):
    """(module, qualname) for one entry of a function table.

    A table holds its entries two ways: `other.mod.func` (a cross-module
    reference, resolved through the module aliases) and a bare `func` (this
    module's own). Reading only the first makes a same-module table look
    like no table at all, and the call it routes silently vanishes.
    """
    if isinstance(item, ast.Name) and item.id in defined:
        return (module, item.id)
    return _reference(item, aliases)


def _containers(tree: ast.Module, aliases: dict, module: str,
                defined: dict) -> dict:
    """Local name -> the function references it holds, for a container of
    them.

    ``parse.LANE_PARSERS[fmt](...)`` is a real call into all three lane
    parsers that no attribute expression names, so its edges have to come
    from the constant's own definition.

    The whole tree is scanned, not only the module body, so a table built
    INSIDE a function is read too. That over-approximates — two functions
    each with a `TABLES` contribute both tables under one name — and the
    direction is the safe one for a coverage gate: it can add a function
    to the surface, never drop one.
    """
    found = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        if not isinstance(value, (ast.Dict, ast.List, ast.Tuple, ast.Set)):
            continue
        items = (list(value.values) if isinstance(value, ast.Dict)
                 else list(value.elts))
        refs = {ref for item in items
                if (ref := _item_reference(item, aliases, module,
                                           defined[module])) is not None}
        targets = (node.targets if isinstance(node, ast.Assign)
                   else [node.target])
        for target in targets:
            if isinstance(target, ast.Name) and refs:
                found[target.id] = refs
    return found


def _imports(tree: ast.Module) -> dict:
    """Local name -> (surface module, qualname), for the names this module
    imports OUT of another surface module.

    Which of those names is a function and which is a container of them is
    resolved against the source module, not here: `parse.py` imports both
    `bash_churn` (a function) and `LANE_PARSERS` (a constant holding three)
    from the same line shape.
    """
    imported = {}
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom):
            continue
        if not node.module or not node.module.startswith('backend.'):
            continue
        target = node.module.split('.', 1)[1]
        if target not in PARSE_SURFACE_MODULES:
            continue
        for alias in node.names:
            imported[alias.asname or alias.name] = (target, alias.name)
    return imported


class _Facts:
    """One surface module's functions, its names' origins, and its calls."""

    def __init__(self, module: str, tree: ast.Module, defined: dict,
                 containers: dict):
        self.module = module
        self.functions = defined[module]
        self.methods: dict = {}
        self.classes: set = set()
        self.modules = _module_aliases(tree)
        self.all_containers = containers
        self.containers = dict(containers[module])
        self.imported = _imports(tree)
        for name, (owner, qualname) in self.imported.items():
            # A container imported from another surface module is the same
            # edges, reached from here.
            if qualname in containers[owner]:
                self.containers[name] = containers[owner][qualname]
        self.edges = self._calls(tree)
        self.unresolved = unresolved(tree, module)

    def _calls(self, tree: ast.Module) -> dict:
        edges: dict = {}
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if _is_def(item):
                        self._body(edges, node.name, item.name, item)
            elif _is_def(node):
                self._body(edges, '', node.name, node)
        return edges

    def _body(self, edges: dict, owner: str, name: str,
              node: ast.AST) -> None:
        """Every call inside one function, attributed to that function.

        A nested `def` inside it is walked too: its calls run or do not
        with the function it is written inside.
        """
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            for target in self._resolve(child.func, owner):
                edges.setdefault(_qualname(owner, name), set()).add(target)

    def _resolve_table(self, func: ast.Subscript):
        """The functions a lookup of a KNOWN table holds.

        `LANE_PARSERS[fmt](...)` and `mod.TABLE[fmt](...)` are real calls
        into every function the table names, and no attribute expression
        spells them out, so the edges come from the table's own
        definition. The base is a Name or an Attribute — the two forms a
        lookup of a known constant is written in; a base that is neither
        is refused rather than dropped (see `reparse_bindings.unresolved`).
        """
        base = func.value
        if isinstance(base, ast.Name):
            yield from self.containers.get(base.id, ())
        elif isinstance(base, ast.Attribute):
            target = _reference(base, self.modules)
            if target is not None:
                yield from self.all_containers.get(target[0], {}).get(
                    target[1], ())

    def _resolve(self, func: ast.AST, owner: str):
        """The surface (module, qualname) pairs one call expression names."""
        if isinstance(func, ast.Subscript):
            yield from self._resolve_table(func)
            return
        if isinstance(func, ast.Name):
            name = func.id
            if name in self.functions:
                yield (self.module, name)
            elif name in self.imported:
                owner, qualname = self.imported[name]
                if qualname in self.all_containers.get(owner, {}):
                    yield from self.all_containers[owner][qualname]
                else:
                    yield (owner, qualname)
            else:
                yield from self.containers.get(name, ())
            return
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            target = _reference(func, self.modules)
            if target is not None:
                yield target
                return
            # `self.method(...)`: the class is the enclosing one, which the
            # caller knows and this expression does not.
            if func.value.id in ('self', 'cls') and owner:
                yield (self.module, _qualname(owner, func.attr))


def _facts(root: Path) -> dict:
    trees = _trees(root)
    defined = {name: _defines(tree) for name, tree in trees.items()}
    aliases = {name: _module_aliases(tree) for name, tree in trees.items()}
    containers = {name: _containers(tree, aliases[name], name, defined)
                  for name, tree in trees.items()}
    facts = {}
    for name, tree in trees.items():
        facts[name] = _Facts(name, tree, defined, containers)
        facts[name].methods = _methods(tree)
        facts[name].classes = set(facts[name].methods)
    return facts


def _defined(facts: dict, module: str, qualname: str) -> bool:
    """Does `module` define `qualname`? A call into a name that is not a
    function there — a re-export, a class, an attribute — is not a function
    the pass can fail to exercise."""
    facts_for = facts.get(module)
    return facts_for is not None and qualname in facts_for.functions


def reachable_surface(root: Path = ROOT) -> dict:
    """`{"module.qualname": "module"}` for every function the walk reaches.

    Raises rather than returning a partial surface: a module that cannot be
    read, or a root that is not there, means the walk cannot say what the
    pass can reach, and a short list is a silent pass.
    """
    facts = _facts(root)
    missed = [item for one in facts.values() for item in one.unresolved]
    if missed:
        raise ValueError('the walk cannot resolve these calls: '
                         + '; '.join(sorted(set(missed))))
    roots = []
    for module, qualname in ROOTS:
        if not _defined(facts, module, qualname):
            raise ValueError(f'the walk has no root {module}.{qualname}')
        roots.append((module, qualname))
    seen = set(roots)
    queue = list(roots)
    while queue:
        module, qualname = queue.pop()
        for target in sorted(facts[module].edges.get(qualname, ())):
            if not _defined(facts, *target) or target in seen:
                continue
            seen.add(target)
            queue.append(target)
            # A reached class is reached in every method it defines: the
            # pass instantiated it, so each method is a path the corpus has
            # to have walked at least once — and each one's own edges
            # count, which is why they are queued rather than only marked.
            # The methods come from the callee's OWN module: a class is
            # routinely constructed from another module's code (`parse`
            # builds `bash_churn.BashCommand`), and reading the methods
            # off the module whose function made the call would expand it
            # to nothing.
            owner, klass = target
            for name in facts[owner].methods.get(klass, ()):
                if (owner, name) not in seen:
                    seen.add((owner, name))
                    queue.append((owner, name))
    # A class is a reachability step, not a finding: it has no code object
    # of its own, so leaving it in the surface would name a function that
    # can never be exercised.
    return {f'{module}.{name}': module for module, name in sorted(seen)
            if name not in facts[module].classes}


def _code_of(target):
    """The code object a module attribute really executes, or None.

    A decorated function is not itself a code object: `functools.lru_cache`
    leaves a wrapper whose `__wrapped__` is the function, and
    `functools.cached_property` leaves a descriptor whose `func` is. Both
    are the same parse path, so the surface names them under the name the
    module gives them and this unwraps them.
    """
    for candidate in (target, getattr(target, 'func', None),
                      getattr(target, '__wrapped__', None)):
        code = getattr(candidate, '__code__', None)
        if code is not None:
            return code
    return None


def code_objects(surface: dict) -> dict:
    """`{code object: "module.qualname"}` for the live parse surface.

    Keyed by code object rather than by name so the INSTRUCTION callback
    resolves what it was told about in one dict lookup and records the rest
    as nothing. A surface entry with no code object at all is a walk that
    named something the import cannot resolve, and the caller reports it
    rather than treating it as never run.
    """
    table = {}
    for dotted in surface:
        module, _, qualname = dotted.partition('.')
        target = importlib.import_module(f'backend.{module}')
        for part in qualname.split('.'):
            target = getattr(target, part, None)
            if target is None:
                break
        code = _code_of(target)
        if code is not None:
            table[code] = dotted
    return table
