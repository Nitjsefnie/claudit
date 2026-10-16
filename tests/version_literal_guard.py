"""The version-literal guard's detector, out of its test module.

test_no_pinned_version_literals.py outgrew the test-file size cap when
issue #675 added the tokenizer short-circuit pin, and the size
ratchet's rule moves code into a new module rather than raising an
entry. This module is the pure scan: the flagged-site detector and the
shapes it reports. The guard surface that drives it — the admission
gate, the marker pass, check() and the tree scan — and every test stay
in the test module, which the suite-cost bench already pins.
"""
from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from functools import cache
from typing import NamedTuple, get_args

from backend import pricing
from tests.source_scan_cache import for_source


VERSION_NAMES = ("PARSER_VERSION", "PRICING_VERSION", "MARKER_READER_VERSION")
# Version-constant modules (issue #986: hourly bot rewrite of constants.py).
VERSION_MODULES = frozenset({"constants", "parser_version"})
RATE_CALL_NAMES = frozenset({"rate_for", "resolve", "compute_cost"})


class Site(NamedTuple):
    """One flagged site of either family: line, name, shape, value."""

    line: int
    name: str
    shape: str
    value: str


def detect(source: str, *, wanted_models: frozenset[str] = frozenset(),
           wanted_hosts: frozenset[str] = frozenset()) -> list[Site]:
    """The flagged sites in one module's source, both families.

    An AST scan, so strings and comments never masquerade as code. The
    version-constant family flags the three literal-assignment shapes
    against the three constant names (either VERSION_MODULES module):
    ``setattr(constants, "X_VERSION", "<literal>")`` (value positional
    or keyword, receiver irrelevant so a bare ``setattr`` matches too),
    a direct ``constants.X_VERSION = "<literal>"``, and
    ``monkeypatch.setenv("X_VERSION", "<literal>")``.

    The live-row family (``wanted_models`` / ``wanted_hosts`` name the
    live rows; only the tree scan passes the real ones) flags: a
    reference to ``pricing.PRICING_JSON`` (wherever it appears — call
    argument, standalone expression, chained attribute); a module-level
    ``Assign`` / ``AnnAssign`` whose value subtree contains the string
    constant "pricing.json" (function-local binds of tmp fixture paths
    never flag); and a ``rate_for`` / ``resolve`` / ``compute_cost``
    call whose positional or keyword argument is a string constant
    naming a wanted model (``pricing._normalise`` membership, or a
    suffixed spelling folded the way ``pricing._match_key`` folds
    ``MODEL_RATES`` and ``pricing._vendor_match`` folds the tracked
    table's bare forms) or equal to a wanted host. The reported line is
    the literal's own line — the marker excuses the literal, so it
    sits beside what it excuses.
    """
    wanted: set[str] = set(VERSION_NAMES)
    artifacts = for_source(source)
    tree = artifacts.tree()
    assert isinstance(tree, ast.Module)
    sites: list[Site] = []
    for node in _site_nodes(tree):
        if isinstance(node, ast.Call):
            sites += _call_sites(node, wanted)
            sites += _live_call_sites(node, wanted_models, wanted_hosts)
        elif isinstance(node, ast.Attribute):
            if _is_pricing_json(node):
                sites.append(Site(node.lineno, "PRICING_JSON",
                                  "pricing-json", "pricing.PRICING_JSON"))
        elif isinstance(node, ast.Assign):
            target = node.targets[0] if len(node.targets) == 1 else None
            name = _version_module_attribute(target, wanted)
            literal = _string_constant(node.value)
            if name is not None and literal is not None:
                sites.append(Site(node.value.lineno, name, "assign", literal))
    for stmt in tree.body:
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            sites += _doc_path_bind_sites(stmt)
    return sorted(sites)


_LEAF_BASES = (ast.expr_context, ast.operator, ast.unaryop, ast.boolop,
               ast.cmpop)
_SITE_TYPES = (ast.Call, ast.Attribute, ast.Assign)


def _has_child_nodes(annotation: object) -> bool:
    if isinstance(annotation, type):
        return (issubclass(annotation, ast.AST)
                and not issubclass(annotation, _LEAF_BASES))
    return any(_has_child_nodes(arg) for arg in get_args(annotation))


@cache
def _child_fields(kind: type[ast.AST]) -> tuple[str, ...]:
    # pylint: disable-next=protected-access  # Stdlib AST schema; no public accessor.
    return tuple(name for name, annotation in kind._field_types.items()
                 if _has_child_nodes(annotation))


def _site_nodes(tree: ast.AST) -> Iterator[ast.AST]:
    """Walk AST-valued fields; yield only detector-relevant node types.

    Unlike ast.walk, this skips scalar fields and terminal operators/contexts
    using the parser's field schema. That saves per-node Python field/type
    checks. DFS replaces breadth-first order safely because detect sorts sites.
    Dict unpack keys and absent keyword defaults contribute None list entries.
    """
    stack: list[ast.AST | None] = [tree]
    while stack:
        node = stack.pop()
        if node is None:
            continue
        if isinstance(node, _SITE_TYPES):
            yield node
        for name in _child_fields(type(node)):
            value = getattr(node, name)
            if isinstance(value, list):
                stack.extend(value)
            elif value is not None:
                stack.append(value)


def _live_call_sites(node: ast.Call, wanted_models: frozenset[str],
                     wanted_hosts: frozenset[str]) -> list[Site]:
    """The flagged sites among rate calls carrying a live-name literal.

    The call's function name (bare ``Name`` or ``Attribute.attr``,
    receiver irrelevant) is one of RATE_CALL_NAMES and a positional or
    keyword-value argument is a string constant naming a wanted model
    or host.
    """
    func = node.func
    if isinstance(func, ast.Attribute):
        name = func.attr
    elif isinstance(func, ast.Name):
        name = func.id
    else:
        return []
    if name not in RATE_CALL_NAMES:
        return []
    sites: list[Site] = []
    for arg in (*node.args, *(kw.value for kw in node.keywords)):
        literal = _string_constant(arg)
        if literal is not None and _names_wanted_row(
                literal, wanted_models, wanted_hosts):
            sites.append(Site(arg.lineno, name, "live-call", literal))
    return sites


@cache
def _names_wanted_row(literal: str, wanted_models: frozenset[str],
                      wanted_hosts: frozenset[str]) -> bool:
    """Whether the literal names a wanted live model or host.

    Hosts compare exactly; models by ``pricing._normalise`` membership
    or by the longest-key fold ``pricing._match_key`` applies to
    ``MODEL_RATES`` and ``pricing._vendor_match`` to the tracked table's
    bare forms (one rule set, two tables) — mirrored over the wanted set
    here, because those helpers read the global tables and a parameter
    set cannot pass through them.
    """
    if literal in wanted_hosts:
        return True
    norm = pricing._normalise(literal)  # pylint: disable=protected-access
    if norm in wanted_models:
        return True
    return _match_wanted(norm, wanted_models) is not None


def _match_wanted(norm: str, wanted: frozenset[str]) -> str | None:
    """The LONGEST wanted key `norm` names, or None.

    The fold pricing._match_key applies to MODEL_RATES and
    pricing._vendor_match to the tracked table's bare forms, over a
    parameter set: a key matches when `norm` starts with it and the
    rest is empty or a bracket, at-suffix or snapshot spelling.

    The candidates are derived from the QUERY, never the set: the keys
    some fold-accepted rest completes are exactly `norm` minus such a
    tail, so `_splits` enumerates those and membership decides — the
    cost is the query's own length, never |wanted| (issue #829: the
    tree scan pays this per string constant under tests/, and a linear
    fold priced the suite-cost run phase by src/pricing.json's row
    count, moving it on every hourly refresh). ``_match_wanted_scan``
    is the literal scan; the parity test pins the two together.
    """
    best = None
    for key in _splits(norm):
        if key in wanted and (best is None or len(key) > len(best)):
            best = key
    return best


def _splits(norm: str) -> Iterator[str]:
    """Every key candidate the fold can accept: `norm` itself (the
    empty rest), and `norm` minus any tail the fold accepts — one
    opening with ``[`` or ``@``, or a snapshot suffix. The predicate is
    the scan's own rest-validity, applied to tails of the query. A
    non-empty `norm` yields only non-empty candidates (the loop starts
    at 1); the empty query yields the empty candidate, which no
    committed table names as a key, so the fold still answers None on
    it.

    Candidates come out in ascending cut order. A snapshot suffix
    (``^-?\\d{6,8}$``, whose ``$`` also admits one trailing newline) is
    at most ``_SNAPSHOT_TAIL`` characters long, so only cuts within that
    distance of the end are tried against it, one by one; the ``[``/``@``
    cuts before them are found by a regex scan, never a per-character
    loop."""
    yield norm
    size = len(norm)
    tail = max(1, size - _SNAPSHOT_TAIL)
    for match in _BRACKET_AT.finditer(norm, 1, tail):
        yield norm[:match.start()]
    for i in range(tail, size):
        rest = norm[i:]
        # pylint: disable-next=protected-access
        if rest[0] in "[@" or pricing._SNAPSHOT_SUFFIX.match(rest):
            yield norm[:i]


# The cuts opening a ``[``/``@`` tail, as zero-width matches; and the
# longest tail the snapshot suffix can match: ``-``, eight digits and
# the newline ``$`` admits.
_BRACKET_AT = re.compile(r"(?=[\[@])")
_SNAPSHOT_TAIL = 10


def _match_wanted_scan(norm: str, wanted: frozenset[str]) -> str | None:
    """The literal O(|wanted|) scan, kept as the reference the parity
    test pins ``_match_wanted`` against; no production path calls it."""
    best = None
    for key in wanted:
        if not norm.startswith(key) or (best and len(key) <= len(best)):
            continue
        rest = norm[len(key):]
        # pylint: disable-next=protected-access
        if rest == "" or rest[0] in "[@" or pricing._SNAPSHOT_SUFFIX.match(
                rest):
            best = key
    return best


def _is_pricing_json(node: ast.Attribute) -> bool:
    """Whether the node reads `pricing.PRICING_JSON`, the committed document."""
    return (isinstance(node.value, ast.Name) and node.value.id == "pricing"
            and node.attr == "PRICING_JSON")


def _doc_path_bind_sites(stmt: ast.Assign | ast.AnnAssign) -> list[Site]:
    """The path-bind sites in one TOP-LEVEL assignment.

    The value subtree is scanned for the committed-document path
    constant; each constant naming it is a site. Function-local binds
    of tmp fixture paths never reach this loop — scope is the module
    body's own Assign / AnnAssign statements.
    """
    value = stmt.value
    if value is None:
        return []
    target = stmt.target if isinstance(stmt, ast.AnnAssign) else (
        stmt.targets[0] if len(stmt.targets) == 1 else None)
    name = target.id if isinstance(target, ast.Name) else ""
    sites: list[Site] = []
    for node in ast.walk(value):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and "pricing.json" in node.value):
            sites.append(Site(node.lineno, name, "path-bind", node.value))
    return sites


def _call_sites(node: ast.Call, wanted: set[str]) -> list[Site]:
    """The flagged sites among setattr/setenv calls, positionally."""
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "setenv":
        name = _name_constant(_pos_or_kw(node, 0, None), wanted)
        value = _pos_or_kw(node, 1, "value")
        literal = _string_constant(value)
        if name is not None and literal is not None and value is not None:
            return [Site(value.lineno, name, "setenv", literal)]
        return []
    if not ((isinstance(func, ast.Attribute) and func.attr == "setattr")
            or isinstance(func, ast.Name) and func.id == "setattr"):
        return []
    target = _pos_or_kw(node, 0, "target")
    name = _name_constant(_pos_or_kw(node, 1, "name"), wanted)
    value = _pos_or_kw(node, 2, "value")
    literal = _string_constant(value)
    if (_is_version_module(target) and name is not None
            and literal is not None and value is not None):
        return [Site(value.lineno, name, "setattr", literal)]
    return []


def _is_version_module(node: ast.expr | None) -> bool:
    """Whether the node names a version-constant module."""
    return isinstance(node, ast.Name) and node.id in VERSION_MODULES


def _name_constant(node: ast.expr | None,
                   wanted: set[str]) -> str | None:
    """The name a string literal carries, if it is a wanted one."""
    value = _string_constant(node)
    return value if value in wanted else None


def _version_module_attribute(target: ast.expr | None,
                              wanted: set[str]) -> str | None:
    """The constant name a `<module>.X` target assigns."""
    if (isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id in VERSION_MODULES
            and target.attr in wanted):
        return target.attr
    return None


def _string_constant(node: ast.expr | None) -> str | None:
    """The literal's string, if the node is a string constant."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _pos_or_kw(node: ast.Call, index: int,
               keyword: str | None) -> ast.expr | None:
    """A call argument given positionally, or under its keyword name."""
    if index < len(node.args):
        return node.args[index]
    if keyword is not None:
        for kw in node.keywords:
            if kw.arg == keyword:
                return kw.value
    return None
