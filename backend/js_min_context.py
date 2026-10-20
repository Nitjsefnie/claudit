"""JavaScript lexical goals and delimiter roles for the comment scanner.

Track statement/operand/value positions, control headers, object versus block
braces and declaration versus expression bodies. Refuse unresolved lexical
goals rather than classifying a slash from punctuation alone.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
import unicodedata


NEWLINES = "\r\n\u2028\u2029"
_CONTROLS = frozenset(("if", "while", "for", "with", "switch", "catch"))
_PREFIX_WORDS = frozenset(("return", "throw", "yield", "await", "typeof",
                           "instanceof", "new", "delete", "void", "in"))
_OTHER_START = "\u1885\u1886\u2118\u212e\u309b\u309c"
_OTHER_PART = "\u00b7\u0387\u19da\u200c\u200d" + "".join(map(chr, range(0x1369, 0x1372)))


def identifier_start(char: str) -> bool:
    return bool(char) and (char in "_$" + _OTHER_START
                           or unicodedata.category(char) in ("Lu", "Ll", "Lt", "Lm", "Lo", "Nl"))


def identifier_part(char: str) -> bool:
    return identifier_start(char) or bool(char) and (
        char in _OTHER_PART or unicodedata.category(char) in ("Mn", "Mc", "Nd", "Pc"))


def js_space(char: str) -> bool:
    return bool(char) and (char in "\t\v\f \ufeff" + NEWLINES or unicodedata.category(char) == "Zs")


@dataclass
class _Frame:
    closer: str
    kind: str
    after: str = "value"
    member: bool = False


class Context:
    def __init__(self, error: type[ValueError], expression: bool = False) -> None:
        self.error = error
        self.phase = "operand" if expression else "statement"
        self.frames: list[_Frame] = []
        self.pending: list[tuple[str, int, str]] = []
        self.body: Optional[tuple[str, str]] = None
        self.control = ""
        self.previous = ""
        self.restricted = ""
        self.line = False
        self.label = False
        self.async_statement = False
        self.default_export = False
        self.importing = False
        self.questions: list[int] = []
        self.cases: list[int] = []

    @property
    def expression_start(self) -> bool:
        if self.phase == "unknown":
            raise self.error("Unresolved JavaScript lexical context")
        return self.phase in ("statement", "operand")

    def newline(self) -> None:
        self.line = True
        if self.restricted in ("return", "throw", "break", "continue", "yield"):
            self.phase, self.restricted = "statement", ""

    def _member(self) -> bool:
        return bool(self.frames and self.frames[-1].member)

    def _word(self, token: str) -> None:
        if self.previous in (".", "?.") or token.startswith("#"):
            self.phase = "value"
            return
        if self._member():
            self.phase = "value"
            return
        if token in ("function", "class"):
            declaration = self.default_export or self.phase == "statement" or self.phase == "value" and self.line or (
                self.previous == "async" and self.async_statement)
            self.pending.append((token, len(self.frames), "statement" if declaration else "value"))
            self.default_export = False
            self.phase = "operand"
        elif token in _CONTROLS:
            self.control, self.phase = token, "statement"
        elif token in ("else", "do", "try", "finally"):
            self.phase = "statement"
        elif token in ("break", "continue"):
            self.restricted, self.phase = token, "unknown"
        elif token in _PREFIX_WORDS:
            self.phase = "operand"
            self.restricted = token if token in ("return", "throw", "yield") else ""
        else:
            self._other_word(token)

    def _other_word(self, token: str) -> None:
        if token == "of" and self.frames and self.frames[-1].kind == "control:for" and self.phase == "value":
            self.phase = "operand"
        elif token in ("const", "let", "var", "extends"):
            self.phase = "operand"
        elif token == "import":
            self.importing, self.phase = True, "operand"
        elif token == "default" and self.previous == "export":
            self.default_export, self.phase = True, "operand"
        elif token in ("case", "default"):
            self.cases.append(len(self.frames))
            self.phase = "operand"
        elif token == "export":
            self.phase = "statement"
        else:
            self.label = self.phase == "statement"
            self.async_statement = token == "async" and self.label
            self.phase = "value"

    def _open_paren(self) -> None:
        if self.previous == "import":
            self.importing = False
        if self.control:
            frame = _Frame(")", "control:" + self.control, "statement")
            self.control = ""
        elif self.pending and self.pending[-1][:2] == ("function", len(self.frames)):
            _, _, after = self.pending.pop()
            frame = _Frame(")", "parameters", after)
        else:
            frame = _Frame(")", "paren")
        self.frames.append(frame)
        self.phase = "operand"

    def _open_brace(self) -> None:
        if self.body:
            kind, after = self.body
            self.body = None
        elif self.pending and self.pending[-1][:2] == ("class", len(self.frames)):
            _, _, after = self.pending.pop()
            kind = "class"
        elif self.phase == "statement":
            kind, after = "block", "statement"
        elif self.previous == ")" and self.frames and self.frames[-1].kind in ("object", "class"):
            kind, after = "method", "value"
        elif self.previous == "static" and self.frames and self.frames[-1].kind == "class":
            kind, after = "method", "value"
        elif self.phase == "operand":
            kind, after = "object", "value"
        elif self.line:
            kind, after = "block", "statement"
        else:
            raise self.error("Unresolved JavaScript brace context")
        self.frames.append(_Frame("}", kind, after, kind in ("object", "class")))
        self.control = ""
        self.phase = "operand" if kind == "object" else "statement"

    def _close(self, token: str) -> None:
        if not self.frames or self.frames[-1].closer != token:
            raise self.error("Unmatched JavaScript delimiter " + token)
        frame = self.frames.pop()
        self.phase = frame.after
        if frame.kind == "parameters":
            self.body = ("function", frame.after)
            self.phase = "statement"
        if frame.kind == "method" and self.frames:
            self.frames[-1].member = True

    def _colon(self) -> None:
        depth = len(self.frames)
        if self.questions and self.questions[-1] == depth:
            self.questions.pop()
            self.phase = "operand"
        elif self.frames and self.frames[-1].kind == "object":
            self.frames[-1].member = False
            self.phase = "operand"
        elif self.label or self.cases and self.cases[-1] == depth:
            if self.cases and self.cases[-1] == depth:
                self.cases.pop()
            self.phase = "statement"
        else:
            self.phase = "unknown"

    def _punctuation(self, token: str) -> None:
        if token == "(":
            self._open_paren()
        elif token == "{":
            self._open_brace()
        elif token == "[":
            self.frames.append(_Frame("]", "bracket"))
            self.phase = "operand"
        elif token in (")", "]", "}"):
            self._close(token)
        elif token == ":":
            self._colon()
        else:
            self._operator(token)

    def _operator(self, token: str) -> None:
        if token == ";":
            in_for = self.frames and self.frames[-1].kind == "control:for"
            self.phase = "operand" if in_for else "statement"
            if self.frames and self.frames[-1].kind == "class":
                self.frames[-1].member = True
        elif token in (".", "?."):
            self.phase = "value"
            if self.previous == "import":
                self.importing = False
        elif token in ("++", "--"):
            if self.line:
                self.phase = "operand"
        elif token == "=>":
            self.body, self.phase = ("arrow", "value"), "operand"
        else:
            if token == "?":
                self.questions.append(len(self.frames))
            if self.frames and self.frames[-1].kind in ("object", "class"):
                self.frames[-1].member = token == ","
            self.phase = "operand"

    def take(self, token: str, kind: str) -> None:
        if self.default_export and token not in ("async", "function", "class"):
            self.default_export = False
        if self.body and token != "{":
            if self.body[0] != "arrow":
                raise self.error("Expected JavaScript function body")
            self.body = None
        if self.restricted in ("break", "continue") and kind == "word":
            self.previous, self.line = token, False
            return
        restricted = self.restricted
        self.restricted = ""
        if restricted in ("break", "continue") and token not in (";", "}"):
            raise self.error("Expected statement end after " + restricted)
        if kind == "word":
            self._word(token)
        elif kind == "value":
            self.phase = "statement" if self.importing and not self.frames else "value"
            if self.phase == "statement":
                self.importing = False
        else:
            self._punctuation(token)
        if token != ":" and kind != "word":
            self.label = False
        self.previous, self.line = token, False
