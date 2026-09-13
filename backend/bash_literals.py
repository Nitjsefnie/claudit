"""Bounded shell words and literal output; no expansion or command execution.

Keep quote provenance until consumers have decided whether a word is literal.
Only printf and numeric-addressed sed append payloads are enumerable here.
"""
from __future__ import annotations

import posixpath
import re

MAX_LITERAL_CHARS = 100_000
NULL_SINKS = ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty")
_WORD = re.compile(r'''(?:'[^']*'|"(?:\\.|[^"\\])*"|\$\{[^}]*\}|\\[\s\S]|[^\s'"\\|;&<>(){}])+''')
_RUNTIME = re.compile(r'''\$|`|[*?\[]|^~''')
_PART = re.compile(r'''('[^']*'|"(?:\\.|[^"\\])*"|\\[\s\S]|[^'"\\]+)''')


class ShellWord(str):
    """A decoded word with its expansion and operator provenance attached."""

    literal: bool
    operator: bool
    expansion: str

    def __new__(cls, value: str, literal: bool = True,
                operator: bool = False) -> ShellWord:
        word = super().__new__(cls, value)
        word.literal = literal
        word.operator = operator
        word.expansion = value
        return word


def _decode_word(raw: str) -> ShellWord:
    """Decode shell quoting without treating single quotes inside double quotes as protection."""
    parts: list[str] = []
    literal = True
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
            parts.append(part)
    expansion = "".join(parts)
    word = ShellWord(expansion.replace("\x00", "$"), literal)
    word.expansion = expansion
    return word


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
        if char in "\n|;&<>(){}":
            end = idx + 1
            if command[idx:end + 1] in ("&&", "||", ">>", "<<", "|&", ">&", "<&"):
                end += 1
            operator = ";" if char == "\n" else command[idx:end]
            if char in "<>" and idx and command[idx - 1].isdigit() and tokens and tokens[-1].isdigit():
                operator = tokens.pop() + operator
            tokens.append(ShellWord(operator, operator=True))
            idx = end
            continue
        match = _WORD.match(command, idx)
        if not match:
            return []
        tokens.append(_decode_word(match.group()))
        idx = match.end()
    return tokens


def literal_path(word: str) -> bool:
    """An explicit file operand, including extensionless and dot filenames."""
    return bool(word and word != "-" and word not in NULL_SINKS
                and getattr(word, "literal", not bool(_RUNTIME.search(word))))


def _option_value(word: str, offset: int) -> ShellWord:
    value = ShellWord(word[offset:], getattr(word, "literal", True))
    value.expansion = getattr(word, "expansion", word)[offset:]
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


def sed_parts(args: list[str]) -> tuple[list[str], list[str], bool]:
    """Separate sed scripts, explicit file operands and in-place mode."""
    modes = {"-" + c: 0 for c in "nErusz"}
    modes.update(dict.fromkeys(("--quiet", "--silent", "--regexp-extended", "--posix", "--unbuffered"), 0))
    modes.update(dict.fromkeys(("-e", "-f", "--expression", "--file"), 1))
    modes.update(dict.fromkeys(("-i", "--in-place"), 2))
    try:
        options, files = command_options(args, modes)
    except ValueError:
        return [], [], False
    scripts = [value for flag, value in options
               if flag in ("-e", "--expression") and value is not None]
    external = any(flag in ("-f", "--file") for flag, _ in options)
    if external:
        scripts.append(ShellWord("", literal=False))
    elif not scripts and files:
        scripts.append(files.pop(0))
    in_place = any(flag in ("-i", "--in-place") for flag, _ in options)
    return scripts, files, in_place


def _printf_format(fmt: str) -> list[str | None] | None:
    """Decode supported format escapes and %s placeholders."""
    pieces: list[str | None] = []
    idx = 0
    escapes = {"n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b", "f": "\f", "v": "\v", "\\": "\\"}
    while idx < len(fmt):
        char = fmt[idx]
        idx += 1
        if char in "\\%":
            if idx == len(fmt):
                return None
            code = fmt[idx]
            idx += 1
            if char == "\\":
                if code not in escapes:
                    return None
                pieces.append(escapes[code])
            elif code == "s":
                pieces.append(None)
            elif code == "%":
                pieces.append("%")
            else:
                return None
        else:
            pieces.append(char)
    return pieces


def _printf(args: list[str]) -> str | None:
    if args and args[0] == "--":
        args = args[1:]
    if not args or any(not getattr(arg, "literal", True) for arg in args):
        return None
    fmt, *values = args
    if fmt.startswith("-") or sum(map(len, args)) > MAX_LITERAL_CHARS:
        return None
    pieces = _printf_format(fmt)
    if pieces is None:
        return None
    slots = pieces.count(None)
    rounds = max(1, (len(values) + slots - 1) // slots) if slots else 1
    if rounds * sum(len(p) for p in pieces if p is not None) + sum(map(len, values)) > MAX_LITERAL_CHARS:
        return None
    out: list[str] = []
    arg_idx = 0
    for _ in range(rounds):
        for piece in pieces:
            if piece is None:
                out.append(values[arg_idx] if arg_idx < len(values) else "")
                arg_idx += 1
            else:
                out.append(piece)
    return "".join(out)


def _sed_append(args: list[str]) -> tuple[str | None, bool]:
    scripts, files, in_place = sed_parts(args)
    if len(scripts) != 1 or not getattr(scripts[0], "literal", True):
        return None, False
    match = re.fullmatch(r"(?:[0-9]+)?a(?:[ \t]+|\\\n)([^\x00]*)", scripts[0])
    if not match:
        return None, False
    body = match.group(1)
    # Accept escaped newlines, including the traditional backslash-newline
    # form. Other escapes and unescaped program separators remain unknown.
    if ";" in body or re.search(r"(?<!\\)\n|\\(?!n|\n|\\)", body):
        return None, False
    body = re.sub(r"\\(n|\n|\\)", lambda m: "\\" if m[1] == "\\" else "\n", body)
    return body + "\n", in_place and any(literal_path(f) for f in files)


def _redirects(words: list[ShellWord]) -> tuple[list[ShellWord], bool | None, bool]:
    """Remove redirects, retaining stdout's sink and whether stdin is replaced."""
    args: list[ShellWord] = []
    sink = None
    stdin_replaced = False
    idx = 0
    while idx < len(words):
        word = words[idx]
        match = re.fullmatch(r"([0-9]*)(>>?|<|>&|<&)", word) if word.operator else None
        if match:
            if idx + 1 == len(words):
                return [], False, True
            fd, redirect = match.groups()
            if fd in ("", "1") and redirect in (">", ">>", ">&"):
                sink = redirect != ">&" and literal_path(words[idx + 1])
            if fd in ("", "0") and redirect in ("<", "<&"):
                stdin_replaced = True
            idx += 2
        else:
            args.append(word)
            idx += 1
    return args, sink, stdin_replaced


def _stage_payload(args: list[ShellWord], pending: str | None) -> tuple[str | None, bool]:
    name = posixpath.basename(args[0])
    if name == "printf":
        return _printf(list(args[1:])), False
    if name == "sed":
        return _sed_append(list(args[1:]))
    if name == "tee":
        try:
            _, files = command_options(list(args[1:]), {
                "-a": 0, "--append": 0, "-i": 0, "--ignore-interrupts": 0,
                "-p": 0, "--output-error": 2})
        except ValueError:
            return None, False
        return pending, any(literal_path(f) for f in files)
    return None, False


def shell_payloads(command: str) -> list[str]:
    """Payloads reaching a file unchanged, each counted once across tee sinks.

    Only one flat brace group is supported. Pipeline transformations end
    provenance; literal portions of a group remain independently enumerable.
    """
    tokens = shell_tokens(command)
    if not tokens or any(t.operator and t in ("(", ")", "<<", "|&", "&") for t in tokens):
        return []
    opens = [i for i, t in enumerate(tokens) if t.operator and t == "{"]
    closes = [i for i, t in enumerate(tokens) if t.operator and t == "}"]
    if not opens and not closes:
        return _pipeline_payloads(tokens, None)
    if len(opens) != 1 or len(closes) != 1 or opens[0] >= closes[0]:
        return []
    start, end = opens[0], closes[0]
    if start and tokens[start - 1] not in (";", "&&", "||"):
        return []
    boundary = next((i for i in range(end + 1, len(tokens))
                     if tokens[i].operator and tokens[i] in (";", "&&", "||", "|")), len(tokens))
    suffix, inherited, _ = _redirects(tokens[end + 1:boundary])
    if suffix or (boundary < len(tokens) and tokens[boundary] == "|"):
        return []
    return (_pipeline_payloads(tokens[:start], None)
            + _pipeline_payloads(tokens[start + 1:end], inherited)
            + _pipeline_payloads(tokens[boundary + 1:], None))


def _pipeline_payloads(tokens: list[ShellWord], inherited: bool | None) -> list[str]:
    """Track each literal producer until its first unchanged file sink."""
    payloads: list[str] = []
    pending: str | None = None
    stage: list[ShellWord] = []
    for token in tokens + [ShellWord(";", operator=True)]:
        if not token.operator or token not in (";", "&&", "||", "|"):
            stage.append(token)
            continue
        args, sink, stdin_replaced = _redirects(stage)
        stage = []
        if not args:
            pending = None
            continue
        pending, own_sink = _stage_payload(args, None if stdin_replaced else pending)
        reaches_file = own_sink or sink or (sink is None and token != "|" and inherited)
        if pending is not None and reaches_file:
            payloads.append(pending)
            pending = None
        if token != "|" or sink is not None:
            pending = None
    return payloads
