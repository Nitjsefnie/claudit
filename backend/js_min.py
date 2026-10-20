"""Strip JavaScript/JSX comments while preserving code and line structure.

Collapse spaces/tabs outside literals and JSX text, retaining a separator
where a comment separated tokens. Preserve all line terminators, string and
regex bytes, template raw parts and JSX text; scan template interpolations
and JSX expression containers as JavaScript. Reject unterminated constructs.

Regex/JSX entry follows the previous significant token's expression-start
reading, including arrow bodies. A closing brace permits a regex (a block
may precede one); division immediately after an object literal can therefore
be misread as a regex. Parenthesizing that object removes the ambiguity.
JSX begins only at expression start with a tag name or fragment opener;
after an identifier, number, closing paren or bracket, '<' is comparison.
This is a lexical transform, not a JavaScript grammar validator.
"""
from __future__ import annotations

import re


_NEWLINES = "\r\n\u2028\u2029"
_WORD = re.compile(r"[\w$]+")
_JSX_NAME = re.compile(r"[\w$][\w$.:-]*")
_PUNCTUATION = re.compile(
    r"===|!==|>>>=|\*\*=|&&=|\|\|=|\?\?=|>>>|<<=|>>=|=>|==|!=|<=|>=|"
    r"\+\+|--|&&|\|\||\?\?|\?\.|\*\*|<<|>>|\+=|-=|\*=|/=|%=|&=|\|=|\^=|\.\.\.")
_PREFIX_WORDS = frozenset((
    "return", "typeof", "instanceof", "new", "delete", "void", "case",
    "in", "of", "do", "else", "yield", "await", "throw"))
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
        if self.source.startswith("//", start):
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
            elif char in _NEWLINES and not jsx:
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
                flags = _WORD.match(self.source, self.pos)
                self._mark(start, flags.end() if flags else self.pos)
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
        match = _JSX_NAME.match(self.source, self.pos)
        if not match:
            raise JsMinError("Expected JSX tag name")
        self.pos = match.end()
        return match.group()

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
        while self.pos < len(self.source) and self.source[self.pos].isspace():
            self.pos += 1
        if closing != name or self.source[self.pos:self.pos + 1] != ">":
            raise JsMinError("Mismatched JSX closing tag")
        self.pos += 1

    def _jsx(self) -> None:
        self.pos += 1
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

    def _token(self, expression_start: bool) -> str:
        char = self.source[self.pos]
        if char in "'\"":
            self._quoted()
            return "value"
        if char == "`":
            self._template()
            return "value"
        if char == "/" and expression_start:
            self._regex()
            return "value"
        if char == "<" and expression_start and (
                self.source[self.pos + 1:self.pos + 2] == ">"
                or _JSX_NAME.match(self.source, self.pos + 1)):
            self._jsx()
            return "value"
        match = _WORD.match(self.source, self.pos) or _PUNCTUATION.match(self.source, self.pos)
        if match:
            self.pos = match.end()
            return match.group()
        self.pos += 1
        return char

    def javascript(self, container: bool = False) -> None:
        depth = 0
        expression_start = True
        previous = ""
        while self.pos < len(self.source):
            char = self.source[self.pos]
            if char.isspace():
                self.pos += 1
                continue
            if self.source.startswith(("//", "/*"), self.pos):
                self._comment()
                continue
            token = self._token(expression_start)
            if token == "}":
                if container and depth == 0:
                    return
                depth -= 1
            elif token == "{":
                depth += 1
            expression_start = (
                token in _PREFIX_WORDS and previous not in (".", "?.")
                if _WORD.fullmatch(token)
                else token not in (")", "]", ".", "?.", "++", "--"))
            previous = token
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
        if self.pending and self.on_line and text[0] not in _NEWLINES:
            self.parts.append(" ")
        self.pending = False
        self.parts.append(text)
        self.on_line = text[-1] not in _NEWLINES

    def add(self, text: str, kind: str = "code") -> None:
        if kind == "literal":
            self._literal(text)
            return
        if kind in ("line", "block"):
            text = " " + "".join(char for char in text if char in _NEWLINES)
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
