"""Read and write targets from literal arguments in Codex exec programs."""
from __future__ import annotations

import re
from collections.abc import Iterator
from enum import Enum, auto

from backend.bash_argv import _INLINE_FLAGS, _PY_PROG, _SHELL_PROGS
from backend.bash_churn import BashCommand
from backend.bash_reads import scan_command
from backend.parse_codex_churn import _codex_json_spans

_SHELL_CALL = re.compile(r"tools\.(exec_command|monitor)\s*\(\s*(?=\{)")
_FIELD = re.compile(r'''\s*(?:"([A-Za-z_][A-Za-z_0-9]*)"|'([A-Za-z_][A-Za-z_0-9]*)'|([A-Za-z_][A-Za-z_0-9]*))\s*:\s*''')
_VALUE_START = re.compile(r"\A")
_STRING = re.compile(r'(?=")')
_PATCH_PATH = re.compile(r"^\*\*\* (Add File|Update File|Delete File|Move to):\s*(.*)$")
_PATCH_PREFIX = re.compile(r"(?:(?P<call>tools\.apply_patch\s*\()|[A-Za-z_$][A-Za-z_0-9$]*\s*=)\s*$")
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


class _Expect(Enum):
    OPERAND = auto()
    OPERATOR = auto()
    MEMBER = auto()


def _trivia_end(src: str, start: int) -> int:
    """Skip whitespace and complete JS comments; retain unterminated blocks."""
    offset = start
    while offset < len(src):
        if src[offset].isspace():
            offset += 1
        elif src.startswith("//", offset):
            end = re.search(r"[\r\n\u2028\u2029]", src[offset + 2:])
            offset = offset + 2 + end.end() if end else len(src)
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
    offset = start + 1
    while offset < len(src):
        if src[offset] == "\\":
            offset += 2
            continue
        if src[offset] == quote:
            return offset + 1
        offset += 1
    return len(src)


def _regex_end(src: str, start: int) -> int:
    """Bound a regex literal, retaining escapes and character-class slashes."""
    offset = start + 1
    in_class = False
    while offset < len(src):
        char = src[offset]
        if char in "\r\n\u2028\u2029":
            return len(src)
        if char == "\\":
            offset += 2
            continue
        if char == "[":
            in_class = True
        elif char == "]":
            in_class = False
        elif char == "/" and not in_class:
            end = re.search(r"[^\w$]", src[offset + 1:])
            return offset + 1 + end.start() if end else len(src)
        offset += 1
    return len(src)


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


def _object_fields(src: str, start: int) -> Iterator[str]:
    """Yield independent fields only after the entire object is safely bounded."""
    closers = {"{": "}", "[": "]", "(": ")"}
    stack: list[str] = []
    fields: list[str] = []
    field_start = start + 1
    offset = start + 1
    expect = _Expect.OPERAND
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
        offset, expect = _value_token_end(src, offset, expect)


def _literal_object(src: str, start: int) -> dict:
    """Mine supported JSON field values independently within one call object."""
    fields = {}
    for member in _object_fields(src, start):
        field = _FIELD.match(member)
        if field is None:
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


def _patch_targets(program: str) -> list[str]:
    """Literal patch destinations; a Move replaces its Update source."""
    writes = []
    for start, value, end in _codex_json_spans(program, _STRING):
        if not isinstance(value, str) or not value.startswith("*** Begin Patch"):
            continue
        after = _trivia_end(program, end)
        prefix = _PATCH_PREFIX.search(program, 0, start)
        if prefix is None:
            continue
        separators = ",)" if prefix["call"] else ",;"
        if after < len(program) and program[after] not in separators:
            continue
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


def program_targets(program: str) -> tuple[str | None, list[str], list[str]]:
    """Fold literal inner calls onto one wrapper row; any slice wins its kind."""
    kind = None
    reads: list[str] = []
    writes = _patch_targets(program) if "tools.apply_patch" in program else []
    for call in _SHELL_CALL.finditer(program):
        args = _literal_object(program, call.end())
        cwd = args.get("workdir")
        command = args.get("cmd") if call[1] == "exec_command" else _argv_command(args.get("command"))
        if not isinstance(command, str):
            continue
        call_kind, call_reads, call_writes = scan_command(
            BashCommand(command), cwd if isinstance(cwd, str) else "")
        if call_kind is not None and kind != "slice":
            kind = call_kind
        reads.extend(call_reads)
        writes.extend(call_writes)
    return kind, list(dict.fromkeys(reads)), list(dict.fromkeys(writes))
