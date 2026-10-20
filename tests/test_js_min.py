"""Comment stripping and lexical preservation, with hand-written goldens.

Every assertion fails on unfixed code by construction: backend.js_min did
not exist before this task. The corpus checks supplement the goldens;
an independent Espree AST comparison is run outside this Python suite.
"""
from __future__ import annotations

from pathlib import Path
import re
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
    assert re.findall(r"\r\n|[\r\n\u2028\u2029]", result) == re.findall(r"\r\n|[\r\n\u2028\u2029]", source)
    assert not comment_spans(result)
    assert minify_js(result) == result
    assert len(result) <= len(source)


@pytest.mark.parametrize("prefix", [
    "", "(", ",", "=", "label:", "[", "!", "&", "|", "?", "{", "if(ok){} ", ";",
    "return ", "typeof ", "instanceof ", "new ", "delete ", "void ",
    "case ", "in ", "do ", "else ", "yield ", "await ", "=> ",
])
def test_regex_expression_start_tokens(prefix: str) -> None:
    source = prefix + r"/a[/* ]\/b/gi; // gone"
    assert minify_js(source) == prefix.rstrip() + (
        " " if prefix.endswith(" ") else "") + r"/a[/* ]\/b/gi;"


@pytest.mark.parametrize("left", ["name", "42", "(name)", "[name]", "'x'", "`x`", "/x/"])
def test_division_after_expression(left: str) -> None:
    assert minify_js(left + " / 2; // gone") == left + " / 2;"


@pytest.mark.parametrize("prefix", [
    "", "= ", "( ", ", ", "[ ", "{ ", "; ", "label: ", "? ",
    "&& ", "|| ", "! ", "return ", "=> ",
])
def test_jsx_expression_start_tokens(prefix: str) -> None:
    source = prefix + "<Tag> // text /* text */ </Tag>; // gone"
    assert minify_js(source) == prefix + "<Tag> // text /* text */ </Tag>;"


@pytest.mark.parametrize("left", ["name", "42", "(name)", "[name]"])
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
    assert re.findall(r"\r\n|[\r\n\u2028\u2029]", result) == re.findall(r"\r\n|[\r\n\u2028\u2029]", source)
    assert not comment_spans(result)
    assert minify_js(result) == result
    assert len(result) <= len(source)


@pytest.mark.parametrize(("source", "expected"), [
    ("while (ok) /a  b/.test(s); // drop", "while (ok) /a  b/.test(s);"),
    ("if (ok) /[/*]/.test(s); // drop", "if (ok) /[/*]/.test(s);"),
    ("while (ok) {break\n/a  b/.test(s);}", "while (ok) {break\n/a  b/.test(s);}"),
    ("const x = {} / 2; // drop", "const x = {} / 2;"),
    ("const x = {} / 2 /* drop */ / 3;", "const x = {} / 2 / 3;"),
    ("const of = 4; const x = of / 2; // drop", "const of = 4; const x = of / 2;"),
    ("const of = 4; const x = of / /a  b/.source.length;",
     "const of = 4; const x = of / /a  b/.source.length;"),
    ("class A { #return=4; f(){return this.#return / 2;} }",
     "class A { #return=4; f(){return this.#return / 2;} }"),
    ("const a\u0301=4; const x=a\u0301 / 2;", "const a\u0301=4; const x=a\u0301 / 2;"),
    ("const x = a\ufeff / 2; // drop", "const x = a\ufeff / 2;"),
    ('const x = "a\u2028b"; // drop', 'const x = "a\u2028b";'),
    ("const x = 'a\u2029b'; // drop", "const x = 'a\u2029b';"),
    ("const x = < A>  // text </ A>; // drop", "const x = < A>  // text </ A>;"),
    ("a();\r/*x*/\nb();", "a();\r \nb();"),
    ("a();\r \nb();", "a();\r \nb();"),
    ("#!/usr/bin/env node\nconst x = 1; // drop", "\nconst x = 1;"),
    ("const x = obj.return / 2; // drop", "const x = obj.return / 2;"),
    ("const x = obj?.return / 2; // drop", "const x = obj?.return / 2;"),
    ('const s = "a\\\r\nb"; // drop', 'const s = "a\\\r\nb";'),
    (r'const e = <A x="a\" />;', r'const e = <A x="a\" />;'),
    (r'const s = "a\\"; // drop', r'const s = "a\\";'),
    ("if (ok) {} /[/* ]/.test(s); // drop", "if (ok) {} /[/* ]/.test(s);"),
    ("function f(){ return/* drop */(1); }", "function f(){ return (1); }"),
    ("const n = (a + b) / 2; // drop", "const n = (a + b) / 2;"),
    ('const t = ` // raw\n /* raw */ ${1 /* drop */ + 2}`;',
     'const t = ` // raw\n /* raw */ ${1 + 2}`;'),
    ('const e = <A>{`out ${`in ${<B>{1 /* drop */}</B>}`}`}</A>;',
     'const e = <A>{`out ${`in ${<B>{1 }</B>}`}`}</A>;'),
    ("const e = <> /* text */ <B />{1 /* drop */}</>;",
     "const e = <> /* text */ <B />{1 }</>;"),
    ('const e = <A title={`raw // ${1 /* drop */}`} />;',
     'const e = <A title={`raw // ${1 }`} />;'),
    ('a();\n//# sourceMappingURL=a.js.map\nb();', 'a();\n\nb();'),
    ('function f(){return // drop\n{a:1};}', 'function f(){return\n{a:1};}'),
    ('a();// drop\u2028b();/* drop\u2029*/c();', 'a();\u2028b();\u2029c();'),
    (r'const r = /[[a]--[b]]/v; // drop', r'const r = /[[a]--[b]]/v;'),
])
def test_review_regressions(source: str, expected: str) -> None:
    assert minify_js(source) == expected
    assert re.findall(r"\r\n|[\r\n\u2028\u2029]", expected) == re.findall(r"\r\n|[\r\n\u2028\u2029]", source)
    assert not comment_spans(expected)
    assert minify_js(expected) == expected


@pytest.mark.parametrize(("source", "message"), [
    ("< A>", "Unterminated JSX element"),
    ("if(ok) /abc", "Unterminated regex"),
    ("/a\\\nb/", "Newline in regex escape"),
    ("/a\\\rb/", "Newline in regex escape"),
    ("/a\\\u2028b/", "Newline in regex escape"),
    ("const x = @ / 2;", "Unsupported JavaScript token '@'"),
    (r"const \u0061=4;", "Unsupported escaped identifier"),
    (": /a  b/;", "Unresolved JavaScript lexical context"),
    ("x{} /a/;", "Unresolved JavaScript brace context"),
    ("function f() /a/;", "Expected JavaScript function body"),
    (") /a/;", "Unmatched JavaScript delimiter )"),
    ("a;#!bad", "Unsupported private identifier"),
    ("'abc", "Unterminated string"),
    ("/* abc", "Unterminated block comment"),
    ("`abc ${1}", "Unterminated template literal"),
    ("<A>", "Unterminated JSX element"),
    ("<>", "Unterminated JSX element"),
    ("<A>{1", "Unterminated JavaScript expression container"),
    ('<A x="a', "Unterminated string"),
])
def test_review_refusals(source: str, message: str) -> None:
    for transform in (minify_js, comment_spans):
        with pytest.raises(JsMinError, match="^" + re.escape(message) + "$"):
            transform(source)


@pytest.mark.parametrize(("source", "expected"), [
    ("for (const x of /a  b/.source) /[/*]/.test(x); // drop",
     "for (const x of /a  b/.source) /[/*]/.test(x);"),
    ("for (const of of values) { of / 2; } // drop",
     "for (const of of values) { of / 2; }"),
    ("for (let x = {} / 2; x; x--) /a  b/.test(s); // drop",
     "for (let x = {} / 2; x; x--) /a  b/.test(s);"),
    ("do /a  b/.test(s); while (ok); /[/*]/.test(s); // drop",
     "do /a  b/.test(s); while (ok); /[/*]/.test(s);"),
    ("label: { /a  b/.test(s); } /[/*]/.test(s); // drop",
     "label: { /a  b/.test(s); } /[/*]/.test(s);"),
    ("label: while (ok) {continue label\n/a  b/.test(s);}",
     "label: while (ok) {continue label\n/a  b/.test(s);}"),
    ("function f(){return /* x\n */ /a  b/;} // drop",
     "function f(){return\n/a  b/;}"),
    ("function* f(){yield\n/a  b/;} // drop", "function* f(){yield\n/a  b/;}"),
    ("async function f(){await\n/a  b/;} // drop", "async function f(){await\n/a  b/;}"),
    ("const f = function(){} / 2; // drop", "const f = function(){} / 2;"),
    ("const f =\nfunction(){} / 2; // drop", "const f =\nfunction(){} / 2;"),
    ("function f(){} /a  b/.test(s); // drop", "function f(){} /a  b/.test(s);"),
    ("const f = (() => {}) / 2; // drop", "const f = (() => {}) / 2;"),
    ("const A = class {} / 2; // drop", "const A = class {} / 2;"),
    ("const A =\nclass {} / 2; // drop", "const A =\nclass {} / 2;"),
    ("class A {} /a  b/.test(s); // drop", "class A {} /a  b/.test(s);"),
    ("const obj = {return: 4, f(){return /a  b/;}} / 2; // drop",
     "const obj = {return: 4, f(){return /a  b/;}} / 2;"),
    ("const x = ({} / 2) / /a  b/.source.length; // drop",
     "const x = ({} / 2) / /a  b/.source.length;"),
    ("const x = ok ? {} : {} / 2; // drop", "const x = ok ? {} : {} / 2;"),
    ('const x = < \tA \tname="//">  /* text */ </ \tA >; // drop',
     'const x = < A name="//">  /* text */ </ A >;'),
    ("a();\r\n/*x*/\r/*y*/\nb();\r\n", "a();\r\n\r \nb();\r\n"),
    ("a();/*\r x\n*/b();", "a();\r \nb();"),
    ("const a\u200c\u200d = 4; a\u200c\u200d / 2; // drop",
     "const a\u200c\u200d = 4; a\u200c\u200d / 2;"),
    ("const n = 1e+2 / 2; // drop", "const n = 1e+2 / 2;"),
    ("for await (const of of values) /a  b/.test(of); // drop",
     "for await (const of of values) /a  b/.test(of);"),
    ("const obj = {if(x){return /a  b/;}} / 2; // drop",
     "const obj = {if(x){return /a  b/;}} / 2;"),
    ("obj.if(x) / 2; obj.while(x) / 2; // drop",
     "obj.if(x) / 2; obj.while(x) / 2;"),
    ("const obj = {f(){}, x: {} / 2}; // drop", "const obj = {f(){}, x: {} / 2};"),
    ("try {} catch { /a  b/.test(s); } finally {} // drop",
     "try {} catch { /a  b/.test(s); } finally {}"),
    ("switch(x){case x ? 1 : 2: /a  b/.test(s); break; default: /[/*]/.test(s);}",
     "switch(x){case x ? 1 : 2: /a  b/.test(s); break; default: /[/*]/.test(s);}"),
    ("function f(){throw /a  b/;} // drop", "function f(){throw /a  b/;}"),
    ("const \u2118 = 4; const a\u00b7 = 4; a\u00b7 / 2; // drop",
     "const \u2118 = 4; const a\u00b7 = 4; a\u00b7 / 2;"),
    ("const x = a\u00a0 / 2; // drop", "const x = a\u00a0 / 2;"),
    ("export default function f(){} /a  b/.test(s); // drop",
     "export default function f(){} /a  b/.test(s);"),
    ("export default class A{} /a  b/.test(s); // drop",
     "export default class A{} /a  b/.test(s);"),
    ('import "m"\n/a  b/.test(s); // drop', 'import "m"\n/a  b/.test(s);'),
    ('import {x} from "m"\n/a  b/.test(s); // drop', 'import {x} from "m"\n/a  b/.test(s);'),
    ('const x = import("m") / 2; // drop', 'const x = import("m") / 2;'),
    ('const x = import.meta.value / 2; // drop', 'const x = import.meta.value / 2;'),
    ("class A {static { /a  b/.test(s); }} /[/*]/.test(s); // drop",
     "class A {static { /a  b/.test(s); }} /[/*]/.test(s);"),
])
def test_context_neighbours(source: str, expected: str) -> None:
    assert minify_js(source) == expected
    assert not comment_spans(expected)
    assert minify_js(expected) == expected


@pytest.mark.parametrize("keyword", ["if", "while", "for", "with", "switch", "catch"])
def test_control_header_regex(keyword: str) -> None:
    assert minify_js(keyword + " ((ok)) /a  b/.test(s); // drop") == keyword + " ((ok)) /a  b/.test(s);"


def test_hashbang_is_a_line_comment_span() -> None:
    assert comment_spans("#!/usr/bin/env node\nconst x = 1; // drop") == [
        (0, 19, "line"), (33, 40, "line")]
