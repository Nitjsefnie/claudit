"""Strip JavaScript/JSX comments while preserving code and line structure.

Collapse spaces/tabs outside literals and JSX text, retaining a separator
where a comment separated tokens. Preserve all line terminators, string and
regex bytes, template raw parts and JSX text; scan template interpolations
and JSX expression containers as JavaScript. Reject unterminated constructs.

Track statement, operand and value contexts, control-header parentheses and
block/object/function/class braces to distinguish regexes from division.
JSX begins in an operand or statement position and permits whitespace around
tag names. Remove a leading hashbang as a line comment. Keep a separating
space when removing whitespace/comments would join a separate CR and LF.
Unsupported tokens and unresolved lexical contexts raise JsMinError; this
lexical transform does not validate the complete JavaScript grammar.
"""
from __future__ import annotations

import re

from backend.js_min_context import Context, NEWLINES, identifier_part, identifier_start, js_space


_NEWLINES = NEWLINES
_PUNCTUATION = re.compile(
    r"===|!==|>>>=|\*\*=|&&=|\|\|=|\?\?=|>>>|<<=|>>=|=>|==|!=|<=|>=|"
    r"\+\+|--|&&|\|\||\?\?|\?\.|\*\*|<<|>>|\+=|-=|\*=|/=|%=|&=|\|=|\^=|\.\.\.")
_NUMBER = re.compile(
    r"(?:0[xX][\da-fA-F](?:_?[\da-fA-F])*n?|0[bB][01](?:_?[01])*n?|"
    r"0[oO][0-7](?:_?[0-7])*n?|[0-9](?:_?[0-9])*n|"
    r"(?:[0-9](?:_?[0-9])*(?:\.[0-9_]*)?|\.[0-9](?:_?[0-9])*)"
    r"(?:[eE][+-]?[0-9](?:_?[0-9])*)?)")
_HORIZONTAL_OR_NEWLINE = re.compile(r"([ \t]+|[\r\n\u2028\u2029])")


class JsMinError(ValueError):
    """The scanner met source it cannot interpret."""


class _Scanner:
    def __init__(self, source: str) -> None:
        self.source = source
        self.pos = 0
        self.spans: list[tuple[int, int, str]] = []

    def _mark(self, start: int, end: int, kind: str = "literal") -> None:
        self.spans.append((start, end, kind))
        self.pos = end

    def _comment(self) -> None:
        start = self.pos
        if self.source.startswith(("//", "#!"), start):
            match = re.search(r"[\r\n\u2028\u2029]", self.source[start:])
            end = start + match.start() if match else len(self.source)
            self._mark(start, end, "line")
        else:
            end = self.source.find("*/", start + 2)
            if end < 0:
                raise JsMinError("Unterminated block comment")
            self._mark(start, end + 2, "block")

    def _quoted(self, jsx: bool = False) -> None:
        start = self.pos
        quote = self.source[start]
        self.pos += 1
        while self.pos < len(self.source):
            char = self.source[self.pos]
            if char == quote:
                self._mark(start, self.pos + 1)
                return
            if char == "\\" and not jsx:
                self.pos += 2
                if self.source[self.pos - 1:self.pos + 1] == "\r\n":
                    self.pos += 1
            elif char in "\r\n" and not jsx:
                raise JsMinError("Unescaped newline in string")
            else:
                self.pos += 1
        raise JsMinError("Unterminated string")

    def _regex(self) -> None:
        start = self.pos
        self.pos += 1
        in_class = False
        while self.pos < len(self.source):
            char = self.source[self.pos]
            if char in _NEWLINES:
                raise JsMinError("Unterminated regex")
            if char == "\\":
                if self.source[self.pos + 1:self.pos + 2] in tuple(_NEWLINES):
                    raise JsMinError("Newline in regex escape")
                self.pos += 2
                continue
            if char == "/" and not in_class:
                self.pos += 1
                while self.pos < len(self.source) and identifier_part(self.source[self.pos]):
                    self.pos += 1
                self._mark(start, self.pos)
                return
            if char == "[":
                in_class = True
            elif char == "]":
                in_class = False
            self.pos += 1
        raise JsMinError("Unterminated regex")

    def _template(self) -> None:
        start = self.pos
        self.pos += 1
        while self.pos < len(self.source):
            char = self.source[self.pos]
            if char == "`":
                self._mark(start, self.pos + 1)
                return
            if char == "\\":
                self.pos += 2
            elif self.source.startswith("${", self.pos):
                self._mark(start, self.pos + 2)
                self.javascript(container=True)
                start = self.pos
            else:
                self.pos += 1
        raise JsMinError("Unterminated template literal")

    def _tag_name(self) -> str:
        self._skip_space()
        start = self.pos
        if not identifier_start(self.source[self.pos:self.pos + 1]):
            raise JsMinError("Expected JSX tag name")
        self.pos += 1
        while self.pos < len(self.source) and (
                identifier_part(self.source[self.pos]) or self.source[self.pos] in ".:-"):
            self.pos += 1
        return self.source[start:self.pos]

    def _skip_space(self) -> None:
        while self.pos < len(self.source) and js_space(self.source[self.pos]):
            self.pos += 1

    def _attributes(self) -> bool:
        while self.pos < len(self.source):
            char = self.source[self.pos]
            if self.source.startswith("/>", self.pos):
                self.pos += 2
                return True
            if char == ">":
                self.pos += 1
                return False
            if char in "'\"":
                self._quoted(jsx=True)
            elif char == "{":
                self.pos += 1
                self.javascript(container=True)
            else:
                self.pos += 1
        raise JsMinError("Unterminated JSX opening tag")

    def _close_tag(self, name: str) -> None:
        self.pos += 2
        closing = self._tag_name() if name else ""
        self._skip_space()
        if closing != name or self.source[self.pos:self.pos + 1] != ">":
            raise JsMinError("Mismatched JSX closing tag")
        self.pos += 1

    def _jsx(self) -> None:
        self.pos += 1
        self._skip_space()
        if self.source[self.pos:self.pos + 1] == ">":
            name = ""
            self.pos += 1
        else:
            name = self._tag_name()
            if self._attributes():
                return
        while self.pos < len(self.source):
            if self.source.startswith("</", self.pos):
                self._close_tag(name)
                return
            char = self.source[self.pos]
            if char == "<":
                self._jsx()
            elif char == "{":
                self.pos += 1
                self.javascript(container=True)
            else:
                end = self.pos + 1
                while end < len(self.source) and self.source[end] not in "<{":
                    end += 1
                self._mark(self.pos, end)
        raise JsMinError("Unterminated JSX element")

    def _token(self, expression_start: bool) -> tuple[str, str]:
        char = self.source[self.pos]
        if char in "'\"":
            self._quoted()
            return "", "value"
        if char == "`":
            self._template()
            return "", "value"
        if char == "/" and expression_start:
            self._regex()
            return "", "value"
        if char == "<" and expression_start:
            self._jsx()
            return "", "value"
        return self._plain_token()

    def _plain_token(self) -> tuple[str, str]:
        start = self.pos
        char = self.source[start]
        if char == "\\":
            raise JsMinError("Unsupported escaped identifier")
        if char == "#" or identifier_start(char):
            if char == "#":
                self.pos += 1
            if not identifier_start(self.source[self.pos:self.pos + 1]):
                raise JsMinError("Unsupported private identifier")
            self.pos += 1
            while self.pos < len(self.source) and identifier_part(self.source[self.pos]):
                self.pos += 1
            return self.source[start:self.pos], "word"
        match = _NUMBER.match(self.source, start)
        if match:
            self.pos = match.end()
            return match.group(), "value"
        match = _PUNCTUATION.match(self.source, self.pos)
        if match:
            self.pos = match.end()
            return match.group(), "punctuation"
        if char not in "{}()[].,;:?~+-*/%<>=!&|^":
            raise JsMinError("Unsupported JavaScript token " + repr(char))
        self.pos += 1
        return char, "punctuation"

    def javascript(self, container: bool = False) -> None:
        context = Context(JsMinError, expression=container)
        while self.pos < len(self.source):
            char = self.source[self.pos]
            if js_space(char):
                if char in _NEWLINES:
                    context.newline()
                self.pos += 1
                continue
            if self.source.startswith(("//", "/*"), self.pos) or (
                    self.pos == 0 and self.source.startswith("#!")):
                start = self.pos
                self._comment()
                if any(char in _NEWLINES for char in self.source[start:self.pos]):
                    context.newline()
                continue
            if char == "}" and container and not context.frames:
                self.pos += 1
                return
            token, kind = self._token(context.expression_start if char in "/<" else False)
            context.take(token, kind)
        if container:
            raise JsMinError("Unterminated JavaScript expression container")


class _Output:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.pending = False
        self.on_line = False

    def _literal(self, text: str) -> None:
        if not text:
            return
        joins_crlf = bool(self.parts and self.parts[-1].endswith("\r") and text.startswith("\n"))
        if self.pending and (self.on_line and text[0] not in _NEWLINES or joins_crlf):
            self.parts.append(" ")
        self.pending = False
        self.parts.append(text)
        self.on_line = text[-1] not in _NEWLINES

    def add(self, text: str, kind: str = "code") -> None:
        if kind == "literal":
            self._literal(text)
            return
        if kind in ("line", "block"):
            text = " " + re.sub(r"[^\r\n\u2028\u2029]+", " ", text) + " "
        for part in _HORIZONTAL_OR_NEWLINE.split(text):
            if part and part[0] in " \t":
                self.pending = True
            else:
                self._literal(part)


def minify_js(source: str) -> str:
    """Return source with comments removed and horizontal whitespace collapsed."""
    scanner = _Scanner(source)
    scanner.javascript()
    output = _Output()
    start = 0
    for left, right, kind in scanner.spans:
        output.add(source[start:left])
        output.add(source[left:right], kind)
        start = right
    output.add(source[start:])
    return "".join(output.parts)


def comment_spans(source: str) -> list[tuple[int, int, str]]:
    """Return (start, end, kind) for each comment, using UTF-8 byte offsets."""
    scanner = _Scanner(source)
    scanner.javascript()
    comments: list[tuple[int, int, str]] = []
    previous = offset = 0
    for start, end, kind in scanner.spans:
        if kind == "literal":
            continue
        offset += len(source[previous:start].encode("utf-8"))
        length = len(source[start:end].encode("utf-8"))
        comments.append((offset, offset + length, kind))
        previous, offset = end, offset + length
    return comments
