"""Bounded shell words; no expansion or command execution.

Keep quote provenance until consumers have decided whether a word is
literal. The command payloads and write effects those words feed —
sed, cp, perl, echo, printf — live in `bash_effects`.
"""
from __future__ import annotations

import re

MAX_LITERAL_CHARS = 100_000
NULL_SINKS = ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty")
_WORD = re.compile(r'''(?:'[^']*'|"(?:\\.|[^"\\])*"|\$\{[^}]*\}|\\[\s\S]|[^\s'"\\|;&<>()])+''')
_RUNTIME = re.compile(r'''\$|`|[*?\[]|^~''')
_PART = re.compile(r'''('[^']*'|"(?:\\.|[^"\\])*"|\\[\s\S]|[^'"\\]+)''')
_QUOTING = re.compile(r'''['"\\\x00]''')
_BRACE_RANGE = re.compile(r"(?:-?[0-9]+\.\.-?[0-9]+|[a-zA-Z]\.\.[a-zA-Z])(?:\.\.-?[0-9]+)?")


class ShellWord(str):
    """A decoded word with its expansion and operator provenance attached."""

    literal: bool
    operator: bool
    expansion: str
    unquoted_expansion: bool
    double_paren: bool

    def __new__(cls, value: str, literal: bool = True,
                operator: bool = False) -> ShellWord:
        word = super().__new__(cls, value)
        word.literal = literal
        word.operator = operator
        word.expansion = value
        word.unquoted_expansion = False
        word.double_paren = False
        return word


def _decode_word(raw: str) -> ShellWord:
    """Decode shell quoting without treating single quotes inside double quotes as protection."""
    # Most words are already decoded. Avoid allocating regex matches and a
    # parts list for them, but retain the same expansion provenance as below.
    if not _QUOTING.search(raw):
        word = ShellWord(raw, not bool(_RUNTIME.search(raw)))
        word.unquoted_expansion = "$" in raw or "`" in raw
        return word
    parts: list[str] = []
    literal = True
    unquoted_expansion = False
    for match in _PART.finditer(raw):
        part = match.group()
        if part.startswith("'"):
            parts.append(part[1:-1].replace("$", "\x00"))
        elif part.startswith('"'):
            value = re.sub(r'\\([$`"\\\n])', lambda m: "\x00" if m[1] == "$" else ("" if m[1] == "\n" else m[1]), part[1:-1])
            literal &= not bool(re.search(r"[$`]", value))
            parts.append(value)
        elif part.startswith("\\"):
            parts.append("\x00" if part[1:] == "$" else ("" if part[1:] == "\n" else part[1:]))
        else:
            literal &= not bool(_RUNTIME.search(part))
            unquoted_expansion |= bool(re.search(r"[$`]", part))
            parts.append(part)
    expansion = "".join(parts)
    word = ShellWord(expansion.replace("\x00", "$"), literal)
    word.expansion = expansion
    word.unquoted_expansion = unquoted_expansion
    return word


def _brace_expands(raw: str) -> bool:
    """Detect unquoted brace lists/ranges without enumerating their results."""
    if "{" not in raw:
        return False
    # Quoted/escaped punctuation cannot delimit a brace expansion, even
    # when it is only part of an otherwise unquoted word.
    syntax = _PART.sub(lambda m: "_" if m[0][0] in "'\"\\" else m[0], raw)
    opens: list[tuple[int, bool]] = []
    for idx, char in enumerate(syntax):
        if char == "{":
            opens.append((idx, False))
        elif char == "," and opens:
            opens[-1] = (opens[-1][0], True)
        elif char == "}" and opens:
            start, comma = opens.pop()
            if comma or _BRACE_RANGE.fullmatch(syntax, start + 1, idx):
                return True
    return False


def _mark_adjacent_parens(token: ShellWord, command: str, idx: int,
                          end: int) -> None:
    """Flag a `(` adjacent to a second `(` and a `)` adjacent to a `)`.

    `((` opens and `))` closes bash's arithmetic command; spaced apart,
    the same parens are a real nested subshell (#771), and the adjacent
    opener with an undoubled close is bash's subshell fallback (#778).
    """
    if token == "(" and end < len(command) and command[end] == "(":
        token.double_paren = True
    elif token == ")" and idx and command[idx - 1] == ")":
        token.double_paren = True


def shell_tokens(command: str) -> list[ShellWord]:
    """Tokenize a small shell grammar; malformed/unsupported syntax is empty."""
    if "\x00" in command:
        return []
    tokens: list[ShellWord] = []
    idx = 0
    while idx < len(command):
        char = command[idx]
        if char in " \t\r":
            idx += 1
            continue
        if char == "#":
            end = command.find("\n", idx)
            idx = len(command) if end < 0 else end
            continue
        if char in "\n|;&<>()":
            end = idx + 1
            if command[idx:end + 2] == "&>>":
                end += 2
            elif command[idx:end + 1] in ("&&", "||", ">>", "<<", "|&", ">&", "<&", ">|", "&>"):
                end += 1
            operator = ";" if char == "\n" else command[idx:end]
            if char in "<>" and idx and command[idx - 1].isdigit() and tokens and tokens[-1].isdigit():
                operator = tokens.pop() + operator
            token = ShellWord(operator, operator=True)
            _mark_adjacent_parens(token, command, idx, end)
            tokens.append(token)
            idx = end
            continue
        match = _WORD.match(command, idx)
        if not match:
            return []
        raw = match.group()
        if _brace_expands(raw):
            # Refuse the unsupported command rather than treating expansion
            # syntax as literal paths or evaluating shell-produced words.
            return []
        if raw in ("{", "}"):
            tokens.append(ShellWord(raw, operator=True))
        else:
            tokens.append(_decode_word(raw))
        idx = match.end()
    return tokens


def literal_path(word: str) -> bool:
    """An explicit file operand, including extensionless and dot filenames."""
    return bool(word and word != "-" and word not in NULL_SINKS
                and not getattr(word, "operator", False)
                and getattr(word, "literal", not bool(_RUNTIME.search(word))))


def _file_sink(word: str) -> bool:
    """Write intent can be known even when the destination path is not."""
    return bool(word and word != "-" and word not in NULL_SINKS
                and not getattr(word, "operator", False))


def _option_value(word: str, offset: int) -> ShellWord:
    value = ShellWord(word[offset:], getattr(word, "literal", True))
    value.expansion = getattr(word, "expansion", word)[offset:]
    value.unquoted_expansion = getattr(word, "unquoted_expansion", False)
    return value


def _short_options(word: str, modes: dict[str, int]) -> list[tuple[str, str | None]]:
    options: list[tuple[str, str | None]] = []
    for offset, char in enumerate(word[1:], 2):
        flag = "-" + char
        mode = modes[flag]  # unsupported flags refuse the command
        value = _option_value(word, offset) if mode else None
        options.append((flag, value))
        if mode:
            break
    return options


def command_options(args: list[str], modes: dict[str, int]
                    ) -> tuple[list[tuple[str, str | None]], list[str]]:
    """Parse known options: mode 0 flag, 1 required value, 2 attached optional.

    Unknown options raise ValueError; callers must refuse to infer operands.
    Values retain quote provenance, including attached --option=value forms.
    """
    if any(getattr(word, "operator", False) for word in args):
        raise ValueError("shell operator is not a command operand")
    options: list[tuple[str, str | None]] = []
    operands: list[str] = []
    idx = 0
    try:
        while idx < len(args):
            word = args[idx]
            idx += 1
            if word == "--":
                operands.extend(args[idx:])
                break
            if not word.startswith("-") or word == "-":
                operands.append(word)
                continue
            if word.startswith("--"):
                flag, sep, _ = word.partition("=")
                if sep and modes[flag] == 0:
                    raise ValueError("flag does not take a value")
                parsed = [(flag, _option_value(word, len(flag) + 1) if sep else None)]
            else:
                parsed = _short_options(word, modes)
            for flag, value in parsed:
                if modes[flag] == 1 and not value:
                    value = args[idx]
                    idx += 1
                options.append((flag, value))
    except (KeyError, IndexError) as exc:
        raise ValueError("unknown option or missing option value") from exc
    return options, operands
