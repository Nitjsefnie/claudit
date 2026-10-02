#!/usr/bin/env python3
"""The names a module binds, and the calls a reader cannot resolve.

Two questions about a module's own grammar, kept out of the walker's file
because they are about the LANGUAGE rather than about this repository:
``bound_names`` answers "does this module give a value to this name
anywhere?", and ``unresolved`` answers "is there a call in here the reader
knows nothing about?".

The second is the fail-closed half (issue #503). A walker's completeness
claim is a claim about a grammar, and `node.func` holds more than a Name:
an Attribute, a Subscript, or a Call whose result is called back. The Name
forms are the ones that could name a surface function and still slip
through — a bare callee the module never binds is a name the walk knows
nothing about, and dropping it silently would make the reachable surface
an under-approximation that reads exactly like a covered one.

An attribute call on a receiver the module did not import (a foreign
object's method, a local instance reached through something other than
`self`) is NOT refused: it is a different grammar form with a different
answer — resolving it means resolving types — and it is named in the
walker's docstring as that module's known limit rather than pretended away.
"""
from __future__ import annotations

import ast
import builtins

#: What a module can call without binding: the interpreter's own. A callee
#: in here names no parse function, so it is not an unresolvable one.
BUILTINS = frozenset(dir(builtins))


def _pattern_names(node) -> set:
    """The names one binding TARGET binds, through tuple/list/star."""
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, (ast.Tuple, ast.List)):
        return {name for element in node.elts
                for name in _pattern_names(element)}
    if isinstance(node, ast.Starred):
        return _pattern_names(node.value)
    return set()


def _argument_names(node) -> set:
    """The names a function definition binds: its own, and every
    parameter form — positional, keyword-only, `*args`, `**kwargs`."""
    arguments = node.args
    names = {node.name}
    for group in (arguments.posonlyargs, arguments.args, arguments.kwonlyargs):
        names.update(argument.arg for argument in group)
    for extra in (arguments.vararg, arguments.kwarg):
        if extra is not None:
            names.add(extra.arg)
    return names


def _targets_names(node) -> set:
    return {name for target in node.targets
            for name in _pattern_names(target)}


def _one_target_names(node) -> set:
    return _pattern_names(node.target)


def _with_names(node) -> set:
    return {name for item in node.items if item.optional_vars is not None
            for name in _pattern_names(item.optional_vars)}


def _handler_name(node) -> set:
    return {node.name} if node.name else set()


def _capture_names(node) -> set:
    return {name for name in (node.name, node.rest) if name}


def _class_name(node) -> set:
    return {node.name}


def _comp_names(node) -> set:
    return {name for generator in node.generators
            for name in _pattern_names(generator.target)}


def _declared_names(node) -> set:
    return set(node.names)


def _import_names(node) -> set:
    return {(alias.asname or alias.name).split('.')[0] for alias in node.names}


#: The language's binding grammar, one row per production, in the order
#: `isinstance` should try them. A table rather than a branch chain: the
#: productions are DATA here, so adding one is adding a row, and a
#: production this table omits is a callee the module binds somewhere the
#: walk does not look — which reads exactly like one it cannot resolve.
_BINDING_PRODUCTIONS = (
    (ast.Assign, _targets_names),
    ((ast.AnnAssign, ast.AugAssign, ast.NamedExpr), _one_target_names),
    ((ast.For, ast.AsyncFor), _one_target_names),
    ((ast.With, ast.AsyncWith), _with_names),
    (ast.ExceptHandler, _handler_name),
    ((ast.MatchAs, ast.MatchStar, ast.MatchMapping), _capture_names),
    (ast.ClassDef, _class_name),
    ((ast.FunctionDef, ast.AsyncFunctionDef), _argument_names),
    ((ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp), _comp_names),
    ((ast.Global, ast.Nonlocal), _declared_names),
    ((ast.Import, ast.ImportFrom), _import_names),
)


def _names_bound_by(node) -> set:
    """Every name ONE statement binds, read off the form it binds with."""
    for kinds, extract in _BINDING_PRODUCTIONS:
        if isinstance(node, kinds):
            return extract(node)
    return set()


def bound_names(tree: ast.Module) -> set:
    """Every name the module GIVES A VALUE TO, over the whole grammar.

    Not "every Name node": the callee of a call is itself an `ast.Name`,
    so counting Names would bind every callee the walk is trying to
    resolve and the check would never fire. Each binding production is
    read off its own form (``_names_bound_by``).

    Scope is deliberately not analysed. The only question is "does this
    module spell a binding for this name somewhere?", and a name bound in
    another scope is still a name the module gives a value to.
    """
    bound: set = set()

    def walk(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            bound.update(_names_bound_by(child))
            walk(child)

    walk(tree)
    return bound


def _parameter_names(tree: ast.Module) -> set:
    """Every function PARAMETER the module spells, in any scope.

    A callee that is one of these is a value the caller passed in, which
    the reader cannot resolve to a parse function — and a name bound
    elsewhere in the module would otherwise hide that.
    """
    names: set = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        arguments = node.args
        for group in (arguments.posonlyargs, arguments.args,
                      arguments.kwonlyargs):
            names.update(argument.arg for argument in group)
        for extra in (arguments.vararg, arguments.kwarg):
            if extra is not None:
                names.add(extra.arg)
    return names


def unresolved(tree: ast.Module, module: str) -> list:
    """Call expressions this reader cannot resolve, which it REFUSES."""
    found = []
    bound = bound_names(tree)
    parameters = _parameter_names(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Call):
            found.append(f'{module}: a call of a call\'s result')
        elif isinstance(func, ast.Name) and (
                func.id not in bound and func.id not in BUILTINS):
            found.append(f'{module}: unbound callee {func.id!r}')
        elif isinstance(func, ast.Name) and func.id in parameters:
            # `callback(payload)`: the target is whatever the caller
            # passed, and a same-named binding elsewhere in the module
            # would let it read as resolved.
            found.append(f'{module}: callee is a parameter: {func.id!r}')
        elif isinstance(func, ast.Subscript) and not isinstance(
                func.value, (ast.Name, ast.Attribute)):
            # A lookup of a constant the walk cannot name is a call whose
            # target it knows nothing about; a Name or an Attribute base is
            # the two forms the reader DOES resolve.
            found.append(f'{module}: subscript of '
                         f'{type(func.value).__name__}')
    return found
