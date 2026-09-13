"""Bounded shell words and literal output; no expansion or command execution.

Keep quote provenance until consumers have decided whether a word is literal.
Shell effects retain known payloads, known empty output and unknown write sizes.
"""
from __future__ import annotations

import posixpath
import re

from backend.target_paths import copy_source_name, directory_spelling

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

    def __new__(cls, value: str, literal: bool = True,
                operator: bool = False) -> ShellWord:
        word = super().__new__(cls, value)
        word.literal = literal
        word.operator = operator
        word.expansion = value
        word.unquoted_expansion = False
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


def _copy_parts(name: str, args: list[str]) -> tuple[dict[str, str | None], list[str], str | None]:
    """Options, source operands and destination before path resolution."""
    modes = {"-" + c: 0 for c in "abcfHilLnprRsTuvDZ"}
    modes.update(dict.fromkeys(("--no-target-directory", "--force", "--verbose",
                               "--no-clobber", "--interactive", "--strip", "--compare"), 0))
    modes.update(dict.fromkeys(("--backup", "--update", "--preserve", "--reflink", "--sparse"), 2))
    modes.update(dict.fromkeys(("-t", "--target-directory", "-S", "--suffix", "--no-preserve"), 1))
    if name == "install":
        modes.update(dict.fromkeys(("-d", "--directory"), 0))
        modes.update(dict.fromkeys(("-o", "--owner", "-g", "--group", "-m", "--mode", "--strip-program"), 1))
    try:
        options, operands = command_options(args, modes)
    except ValueError:
        return {}, [], None
    flags = dict(options)
    if name == "install" and any(f in flags for f in ("-d", "--directory")):
        return flags, [], None
    directory = any(f in flags for f in ("-t", "--target-directory"))
    target = flags.get("-t", flags.get("--target-directory"))
    if directory:
        sources = operands
    elif len(operands) >= 2:
        *sources, target = operands
    else:
        return flags, [], None
    return flags, sources, target


def destination_paths(name: str, args: list[str], *, literal_only: bool = True,
                      base: str | None = "") -> list[str]:
    """cp/install/mv destinations; directory facts must be in the text."""
    flags, sources, target = _copy_parts(name, args)
    if target is None or not (literal_path(target) if literal_only else _file_sink(target)) or not sources:
        return []
    no_directory = any(f in flags for f in ("-T", "--no-target-directory"))
    directory = any(f in flags for f in ("-t", "--target-directory"))
    directory |= directory_spelling(target, base) or len(sources) > 1
    if not literal_only:
        paths: list[str] = [target]
    elif directory and not no_directory:
        paths = []
        for source in sources:
            if literal_path(source):
                child = copy_source_name(source, base)
                if child is not None:
                    paths.append(ShellWord(posixpath.join(target, child)))
    else:
        paths = [target] if len(sources) == 1 and not directory else []
    return paths


def perl_paths(args: list[str], *, literal_only: bool = True) -> list[str]:
    """In-place Perl one-liners; program and option arguments are not files."""
    modes = {"-" + c: 0 for c in "pnwWl"}
    modes.update({"-" + c: 1 for c in "eEIMmF"})
    modes["-i"] = 2
    try:
        options, files = command_options(args, modes)
    except ValueError:
        return []
    flags = dict(options)
    is_file = literal_path if literal_only else _file_sink
    return [p for p in files if is_file(p)] if "-i" in flags and ("-e" in flags or "-E" in flags) else []


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


def _sed_field(script: str, idx: int, delimiter: str,
               replacement: bool) -> tuple[str | None, int]:
    """Decode a field; unknown syntax retains the delimiter position."""
    field: list[str] = []
    unknown = False
    while idx < len(script):
        char = script[idx]
        idx += 1
        if char == delimiter:
            return None if unknown else "".join(field), idx
        if char == "\\" and idx < len(script):
            char = script[idx]
            idx += 1
            if char in ("n", "\n"):
                char = "\n"
            elif char not in delimiter + "\\" + ("&" if replacement else ".*[^$"):
                unknown = True
        elif char in ("&" if replacement else ".*[^$+?(){}|"):
            unknown = True
        field.append(char)
    return None, -1


def _sed_substitution(script: str) -> tuple[str, str] | None:
    """One literal old/new pair, without inferring match multiplicity."""
    if not getattr(script, "literal", True) or len(script) < 4 or script[0] != "s":
        return None
    delimiter = script[1]
    if delimiter.isalnum() or delimiter in "\\\n":
        return None
    old, idx = _sed_field(script, 2, delimiter, False)
    if idx < 0:
        return None
    new, idx = _sed_field(script, idx, delimiter, True)
    if idx < 0 or new is None or old == "" or not re.fullmatch(r"(?:[1-9][0-9]*)?g?", script[idx:]):
        return None
    if old is None:
        return ("", "") if new == "" else None
    return old, new


def _echo(args: list[str]) -> str | None:
    newline, escapes = True, False
    while args and re.fullmatch(r"-[neE]+", args[0]):
        for flag in args.pop(0)[1:]:
            if flag == "n":
                newline = False
            else:
                escapes = flag == "e"
    if any(not getattr(arg, "literal", True) for arg in args):
        return None
    payload = " ".join(args)
    if len(payload) > MAX_LITERAL_CHARS:
        return None
    if escapes:
        # Reuse the bounded printf escape decoder, with percent signs literal.
        before, stop, _ = payload.partition("\\c")
        pieces = _printf_format(before.replace("%", "%%"))
        if pieces is None:
            return None
        payload = "".join(piece or "" for piece in pieces)
        newline &= not bool(stop)
    return payload + ("\n" if newline else "")


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
                sink = redirect != ">&" and _file_sink(words[idx + 1])
            if fd in ("", "0") and redirect in ("<", "<&"):
                stdin_replaced = True
            idx += 2
        else:
            args.append(word)
            idx += 1
    return args, sink, stdin_replaced


def _sed_effect(args: list[str]) -> tuple[str | None, bool, str]:
    scripts, files, in_place = sed_parts(args)
    own_sink = in_place and any(_file_sink(f) for f in files)
    if len(scripts) == 1 and own_sink:
        pair = _sed_substitution(scripts[0])
        if pair is not None:
            return pair[1], True, pair[0]
        if getattr(scripts[0], "literal", True) and re.fullmatch(r"(?:[0-9]+(?:,[0-9]+)?)?d", scripts[0]):
            return "", True, ""
    payload, _ = _sed_append(args)
    return payload, own_sink, ""


def _other_stage_payload(args: list[ShellWord], pending: str | None) -> tuple[str | None, bool, str]:
    name = posixpath.basename(args[0])
    if name in ("cp", "install", "mv"):
        _, sources, target = _copy_parts(name, list(args[1:]))
        empty = bool(sources) and all(source == "/dev/null" for source in sources)
        return "" if empty else None, bool(sources and target and _file_sink(target)), ""
    if name == "perl":
        return None, bool(perl_paths(list(args[1:]), literal_only=False)), ""
    if name in (":", "true", "false"):
        return "", False, ""
    if name == "cat" and len(args) == 1:
        return pending, False, ""
    if name == "tee":
        try:
            _, files = command_options(list(args[1:]), {
                "-a": 0, "--append": 0, "-i": 0, "--ignore-interrupts": 0,
                "-p": 0, "--output-error": 2})
        except ValueError:
            files = []
        return pending, any(_file_sink(f) for f in files), ""
    return None, False, ""


def _stage_payload(args: list[ShellWord], pending: str | None) -> tuple[str | None, bool, str]:
    name = posixpath.basename(args[0])
    if (name == "echo" and len(args) == 2
            and args[1].expansion in ("EXIT=$?", "exit=$?")
            and not args[1].unquoted_expansion):
        # The quoted numeric status changes the bytes, never the line count.
        return args[1] + "\n", False, ""
    if name == "echo":
        return _echo(list(args[1:])), False, ""
    if name == "printf":
        return _printf(list(args[1:])), False, ""
    if name == "sed":
        return _sed_effect(list(args[1:]))
    return _other_stage_payload(args, pending)


def shell_payloads(command: str) -> list[str]:
    """Known file payloads; the compatibility view excludes unknown sizes."""
    return [payload for payload, _ in shell_effects(command) if payload is not None]


def shell_effects(command: str) -> list[tuple[str | None, str]]:
    """File effects as (added payload or None for unknown, deleted payload).

    Each payload counts once across tee sinks. An empty string is known zero.

    Only one flat brace group is supported. Pipeline transformations end
    provenance; literal portions of a group remain independently enumerable.
    """
    return effects_from_tokens(shell_tokens(command))


def payloads_from_tokens(tokens: list[ShellWord]) -> list[str]:
    """Classify literal output using a command's already decoded shell words."""
    return [payload for payload, _ in effects_from_tokens(tokens) if payload is not None]


def effects_from_tokens(tokens: list[ShellWord]) -> list[tuple[str | None, str]]:
    """Classify file effects without repeating the shared syntax scan."""
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
    if suffix:
        return []
    if boundary < len(tokens) and tokens[boundary] == "|":
        # The downstream stage still establishes a file write, but the brace
        # group's output is not tracked through a transformation.
        before = []
    else:
        before = (_pipeline_payloads(tokens[:start], None)
                  + _pipeline_payloads(tokens[start + 1:end], inherited))
    return before + _pipeline_payloads(tokens[boundary + 1:], None)


def _pipeline_payloads(tokens: list[ShellWord], inherited: bool | None) -> list[tuple[str | None, str]]:
    """Track each literal producer until its first unchanged file sink."""
    payloads: list[tuple[str | None, str]] = []
    pending: str | None = None
    stage: list[ShellWord] = []
    for token in tokens + [ShellWord(";", operator=True)]:
        if not token.operator or token not in (";", "&&", "||", "|"):
            stage.append(token)
            continue
        args, sink, stdin_replaced = _redirects(stage)
        empty_input = any(t.operator and t in ("<&", "0<&", "<", "0<")
                          and i + 1 < len(stage) and stage[i + 1] in ("-", "/dev/null")
                          for i, t in enumerate(stage))
        other_sink = any(t.operator and re.fullmatch(r"[2-9][0-9]*>>?", t)
                         and i + 1 < len(stage) and _file_sink(stage[i + 1])
                         for i, t in enumerate(stage))
        stage = []
        if not args:
            pending = None
            continue
        incoming = ("" if empty_input else None) if stdin_replaced else pending
        pending, own_sink, deleted = _stage_payload(args, incoming)
        if other_sink and posixpath.basename(args[0]) not in ("echo", "printf", ":", "true", "false"):
            payloads.append((None, ""))
        reaches_file = own_sink or sink or (sink is None and token != "|" and inherited)
        if reaches_file:
            payloads.append((pending, deleted))
            pending = ""
        if token != "|":
            pending = None
        elif sink is not None:
            pending = ""
    return payloads
