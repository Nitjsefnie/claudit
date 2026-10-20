"""Read and write targets from literal arguments in Codex exec programs."""
from __future__ import annotations

import re
from collections.abc import Iterator

from backend.bash_argv import _INLINE_FLAGS, _PY_PROG, _SHELL_PROGS
from backend.bash_churn import BashCommand
from backend.bash_reads import scan_command
from backend.parse_codex_churn import _codex_json_values

_SHELL_CALL = re.compile(r"tools\.(exec_command|monitor)\s*\(\s*(?=\{)")
_FIELD = re.compile(r'''\s*(?:"([A-Za-z_][A-Za-z_0-9]*)"|'([A-Za-z_][A-Za-z_0-9]*)'|([A-Za-z_][A-Za-z_0-9]*))\s*:\s*''')
_VALUE_START = re.compile(r"\A")
_STRING = re.compile(r'(?=")')
_PATCH_PATH = re.compile(r"^\*\*\* (Add File|Update File|Delete File|Move to):\s*(.*)$")


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


def _object_fields(src: str, start: int) -> Iterator[str]:
    """Bound top-level fields to one call object, allowing non-JSON siblings."""
    closers = {"{": "}", "[": "]", "(": ")"}
    stack: list[str] = []
    field_start = start + 1
    offset = start + 1
    while offset < len(src):
        char = src[offset]
        if char in "\"'`":
            offset = _quoted_end(src, offset)
            continue
        if src.startswith(("//", "/*"), offset):
            line_comment = src.startswith("//", offset)
            end = src.find("\n" if line_comment else "*/", offset + 2)
            if end < 0:
                return
            end += 1 if line_comment else 2
            if not src[field_start:offset].strip():
                field_start = end
            offset = end
            continue
        if char in closers:
            stack.append(closers[char])
        elif char == "}" and not stack:
            yield src[field_start:offset]
            return
        elif char in "}])":
            if not stack or stack.pop() != char:
                return
        elif char == "," and not stack:
            yield src[field_start:offset]
            field_start = offset + 1
        offset += 1


def _literal_object(src: str, start: int) -> dict:
    """Mine supported JSON field values independently within one call object."""
    fields = {}
    for member in _object_fields(src, start):
        field = _FIELD.match(member)
        if field is None:
            continue
        key = next(key for key in field.groups() if key is not None)
        if key in ("cmd", "command", "workdir"):
            fields[key] = next(_codex_json_values(member[field.end():], _VALUE_START), None)
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
    for value in _codex_json_values(program, _STRING):
        if not isinstance(value, str) or not value.startswith("*** Begin Patch"):
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
