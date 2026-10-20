"""Comment stripping and lexical preservation, with hand-written goldens.

Every assertion fails on unfixed code by construction: backend.js_min did
not exist before this task. The corpus checks supplement the goldens;
an independent Espree AST comparison is run outside this Python suite.
"""
from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from backend.js_min import JsMinError, comment_spans, minify_js


@pytest.mark.parametrize(("source", "expected"), [
    ("let a = 1; // tail\n// whole line\na++;\n",
     "let a = 1;\n\na++;\n"),
    ("a/* inline */+ b;\n/* first\n second\n third */c();",
     "a + b;\n\n\nc();"),
    ("const s = 'a  // b /* c */'; // removed\n",
     "const s = 'a  // b /* c */';\n"),
    ('const s = "a  // b /* c */"; /* removed */',
     'const s = "a  // b /* c */";'),
    (r'''let s = 'it\'s // literal'; let t = "a\"/* literal */"; // gone''',
     r'''let s = 'it\'s // literal'; let t = "a\"/* literal */";'''),
    ("const t = `a  // b\n  /* c */`; // removed\n",
     "const t = `a  // b\n  /* c */`;\n"),
    ('const t = `raw ${ "//" + \'/*\' /* gone */ } tail`;\n',
     'const t = `raw ${ "//" + \'/*\' } tail`;\n'),
    (r"const r = /a\/\/b[/* ]+/gi; // gone" + "\n",
     r"const r = /a\/\/b[/* ]+/gi;" + "\n"),
    ("const x = total / count / 2; // gone\n",
     "const x = total / count / 2;\n"),
    ("const el = <p>  http://example.com\n /* not a comment */  </p>; // gone\n",
     "const el = <p>  http://example.com\n /* not a comment */  </p>;\n"),
    ("const el = <p>{/* eslint-disable-next-line */}</p>;",
     "const el = <p>{ }</p>;"),
    ('const el = <Tag enabled href="http://example.com" data={ a /* x */ } />;',
     'const el = <Tag enabled href="http://example.com" data={ a } />;'),
    ("const el = <>{items.map(x => <b key={x.id}>{x.label /* gone */}</b>)}</>;",
     "const el = <>{items.map(x => <b key={x.id}>{x.label }</b>)}</>;"),
    ("run();\n//# sourceMappingURL=app.js.map\n", "run();\n\n"),
    ("  const\t a   =  1; \t\n\t a  +\t b;  ", "const a = 1;\na + b;"),
    ("function f() {\n  return // ASI\n    { x: 1 };\n}\n",
     "function f() {\nreturn\n{ x: 1 };\n}\n"),
    ("left/* separator */right; a+/* separator */+b;",
     "left right; a+ +b;"),
    ("a(); // CRLF\r\n/* a\r\nb */ b();\r\n",
     "a();\r\n\r\nb();\r\n"),
    ("// eof", ""),
    ("\t \n  \t", "\n"),
    ("", ""),
], ids=[
    "line-comments", "block-comments", "single-string", "double-string",
    "escaped-quotes", "template-raw", "template-interpolation", "regex-slashes",
    "division", "jsx-text", "jsx-comment", "jsx-attributes", "nested-jsx",
    "source-map", "whitespace", "asi-return", "comment-token-boundaries",
    "crlf", "eof-comment", "blank-lines", "empty",
])
def test_golden_transform(source: str, expected: str) -> None:
    result = minify_js(source)
    assert result == expected
    assert result.count("\n") == source.count("\n")
    assert not comment_spans(result)
    assert minify_js(result) == result
    assert len(result) <= len(source)


@pytest.mark.parametrize("prefix", [
    "", "(", ",", "=", ":", "[", "!", "&", "|", "?", "{", "}", ";",
    "return ", "typeof ", "instanceof ", "new ", "delete ", "void ",
    "case ", "in ", "of ", "do ", "else ", "yield ", "await ", "=> ",
])
def test_regex_expression_start_tokens(prefix: str) -> None:
    source = prefix + r"/a[/* ]\/b/gi; // gone"
    assert minify_js(source) == prefix.rstrip() + (
        " " if prefix.endswith(" ") else "") + r"/a[/* ]\/b/gi;"


@pytest.mark.parametrize("left", ["name", "42", ")", "]", "'x'", "`x`", "/x/"])
def test_division_after_expression(left: str) -> None:
    assert minify_js(left + " / 2; // gone") == left + " / 2;"


@pytest.mark.parametrize("prefix", [
    "", "= ", "( ", ", ", "[ ", "{ ", "; ", ": ", "? ",
    "&& ", "|| ", "! ", "return ", "=> ",
])
def test_jsx_expression_start_tokens(prefix: str) -> None:
    source = prefix + "<Tag> // text /* text */ </Tag>; // gone"
    assert minify_js(source) == prefix + "<Tag> // text /* text */ </Tag>;"


@pytest.mark.parametrize("left", ["name", "42", ")", "]"])
def test_less_than_after_expression_is_comparison(left: str) -> None:
    assert minify_js(left + " < limit; // gone") == left + " < limit;"


def test_nested_templates_braces_regex_and_jsx() -> None:
    source = ('const t = `outer ${`inner ${({value: /[}/*]/}).value}` + '
              '(<b>{ {value: "}"}.value /* gone */ }</b>)} tail`;')
    expected = ('const t = `outer ${`inner ${({value: /[}/*]/}).value}` + '
                '(<b>{ {value: "}"}.value }</b>)} tail`;')
    assert minify_js(source) == expected
    assert comment_spans(source) == [(source.index("/* gone */"),
                                      source.index("/* gone */") + 10, "block")]


def test_comment_spans_are_utf8_byte_offsets() -> None:
    source = "const x = 'é'; /* α */\n// β\nx();"
    assert comment_spans(source) == [(16, 24, "block"), (25, 30, "line")]
    raw = source.encode("utf-8")
    assert [raw[start:end] for start, end, _ in comment_spans(source)] == [
        "/* α */".encode("utf-8"), "// β".encode("utf-8")]


@pytest.mark.parametrize("source", [
    "'unterminated", '"unterminated', "'unescaped\nnewline'", "/* unclosed",
    "`unclosed", "`raw ${1", "<Tag>", "<>", '<Tag value="open',
    "<Tag>{ {a: 1}", "<a></b>", "/unterminated", "/[unclosed/",
])
def test_unterminated_constructs_raise(source: str) -> None:
    with pytest.raises(JsMinError):
        minify_js(source)
    with pytest.raises(JsMinError):
        comment_spans(source)


ROOT = Path(__file__).resolve().parents[1]
SOURCES = subprocess.check_output(
    ["git", "ls-files", "src/*.js", "src/*.jsx", "src/views/*.jsx"],
    cwd=ROOT, text=True).splitlines()


@pytest.mark.parametrize("name", SOURCES)
def test_repository_corpus(name: str) -> None:
    source = (ROOT / name).read_text(encoding="utf-8")
    result = minify_js(source)
    assert result.count("\n") == source.count("\n")
    assert not comment_spans(result)
    assert minify_js(result) == result
    assert len(result) <= len(source)
