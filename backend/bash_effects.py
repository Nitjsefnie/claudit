"""Shell command payloads and write effects: sed, cp, perl, echo, printf.

The effect half of the old `bash_literals`, split so that module stays
the bounded-words half: what a command line's payload text is and what
paths it puts on disk — `sed_parts`, `destination_paths`, `perl_paths`,
and the pipeline folds (`shell_effects`, `effects_from_tokens`).
`command_options` refuses any flag the command's modes dict does not
list, so an unparsable command names nothing rather than a guessed path.
"""
from __future__ import annotations

import posixpath
import re

from backend.bash_literals import (MAX_LITERAL_CHARS, ShellWord, _file_sink,
                                   command_options, literal_path, shell_tokens)
from backend.target_paths import copy_source_name, directory_spelling, join_child


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
                    paths.append(ShellWord(join_child(target, child, base)))
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
        match = re.fullmatch(r"([0-9]*)(>>?|<|>&|<&|>\||&>>?)", word) if word.operator else None
        if match:
            if idx + 1 == len(words):
                return [], False, True
            fd, redirect = match.groups()
            if fd and redirect in ("&>", "&>>"):
                # bash keeps a digit before `&>` a plain word, so this
                # shape never tokenizes; refuse it like an incomplete
                # redirect rather than reading the target as an operand.
                return [], False, True
            if fd in ("", "1") and redirect in (">", ">>", ">&", ">|", "&>", "&>>"):
                # `>& file` with the fd omitted (or spelled 1) is the
                # stdout+stderr-to-file spelling, `&>`'s twin (#802);
                # against a digit or `-` it is a duplication or close
                # and sinks nothing.
                dup = redirect == ">&" and re.fullmatch(r"[0-9]+|-", words[idx + 1])
                sink = not dup and _file_sink(words[idx + 1])
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
    if name == "cat":
        payload = pending if len(args) == 1 else ("" if all(arg == "/dev/null" for arg in args[1:]) else None)
        return payload, False, ""
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
        other_sink = any(t.operator and re.fullmatch(r"[2-9][0-9]*(?:>>|>\|?)", t)
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
