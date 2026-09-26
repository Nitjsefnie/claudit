"""SV-TEST-DATA's guard: no test pins a version constant with a literal.

A test that patches ``constants.PARSER_VERSION`` / ``PRICING_VERSION`` /
``MARKER_READER_VERSION`` (or sets the parser-version environment
variable) with a string literal silently changes meaning when the
committed value reaches the literal — the patch turns into a no-op, the
test keeps passing until the data moves under it, and then the failure
lands on an unrelated surface (a refresh bot's working tree, say).

The detector below is a pure function over source text: ``detect()``
lists the flagged sites (an AST scan for the three assignment shapes
against the three constant names), ``check()`` adds the allowlist
matching. An inline ``# sv-test-data: allow`` comment on the flagged
statement's first line excuses a site; a marker on a line with no
flagged site is marker rot and fails the guard, so stale excuses cannot
accumulate.
"""
from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parents[1]

VERSION_NAMES = ("PARSER_VERSION", "PRICING_VERSION", "MARKER_READER_VERSION")
MARKER = re.compile(r"#\s*sv-test-data:\s*allow\b")


class Site(NamedTuple):
    """One pinned version constant: its line, name, shape and literal."""

    line: int
    name: str
    shape: str
    value: str


def detect(source: str) -> list[Site]:
    """The version-constant literal sites in one module's source.

    An AST scan, so strings and comments never masquerade as code. The
    three shapes: ``setattr(constants, "X_VERSION", "<literal>")`` (the
    value positional or keyword, receiver irrelevant so a bare
    ``setattr`` matches too), a direct ``constants.X_VERSION =
    "<literal>"``, and ``monkeypatch.setenv("X_VERSION", "<literal>")``.
    ``X_VERSION`` is one of the three names in VERSION_NAMES, exactly.
    The reported line is the literal's own line — the marker excuses
    the literal, so it sits beside what it excuses.
    """
    wanted: set[str] = set(VERSION_NAMES)
    sites: list[Site] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            sites += _call_sites(node, wanted)
        elif isinstance(node, ast.Assign):
            target = node.targets[0] if len(node.targets) == 1 else None
            name = _constants_attribute(target, wanted)
            literal = _string_constant(node.value)
            if name is not None and literal is not None:
                sites.append(Site(node.value.lineno, name, "assign", literal))
    return sorted(sites)


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


def check(source: str) -> list[str]:
    """Every problem in one module's source, as messages naming the line.

    A flagged site without an allow marker on its own line, and a marker
    on a line that carries no flagged site (marker rot), are both
    problems.
    """
    sites = detect(source)
    site_lines = {site.line for site in sites}
    marker_lines = _marker_lines(source)
    problems = [f"line {site.line}: assigns a literal to {site.name} "
                f"({site.shape}); add '# sv-test-data: allow' with a reason, "
                "or derive the value from the current constant"
                for site in sites if site.line not in marker_lines]
    problems += [f"line {line}: sv-test-data marker on a line with no pinned "
                 "version literal (marker rot)"
                 for line in sorted(marker_lines - site_lines)]
    return problems


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


def test_the_tests_tree_holds_no_unmarked_site_and_no_rotted_marker():
    problems = []
    for path in sorted((REPO_ROOT / "tests").glob("*.py")):
        for message in check(path.read_text(encoding="utf-8")):
            problems.append(f"{path.name}:{message}")
    assert not problems
