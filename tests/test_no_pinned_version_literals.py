"""SV-TEST-DATA's guard: no test pins repository-managed data with a literal.

A test that patches ``constants.PARSER_VERSION`` / ``PRICING_VERSION`` /
``MARKER_READER_VERSION`` (or sets the parser-version environment
variable) with a string literal silently changes meaning when the
committed value reaches the literal — the patch turns into a no-op, the
test keeps passing until the data moves under it, and then the failure
lands on an unrelated surface (a refresh bot's working tree, say).

The same holds for the LIVE-ROW shapes: a test that reads the committed
pricing document (``pricing.PRICING_JSON``), that binds its path at
module level, or that hands a live model or host name to ``rate_for`` /
``resolve`` / ``compute_cost`` breaks the same way when the committed
rows move.

The detector below is a pure function over source text: ``detect()``
lists the flagged sites (an AST scan), ``check()`` adds the allowlist
matching. An inline ``# sv-test-data: allow`` comment on the flagged
statement's first line excuses a site; a marker on a line with no
flagged site of either family is marker rot and fails the guard, so
stale excuses cannot accumulate.
"""
from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path
from typing import NamedTuple

from backend import pricing

REPO_ROOT = Path(__file__).resolve().parents[1]

VERSION_NAMES = ("PARSER_VERSION", "PRICING_VERSION", "MARKER_READER_VERSION")
MARKER = re.compile(r"#\s*sv-test-data:\s*allow\b")
RATE_CALL_NAMES = frozenset({"rate_for", "resolve", "compute_cost"})

# The live-row Site.shape values, and the problem text each carries.
_LIVE_ROW_SHAPES = ("pricing-json", "path-bind", "live-call")
_LIVE_ROW_TEXT = {
    "pricing-json":
        "reads the committed pricing document (pricing-json path bind); "
        "add '# sv-test-data: allow' with a reason, or load synthetic data",
    "path-bind":
        "binds the committed pricing document path (pricing-json path "
        "bind); add '# sv-test-data: allow' with a reason, or load "
        "synthetic data",
    "live-call":
        "names a live rate row (live model or host in a pricing call); "
        "add '# sv-test-data: allow' with a reason, or derive the name "
        "from the current tables",
}


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


def _marker_lines(source: str) -> set[int]:
    """Lines carrying a real ``# sv-test-data: allow`` COMMENT token.

    tokenize, not a line regex: marker text inside a string literal (a
    test's synthetic code sample, say) is not a comment.
    """
    found: set[int] = set()
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT and MARKER.search(token.string):
            found.add(token.start[0])
    return found


def check(source: str, *, wanted_models: frozenset[str] = frozenset(),
          wanted_hosts: frozenset[str] = frozenset()) -> list[str]:
    """Every problem in one module's source, as messages naming the line.

    A flagged site of either family without an allow marker on its own
    line, and a marker on a line that carries no flagged site of ANY
    family (marker rot), are both problems.
    """
    sites = detect(source, wanted_models=wanted_models,
                   wanted_hosts=wanted_hosts)
    site_lines = {site.line for site in sites}
    marker_lines = _marker_lines(source)
    problems = [_problem(site) for site in sites
                if site.line not in marker_lines]
    problems += [f"line {line}: sv-test-data marker on a line with no "
                 "flagged site (marker rot)"
                 for line in sorted(marker_lines - site_lines)]
    return problems


def _problem(site: Site) -> str:
    """One site's problem message, naming the line and the shape."""
    if site.shape in _LIVE_ROW_SHAPES:
        return f"line {site.line}: {_LIVE_ROW_TEXT[site.shape]}"
    return (f"line {site.line}: assigns a literal to {site.name} "
            f"({site.shape}); add '# sv-test-data: allow' with a reason, "
            "or derive the value from the current constant")


def test_setattr_positional_literal_is_flagged():
    result = detect("monkeypatch.setattr(constants, 'PARSER_VERSION', '2')\n")
    assert [site.name for site in result] == ["PARSER_VERSION"]
    assert result[0].line == 1
    assert result[0].value == "2"
    assert result[0].shape == "setattr"


def test_setattr_keyword_value_literal_is_flagged():
    result = detect(
        "monkeypatch.setattr(constants, 'PRICING_VERSION', value='9')\n")
    assert [(site.name, site.value) for site in result] == [
        ("PRICING_VERSION", "9")]


def test_bare_setattr_literal_is_flagged():
    result = detect("setattr(constants, 'MARKER_READER_VERSION', '2')\n")
    assert [(site.name, site.value) for site in result] == [
        ("MARKER_READER_VERSION", "2")]


def test_direct_constant_assignment_is_flagged():
    result = detect("constants.PARSER_VERSION = '82'\n")
    assert [(site.name, site.shape) for site in result] == [
        ("PARSER_VERSION", "assign")]


def test_setenv_with_a_version_name_is_flagged():
    result = detect("monkeypatch.setenv('PARSER_VERSION', '999')\n")
    assert [(site.name, site.shape, site.value) for site in result] == [
        ("PARSER_VERSION", "setenv", "999")]


def test_setenv_with_an_unrelated_name_is_not_flagged():
    assert detect("monkeypatch.setenv('R2_BUCKET', 'claude')\n") == []


def test_setenv_with_a_derived_value_is_not_flagged():
    assert detect(
        "monkeypatch.setenv('PARSER_VERSION', str(int(n) + 1))\n") == []


def test_derived_value_is_never_flagged():
    source = ("monkeypatch.setattr(constants, 'PRICING_VERSION',\n"
              "    str(int(constants.PRICING_VERSION) + 1))\n")
    assert detect(source) == []


def test_non_version_constant_names_are_not_flagged():
    assert detect("monkeypatch.setattr(constants, '__file__', 'x')\n") == []
    assert detect("constants.OTHER_VERSION = '1'\n") == []
    assert detect(
        "monkeypatch.setattr(constants, 'VERSION', 'x')\n") == []


def test_non_literal_values_are_not_flagged():
    assert detect(
        "monkeypatch.setattr(constants, 'PARSER_VERSION', 81)\n") == []
    assert detect(
        "monkeypatch.setattr(constants, 'PARSER_VERSION', name)\n") == []


def test_stored_version_row_values_are_not_flagged():
    """A stored-version row's value is data, not a constant assignment."""
    assert detect(
        "_seed(c, key, 1, pricing_version='abc')\n") == []


def test_marker_on_the_site_line_excuses_it():
    source = ("monkeypatch.setattr(constants, 'PARSER_VERSION', '2')  "
              "# sv-test-data: allow (synthetic)\n")
    assert check(source) == []
    assert [site.name for site in detect(source)] == ["PARSER_VERSION"]


def test_site_without_a_marker_fails_check():
    problems = check("monkeypatch.setenv('PARSER_VERSION', '999')\n")
    assert len(problems) == 1
    assert "line 1" in problems[0]
    assert "PARSER_VERSION" in problems[0]


def test_marker_on_a_line_without_a_site_is_rot():
    source = ("x = 1  # sv-test-data: allow (gone)\n"
              "monkeypatch.setenv('PARSER_VERSION', '999')\n")
    problems = check(source)
    assert len(problems) == 2
    assert "line 2" in problems[0] and "PARSER_VERSION" in problems[0]
    assert "line 1" in problems[1] and "rot" in problems[1]


def test_marker_text_inside_a_string_is_not_a_marker():
    source = ("CODE = \"monkeypatch.setenv('PARSER_VERSION', '9')  "
              "# sv-test-data: allow (sample)\"\n"
              "monkeypatch.setenv('PARSER_VERSION', '9')\n")
    problems = check(source)
    assert len(problems) == 1
    assert "line 2" in problems[0]


def test_marker_text_inside_the_value_cannot_self_excuse():
    """Marker text inside a value literal is not a comment on its line."""
    source = ("monkeypatch.setattr(constants, 'PARSER_VERSION',\n"
              "    'x  # sv-test-data: allow (self)')\n")
    problems = check(source)
    assert len(problems) == 1
    assert "line 2" in problems[0]


def test_multiline_statement_marks_on_the_literal_line():
    source = ("monkeypatch.setattr(\n"
              "    constants, 'PARSER_VERSION',\n"
              "    '2')  # sv-test-data: allow (synthetic)\n")
    assert check(source) == []


def test_multiline_marker_on_an_inner_line_is_rot():
    source = ("monkeypatch.setattr(\n"
              "    constants, 'PARSER_VERSION', '2')\n"
              "unused = 1  # sv-test-data: allow (misplaced)\n")
    problems = check(source)
    assert len(problems) == 2
    assert "line 2" in problems[0]
    assert "line 3" in problems[1] and "rot" in problems[1]


# --- the live-row family: the committed-document and rate-row sites ---

WANTED_MODELS = frozenset({"acme/acme-9", "claude-sonnet-5"})
WANTED_HOSTS = frozenset({"HostCo"})


def test_rate_call_with_a_wanted_model_literal_is_flagged():
    result = detect("rate_for('acme/acme-9')\n",
                    wanted_models=WANTED_MODELS, wanted_hosts=WANTED_HOSTS)
    assert [(site.name, site.shape, site.value) for site in result] == [
        ("rate_for", "live-call", "acme/acme-9")]


def test_rate_call_normalises_the_model_literal():
    result = detect("rate_for('acme/acme.9')\n",
                    wanted_models=WANTED_MODELS, wanted_hosts=WANTED_HOSTS)
    assert [(site.name, site.value) for site in result] == [
        ("rate_for", "acme/acme.9")]


def test_rate_call_folds_suffixed_model_spellings():
    result = detect("rate_for('claude-sonnet-5[1m]')\n",
                    wanted_models=WANTED_MODELS, wanted_hosts=WANTED_HOSTS)
    assert [(site.name, site.value) for site in result] == [
        ("rate_for", "claude-sonnet-5[1m]")]


def test_resolve_call_with_a_wanted_model_is_flagged():
    result = detect("pricing.resolve('acme/acme-9', ts)\n",
                    wanted_models=WANTED_MODELS, wanted_hosts=WANTED_HOSTS)
    assert [(site.name, site.value) for site in result] == [
        ("resolve", "acme/acme-9")]


def test_rate_call_with_a_wanted_host_keyword_is_flagged():
    result = detect("pricing.resolve(model, provider='HostCo')\n",
                    wanted_models=WANTED_MODELS, wanted_hosts=WANTED_HOSTS)
    assert [(site.name, site.value) for site in result] == [
        ("resolve", "HostCo")]


def test_host_literal_in_another_rate_call_is_flagged():
    result = detect("compute_cost('HostCo', tokens)\n",
                    wanted_models=WANTED_MODELS, wanted_hosts=WANTED_HOSTS)
    assert [(site.name, site.value) for site in result] == [
        ("compute_cost", "HostCo")]


def test_rate_call_with_a_variable_argument_is_not_flagged():
    assert detect("rate_for(model)\n",
                  wanted_models=WANTED_MODELS,
                  wanted_hosts=WANTED_HOSTS) == []


def test_rate_call_with_an_unknown_model_literal_is_not_flagged():
    assert detect("rate_for('acme-ghost-9')\n",
                  wanted_models=WANTED_MODELS,
                  wanted_hosts=WANTED_HOSTS) == []


def test_rate_call_of_a_differently_named_function_is_not_flagged():
    assert detect("price_for('acme/acme-9')\n",
                  wanted_models=WANTED_MODELS,
                  wanted_hosts=WANTED_HOSTS) == []


def test_pricing_json_reference_is_flagged():
    result = detect("pricing.PRICING_JSON.read_text()\n")
    assert [(site.name, site.shape, site.value, site.line)
            for site in result] == [
        ("PRICING_JSON", "pricing-json", "pricing.PRICING_JSON", 1)]


def test_pricing_json_off_the_pricing_module_is_not_flagged():
    assert detect("PRICING_JSON.read_text()\n") == []


def test_module_level_doc_path_bind_is_flagged():
    source = "PRICING_JSON = ROOT / 'src' / 'pricing.json'\n"
    result = detect(source)
    assert [(site.name, site.shape, site.value) for site in result] == [
        ("PRICING_JSON", "path-bind", "pricing.json")]


def test_function_local_doc_path_bind_is_not_flagged():
    source = ("def make(tmp):\n"
              "    p = tmp / 'src' / 'pricing.json'\n"
              "    return p\n")
    assert detect(source) == []


def test_marker_excuses_a_live_row_site():
    source = ("doc = pricing.PRICING_JSON.read_text()  "
              "# sv-test-data: allow (synthetic doc)\n")
    assert check(source) == []


def test_rot_fails_when_a_live_row_site_is_removed():
    source = ("x = 1  # sv-test-data: allow (gone)\n"
              "doc = pricing.PRICING_JSON.read_text()\n")
    problems = check(source)
    assert len(problems) == 2
    assert "line 2" in problems[0] and "pricing document" in problems[0]
    assert "line 1" in problems[1] and "rot" in problems[1]


def test_the_tests_tree_holds_no_unmarked_site_and_no_rotted_marker():
    wanted_models = frozenset(pricing.MODEL_RATES)
    wanted_hosts = frozenset(host for _, host in pricing.PROVIDER_RATES)
    problems = []
    for path in sorted((REPO_ROOT / "tests").rglob("*.py")):
        for message in check(path.read_text(encoding="utf-8"),
                             wanted_models=wanted_models,
                             wanted_hosts=wanted_hosts):
            problems.append(f"{path.name}:{message}")
    assert not problems, "\n".join(problems)
