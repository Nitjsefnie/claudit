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
from typing import NamedTuple

from backend import pricing


VERSION_NAMES = ("PARSER_VERSION", "PRICING_VERSION", "MARKER_READER_VERSION")
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
    against the three constant names: ``setattr(constants, "X_VERSION",
    "<literal>")`` (the value positional or keyword, receiver
    irrelevant so a bare ``setattr`` matches too), a direct
    ``constants.X_VERSION = "<literal>"``, and
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
    ``MODEL_RATES``) or equal to a wanted host. The reported line is
    the literal's own line — the marker excuses the literal, so it
    sits beside what it excuses.
    """
    wanted: set[str] = set(VERSION_NAMES)
    tree = ast.parse(source)
    sites: list[Site] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            sites += _call_sites(node, wanted)
            sites += _live_call_sites(node, wanted_models, wanted_hosts)
        elif isinstance(node, ast.Attribute):
            if _is_pricing_json(node):
                sites.append(Site(node.lineno, "PRICING_JSON",
                                  "pricing-json", "pricing.PRICING_JSON"))
        elif isinstance(node, ast.Assign):
            target = node.targets[0] if len(node.targets) == 1 else None
            name = _constants_attribute(target, wanted)
            literal = _string_constant(node.value)
            if name is not None and literal is not None:
                sites.append(Site(node.value.lineno, name, "assign", literal))
    for stmt in tree.body:
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            sites += _doc_path_bind_sites(stmt)
    return sorted(sites)


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


def _names_wanted_row(literal: str, wanted_models: frozenset[str],
                      wanted_hosts: frozenset[str]) -> bool:
    """Whether the literal names a wanted live model or host.

    Hosts compare exactly; models by ``pricing._normalise`` membership
    or by the longest-key fold ``pricing._match_key`` applies to
    ``MODEL_RATES`` — mirrored over the wanted set here, because that
    helper reads the global table and a parameter set cannot pass
    through it.
    """
    if literal in wanted_hosts:
        return True
    norm = pricing._normalise(literal)  # pylint: disable=protected-access
    if norm in wanted_models:
        return True
    return _match_wanted(norm, wanted_models) is not None


def _match_wanted(norm: str, wanted: frozenset[str]) -> str | None:
    """The LONGEST wanted key `norm` names, or None.

    The fold pricing._match_key applies to MODEL_RATES, over a
    parameter set: a key matches when `norm` starts with it and the
    rest is empty or a bracket, at-suffix or snapshot spelling.
    """
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
    if (_is_constants(target) and name is not None
            and literal is not None and value is not None):
        return [Site(value.lineno, name, "setattr", literal)]
    return []


def _is_constants(node: ast.expr | None) -> bool:
    """Whether the node names the constants module directly."""
    return isinstance(node, ast.Name) and node.id == "constants"


def _name_constant(node: ast.expr | None,
                   wanted: set[str]) -> str | None:
    """The name a string literal carries, if it is a wanted one."""
    value = _string_constant(node)
    return value if value in wanted else None


def _constants_attribute(target: ast.expr | None,
                         wanted: set[str]) -> str | None:
    """The constant name a `constants.X = ...` target assigns, if any."""
    if (isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "constants"
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
