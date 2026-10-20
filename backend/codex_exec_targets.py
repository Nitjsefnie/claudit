"""Read and write targets from literal arguments in Codex exec programs."""
from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Iterator
from enum import Enum, auto
from typing import NamedTuple

from backend.bash_argv import _INLINE_FLAGS, _PY_PROG, _SHELL_PROGS
from backend.bash_churn import BashCommand, MAX_COMMAND_CHARS
from backend.bash_reads import scan_command
from backend.parse_codex_churn import _codex_json_spans

_FIELD = re.compile(r'''\s*(?:"([A-Za-z_][A-Za-z_0-9]*)"|'([A-Za-z_][A-Za-z_0-9]*)'|([A-Za-z_][A-Za-z_0-9]*))\s*:\s*''')
_VALUE_START = re.compile(r"\A")
_PATCH_PATH = re.compile(r"^\*\*\* (Add File|Update File|Delete File|Move to):\s*(.*)$")
_IDENTIFIER = re.compile(r"(?:[^\W\d]|[$])[\w$]*")
_WORD = re.compile(r"(?:[^\W\d]|[$])[\w$]*|[0-9][A-Za-z_0-9.]*")
_PUNCTUATOR = re.compile(
    r"\.\.\.|\?\.(?![0-9])|>>>=|===|!==|>>>|\*\*=|<<=|>>=|&&=|\|\|=|\?\?="
    r"|\+\+|--|\*\*|<<|>>|&&|\|\||\?\?|=>|[<>=!+\-*/%&|^]="
    r"|[{}()[\].;,<>+\-*/%&|^!~?:=]"
)
_EXPRESSION_WORDS = frozenset({
    "return", "throw", "case", "delete", "void", "typeof",
    "instanceof", "in", "of", "yield", "await", "new",
})
_CONTROL_WORDS = frozenset({"if", "while", "for", "with", "switch", "catch"})
_LINE_END = re.compile(r"[\r\n\u2028\u2029]")
_REGEX_FLAGS_END = re.compile(r"[^\w$]")
_SUPPORTED_API = re.compile(r"\b(?:exec_command|monitor|apply_patch)\b")
_BARE_COMMAND = re.compile(r"[A-Za-z0-9_./-]*\Z")
_JSON_STRING = r'"(?:[^"\\\r\n\u2028\u2029]|\\.)*"'
_JSON_VALUE = (
    rf'(?:{_JSON_STRING}|\[\s*(?:{_JSON_STRING}(?:\s*,\s*{_JSON_STRING})*)?\s*\]'
    r'|true|false|null|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)'
)
_JSON_FIELD = rf'"[A-Za-z_][A-Za-z_0-9]*"\s*:\s*{_JSON_VALUE}'
_DIRECT_SHELL_CALL = re.compile(
    rf'\s*(?:await\s+)?tools\.(exec_command|monitor)\s*\(\s*'
    rf'(\{{\s*(?:{_JSON_FIELD}(?:\s*,\s*{_JSON_FIELD})*)?\s*\}})\s*\)\s*;?\s*',
    re.DOTALL,
)
_JSON_DECODER = json.JSONDecoder()
_MAX_DIRECT_CHARS = 4096


class _Expect(Enum):
    OPERAND = auto()
    OPERATOR = auto()
    MEMBER = auto()


class _Token(NamedTuple):
    text: str
    start: int
    end: int


@dataclasses.dataclass
class _LexContext:
    expect: _Expect = _Expect.OPERAND
    control: str = ""
    parentheses: list[bool] = dataclasses.field(default_factory=list)

    def consume(self, src: str, offset: int) -> int:
        """A control condition closes into a statement; other parentheses end operands."""
        member = self.expect is _Expect.MEMBER
        pending = self.control
        end, self.expect = _value_token_end(src, offset, self.expect)
        token = src[offset:end]
        if token == "(":
            self.parentheses.append(bool(pending))
        elif token == ")" and self.parentheses and self.parentheses.pop():
            self.expect = _Expect.OPERAND
        self.control = token if token in _CONTROL_WORDS and not member else ""
        if token == "await" and pending == "for":
            self.control = pending
        return end


def _trivia_end(src: str, start: int) -> int:
    """Skip whitespace and complete JS comments; retain unterminated blocks."""
    offset = start
    while offset < len(src):
        if src[offset].isspace():
            offset += 1
        elif src.startswith("//", offset):
            end = _LINE_END.search(src, offset + 2)
            offset = end.end() if end else len(src)
        elif src.startswith("/*", offset):
            end = src.find("*/", offset + 2)
            if end < 0:
                return offset
            offset = end + 2
        else:
            break
    return offset


def _literal_value(src: str) -> object:
    """Accept a JSON value only when its entire expression is literal."""
    src = src[_trivia_end(src, 0):]
    decoded = next(_codex_json_spans(src, _VALUE_START), None)
    if decoded is None:
        return None
    _, value, end = decoded
    return value if _trivia_end(src, end) == len(src) else None


def _quoted_end(src: str, start: int) -> int:
    """Skip an escaped JS string or template without interpreting its contents."""
    quote = src[start]
    if quote == "`":
        return _template_end(src, start)
    offset = start + 1
    while offset < len(src):
        if src[offset] == "\\":
            offset += 2
            continue
        if src[offset] == quote:
            return offset + 1
        if quote != "`" and src[offset] in "\r\n\u2028\u2029":
            return len(src) + 1
        offset += 1
    return len(src) + 1


def _template_end(src: str, start: int) -> int:
    """Skip nested templates and interpolation with an iterative delimiter stack."""
    stack = ["`"]
    offset = start + 1
    context = _LexContext()
    while stack and offset < len(src):
        if stack[-1] == "`":
            if src[offset] == "\\":
                offset += 2
            elif src.startswith("${", offset):
                stack.append("}")
                context.expect = _Expect.OPERAND
                offset += 2
            elif src[offset] == "`":
                stack.pop()
                context.expect = _Expect.OPERATOR
                offset += 1
            else:
                offset += 1
            continue
        offset = _trivia_end(src, offset)
        if offset >= len(src) or src.startswith("/*", offset):
            return len(src) + 1
        if src[offset] in "{`":
            stack.append("}" if src[offset] == "{" else "`")
            context.expect = _Expect.OPERAND
            offset += 1
        elif src[offset] == "}":
            stack.pop()
            context.expect = _Expect.OPERATOR
            offset += 1
        else:
            offset = context.consume(src, offset)
    return offset if not stack else len(src) + 1


def _regex_end(src: str, start: int) -> int:
    """Bound a regex literal, retaining escapes and character-class slashes."""
    offset = start + 1
    in_class = False
    while offset < len(src):
        char = src[offset]
        if char in "\r\n\u2028\u2029":
            return len(src) + 1
        if char == "\\":
            offset += 2
            continue
        if char == "[":
            in_class = True
        elif char == "]":
            in_class = False
        elif char == "/" and not in_class:
            end = _REGEX_FLAGS_END.search(src, offset + 1)
            return end.start() if end else len(src)
        offset += 1
    return len(src) + 1


def _value_token_end(src: str, offset: int, expect: _Expect) -> tuple[int, _Expect]:
    """Consume one token; its role determines the next slash's lexical goal."""
    char = src[offset]
    if char in "\"'`":
        return _quoted_end(src, offset), _Expect.OPERATOR
    if char == "/" and expect is _Expect.OPERAND:
        return _regex_end(src, offset), _Expect.OPERATOR
    word = _WORD.match(src, offset)
    if word is not None:
        next_expect = (_Expect.OPERAND if expect is not _Expect.MEMBER and word[0] in _EXPRESSION_WORDS
                       else _Expect.OPERATOR)
        return word.end(), next_expect
    punctuator = _PUNCTUATOR.match(src, offset)
    if punctuator is None:
        return offset + 1, _Expect.OPERATOR
    token = punctuator[0]
    if token in ("++", "--"):
        return punctuator.end(), expect
    next_expect = _Expect.OPERAND
    if token in (".", "?."):
        next_expect = _Expect.MEMBER
    elif token in ("}", "]", ")"):
        next_expect = _Expect.OPERATOR
    return punctuator.end(), next_expect


def _program_tokens(src: str) -> list[_Token]:
    """Keep opaque literals whole and discard comments before discovering calls."""
    tokens = []
    offset = 0
    context = _LexContext()
    while offset < len(src):
        offset = _trivia_end(src, offset)
        if offset == len(src):
            break
        if src.startswith("/*", offset):
            return []
        end = context.consume(src, offset)
        if end > len(src):
            return []
        tokens.append(_Token(src[offset:end], offset, end))
        offset = end
    return tokens


def _delimiter_pairs(tokens: list[_Token]) -> dict[int, int]:
    """Index balanced groups, rejecting the entire program on broken boundaries."""
    pairs = {}
    stack = []
    closers = {"{": "}", "[": "]", "(": ")"}
    for index, token in enumerate(tokens):
        if token.text in closers:
            stack.append(index)
        elif token.text in ("}", "]", ")"):
            if not stack:
                return {}
            opener = stack.pop()
            if closers[tokens[opener].text] != token.text:
                return {}
            pairs[opener] = index
    return {} if stack else pairs


def _level_indices(pairs: dict[int, int], start: int, stop: int) -> Iterator[int]:
    index = start
    while index < stop:
        yield index
        index = pairs.get(index, index) + 1


def _tool_calls(tokens: list[_Token], pairs: dict[int, int]) -> Iterator[tuple[str, int, int]]:
    """Find real tools calls, including nested calls, with complete first arguments."""
    for index in range(len(tokens) - 4):
        if tokens[index].text != "tools" or (index and tokens[index - 1].text in (".", "?.")):
            continue
        member, name, opener = (token.text for token in tokens[index + 1:index + 4])
        if member != "." or name not in ("exec_command", "monitor", "apply_patch") or opener != "(":
            continue
        close = pairs.get(index + 3)
        if close is None:
            continue
        start = index + 4
        stop = next((i for i in _level_indices(pairs, start, close)
                     if tokens[i].text == ","), close)
        if start < stop:
            yield name, start, stop


def _object_fields(src: str, start: int) -> Iterator[str]:
    """Yield independent fields only after the entire object is safely bounded."""
    closers = {"{": "}", "[": "]", "(": ")"}
    stack: list[str] = []
    fields: list[str] = []
    field_start = start + 1
    offset = start + 1
    context = _LexContext()
    while offset < len(src):
        char = src[offset]
        if char.isspace():
            offset += 1
            continue
        if src.startswith(("//", "/*"), offset):
            end = _trivia_end(src, offset)
            if end == offset:
                return
            if not src[field_start:offset].strip():
                field_start = end
            offset = end
            continue
        if char in closers:
            stack.append(closers[char])
        elif char == "}" and not stack:
            yield from fields
            yield src[field_start:offset]
            return
        elif char in "}])":
            if not stack or stack.pop() != char:
                return
        elif char == "," and not stack:
            fields.append(src[field_start:offset])
            field_start = offset + 1
        offset = context.consume(src, offset)


def _literal_object(src: str, start: int) -> dict:
    """Mine supported JSON field values independently within one call object."""
    fields = {}
    for member in _object_fields(src, start):
        field = _FIELD.match(member)
        if field is None:
            words = [token.text for token in _program_tokens(member)]
            while words and words[0] in ("get", "set", "async", "*"):
                words = words[1:]
            if words and words[0] in ("...", "["):
                fields.clear()
            elif words and words[0].strip("'\"") in fields:
                fields[words[0].strip("'\"")] = None
            continue
        key = next(key for key in field.groups() if key is not None)
        if key in ("cmd", "command", "workdir"):
            fields[key] = _literal_value(member[field.end():])
    return fields


def _argv_command(argv: object) -> str:
    """Expose only positional inline shell/Python payloads, as argv_churn does."""
    if not isinstance(argv, list) or not argv or not all(isinstance(t, str) for t in argv):
        return ""
    prog = argv[0].rsplit("/", 1)[-1]
    for index, token in enumerate(argv[1:], start=1):
        if token not in _INLINE_FLAGS or index + 1 >= len(argv):
            continue
        if prog in _SHELL_PROGS or _PY_PROG.match(prog):
            return argv[index + 1]
        return ""
    return ""


def _patch_bindings(tokens: list[_Token], pairs: dict[int, int],
                    calls: list[tuple[str, int, int]]) -> dict[str, tuple[int, str]]:
    """Resolve simple top-level declarations used only as bare patch arguments."""
    if any(token.text.startswith("`") and "${" in token.text for token in tokens):
        return {}
    candidates = {}
    references: dict[str, set[int]] = {}
    for name, start, stop in calls:
        if name == "apply_patch" and stop == start + 1:
            references.setdefault(tokens[start].text, set()).add(start)
    for index in _level_indices(pairs, 0, len(tokens) - 4):
        if (tokens[index].text not in ("const", "let", "var")
                or tokens[index + 2].text != "=" or tokens[index + 4].text != ";"):
            continue
        name = tokens[index + 1].text
        value = _literal_value(tokens[index + 3].text)
        if _IDENTIFIER.fullmatch(name) and isinstance(value, str):
            candidates[name] = (index + 1, value)
    occurrences: dict[str, set[int]] = {}
    for index, token in enumerate(tokens):
        if token.text in candidates:
            occurrences.setdefault(token.text, set()).add(index)
    return {name: (index, value) for name, (index, value) in candidates.items()
            if occurrences[name] == references.get(name, set()) | {index}}


def _patch_targets(value: object) -> list[str]:
    """Literal patch destinations; a Move replaces its Update source."""
    if not isinstance(value, str) or not value.startswith("*** Begin Patch"):
        return []
    writes = []
    pending = None
    for line in value.splitlines():
        match = _PATCH_PATH.match(line)
        if match is None:
            continue
        operation, path = match.groups()
        path = path.strip()
        if operation == "Move to":
            pending = path or None
            continue
        if pending:
            writes.append(pending)
        pending = path or None
    if pending:
        writes.append(pending)
    return writes


def _call_patch(program: str, tokens: list[_Token], start: int, stop: int,
                bindings: dict[str, tuple[int, str]]) -> object:
    if stop == start + 1 and tokens[start].text in bindings:
        declaration, value = bindings[tokens[start].text]
        return value if declaration < start else None
    return _literal_value(program[tokens[start].start:tokens[stop - 1].end])


def _shell_targets(args: dict, name: str) -> tuple[str | None, list[str], list[str]]:
    cwd = args.get("workdir")
    command = args.get("cmd") if name == "exec_command" else _argv_command(args.get("command"))
    if (not isinstance(command, str) or len(command) > MAX_COMMAND_CHARS
            or _BARE_COMMAND.fullmatch(command.strip())):
        return None, [], []
    return scan_command(BashCommand(command), cwd if isinstance(cwd, str) else "")


def _scanned_targets(program: str) -> tuple[str | None, list[str], list[str]]:
    """Fold literal inner calls onto one wrapper row; any slice wins its kind."""
    kind = None
    reads: list[str] = []
    writes: list[str] = []
    tokens = _program_tokens(program)
    pairs = _delimiter_pairs(tokens)
    calls = list(_tool_calls(tokens, pairs))
    bindings = _patch_bindings(tokens, pairs, calls)
    for name, start, stop in calls:
        if name == "apply_patch":
            writes.extend(_patch_targets(_call_patch(program, tokens, start, stop, bindings)))
            continue
        if tokens[start].text != "{" or pairs.get(start) != stop - 1:
            continue
        call_kind, call_reads, call_writes = _shell_targets(_literal_object(program, tokens[start].start), name)
        if call_kind is not None and kind != "slice":
            kind = call_kind
        reads.extend(call_reads)
        writes.extend(call_writes)
    return kind, list(dict.fromkeys(reads)), list(dict.fromkeys(writes))


def program_targets(program: str) -> tuple[str | None, list[str], list[str]]:
    """Decode small, complete JSON calls directly; scan all other supported programs."""
    if "tools" not in program:
        return None, [], []
    direct = _DIRECT_SHELL_CALL.fullmatch(program) if len(program) <= _MAX_DIRECT_CHARS else None
    if direct is not None:
        try:
            args = _JSON_DECODER.decode(direct[2])
        except ValueError:
            pass
        else:
            return _shell_targets(args, direct[1])
    return _scanned_targets(program) if _SUPPORTED_API.search(program) else (None, [], [])
