"""Command segments, capture shapes and subshell groups in Bash text.

The token-level half of `bash_reads`' segment scan: splitting decoded
shell words into command segments, and recognising the `NAME=$( … )`
capture (bare, double-quoted, or behind a declaration builtin) and the
`( … )` subshell group a segment may carry. `_Scan` in `bash_reads`
drives these; every function here is shared with it.
"""
from __future__ import annotations

import re

from typing import cast

from backend.bash_literals import ShellWord, shell_tokens

_ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)


# Shell operators the tokenizer identifies, each of which ends a
# command segment.
_OPERATORS = frozenset({"|", "||", "&&", ";", "&", "|&"})


_DECLARATION_BUILTINS = frozenset(
    {"export", "readonly", "local", "declare", "typeset"})

# Nesting beyond this depth refuses the group (and the capture): real
# commands nest parens a few levels, and the bound keeps a command of
# many nested groups from recursing without end.
MAX_NESTED_GROUP_DEPTH = 32


def _segments(tokens: list[ShellWord]) -> list[list[str]]:
    """Split decoded shell words into command segments, honouring quotes.

    Quote-preserving shell tokens are used instead of splitting on `|;&`: those
    characters appear constantly INSIDE quoted arguments — a
    `grep 'a\\|b' f.py` alternation is one token, not two segments, and
    splitting it textually both loses the file and invents a segment.
    Operators inside a `$( … )` capture or any other `(`-group do not
    split: the group is one segment ending at its matching `)`, so
    `d=$(cd /tmp && mktemp -d probe.XXXXXX)` reaches the capture scan
    whole. A group arriving as its own segment (`( … )` around whole
    commands, redirects aside) is a subshell for `_Scan.segment`. A
    command the tokenizer cannot parse (an unbalanced quote, a
    heredoc body spliced in) yields no segments rather than a partial misreading.
    """
    segments: list[list[str]] = []
    current: list[str] = []
    depth = 0
    for tok in tokens:
        if getattr(tok, "operator", False):
            if tok == "(":
                depth += 1
            elif tok == ")" and depth > 0:
                depth -= 1
            if tok in _OPERATORS and depth == 0:
                if current:
                    segments.append(current)
                current = []
                continue
        current.append(tok)
    if current:
        segments.append(current)
    return segments


def _matching_paren(tokens: list[str], open_idx: int) -> int | None:
    """The index of the `)` closing the `(` at open_idx, or None."""
    depth = 0
    for idx in range(open_idx, len(tokens)):
        tok = tokens[idx]
        if not getattr(tok, "operator", False):
            continue
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth -= 1
            if depth == 0:
                return idx
    return None


def _capture_close(value: str) -> int | None:
    """Index in `value` of the `)` closing a leading `$(`, or None.

    The close must be the value's LAST character: anything after it (a
    value suffix, a following command) is not modelled. Literal parens
    inside single-quoted spans do not nest. A ", a backtick or a
    backslash in the body leaves the provenance of every later paren unknown — this value's expansion
    form has already flattened quotes and escapes away — so it refuses.
    """
    depth = 1
    idx = 2
    while idx < len(value):
        ch = value[idx]
        if ch in '"`\\':
            return None
        if ch == "'":
            end = value.find("'", idx + 1)
            if end < 0:
                return None
            idx = end + 1
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return idx if idx == len(value) - 1 else None
        idx += 1
    return None


def _quoted_capture_value(value: str) -> list[ShellWord] | None:
    """Inner tokens of a double-quoted capture's `$( … )` value, or None.

    `$(( … ))` is an arithmetic expansion's opener, never a group; token
    level cannot ask for whitespace, so the adjacent spelling refuses. A
    spaced `$( ( … ) )` is a real captured subshell and tokenizes.
    """
    if value[2] == "(":
        return None
    close = _capture_close(value)
    if close is None:
        return None
    return shell_tokens(value[2:close])


def _capture_split(raw_segment: list[str]) -> tuple[list[str], list[ShellWord]] | None:
    """Split off a leading assignment's captured substitution: (prefix, inner).

    Three spellings reach the capture: the unquoted `d=$( … )` splits at
    the `(` the tokenizer keeps out of a word — a `NAME=$` word, then the
    `(` operator — the double-quoted `d="$( … )"` stays one word whose
    value is the whole substitution, and any of the declaration builtins
    may lead either (`export d=$( … )`). The double-quoted form's inner
    text is tokenized with its quote provenance intact, so a literal
    paren inside a quoted template does not read as substitution
    nesting; a nested live `$( … )` still refuses through the shared
    dispatch's operator refusal as it does elsewhere. Leading
    `NAME=value` words and declaration builtins may precede either, and
    the closing `)` must end the segment: anything after it (a value
    suffix, a following command) is not modelled, so a backtick capture
    never matches.
    """
    for idx, tok in enumerate(raw_segment):
        if getattr(tok, "operator", False):
            return None
        if tok in _DECLARATION_BUILTINS:
            continue
        m = _ASSIGN.match(tok)
        if m is None:
            return None
        value = getattr(tok, "expansion", tok)[m.start(2):]
        nxt = raw_segment[idx + 1] if idx + 1 < len(raw_segment) else None
        if (nxt is not None and getattr(nxt, "operator", False) and nxt == "("
                and value.endswith("$")):
            nxt2 = raw_segment[idx + 2] if idx + 2 < len(raw_segment) else None
            if nxt2 is None or getattr(nxt2, "operator", False) is False \
                    or nxt2 != "(":
                close = _matching_paren(raw_segment, idx + 1)
                if close != len(raw_segment) - 1:
                    return None
                return raw_segment[:idx], cast("list[ShellWord]", raw_segment[idx + 2:close])
            # `$((` is an arithmetic expansion's opener, never a group;
            # token-level whitespace is gone, so the adjacent spelling
            # refuses however it was quoted: the next loop iteration
            # reaches the operator refusal on that `(`.
            continue
        if nxt is None and value.startswith("$("):
            tokens = _quoted_capture_value(value)
            if tokens is not None:
                return raw_segment[:idx], tokens
    return None


def _split_redirects(args: list[str]) -> tuple[list[str], list[str], list[str]]:
    """(operands, output files, stdin files) for one segment's arguments.

    A redirect target is a file the command WRITES, so it must be pulled
    out before operand scanning — otherwise `grep x src.py > out.txt`
    books out.txt as something that was read. Input and fd redirections also
    consume an argument, but neither is a destination operand. Unknown shell
    operators raise ValueError rather than entering the filename heuristics.
    """
    operands: list[str] = []
    writes: list[str] = []
    inputs: list[str] = []
    idx = 0
    while idx < len(args):
        tok = args[idx]
        if not getattr(tok, "operator", False):
            operands.append(tok)
            idx += 1
            continue
        match = re.fullmatch(r"([0-9]*)(>>?|<|>&|<&)", tok)
        if not match or idx + 1 == len(args) or getattr(args[idx + 1], "operator", False):
            raise ValueError("unsupported or incomplete shell redirection")
        fd, operator = match.groups()
        target = args[idx + 1]
        if operator in (">", ">>"):
            writes.append(target)
        elif operator == "<" and fd in ("", "0"):
            inputs.append(target)
        elif operator in (">&", "<&") and not re.fullmatch(r"[0-9]+|-", target):
            raise ValueError("unsupported descriptor target")
        idx += 2
    return operands, writes, inputs
