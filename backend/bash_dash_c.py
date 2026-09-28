"""Recognize Python shell commands and decode their inline scripts."""
from __future__ import annotations

import io
import re
import shlex


_PREFIX_DELIMITERS = "|;&("
_PYTHON_CANDIDATE = re.compile(r"python(?:3(?:\.\d+)?)?\s+-c\s")
# The one-character lookbehind admits every legal prefix, including `/` paths.
_PYTHON_STDIN = re.compile(
    r"(?<![^\s|;&(/])python(?:3(?:\.\d+)?)?\s+-(?:\s|$)")
_SHLEX_SPECIAL = re.compile(r"[ \t\r\n'\"\\]")

# Suffix validity depends on the shlex state at each position. State order is
# between words, word, single quote, double quote, escape-to-word, escape-to-double.
_SHLEX_END_RAISES = (False, False, True, True, True, True)
_SHLEX_ORDINARY = (1, 1, 2, 3, 1, 3)
_SHLEX_WHITESPACE = (0, 0, 2, 3, 1, 3)
_SHLEX_SINGLE_QUOTE = (2, 2, 1, 3, 1, 3)
_SHLEX_DOUBLE_QUOTE = (3, 3, 2, 1, 1, 3)
_SHLEX_ESCAPE = (4, 4, 2, 5, 1, 3)


def _advance_shlex(
        raises: tuple[bool, ...], transition: tuple[int, ...]
) -> tuple[bool, ...]:
    """Apply one character's state transition to suffix raise outcomes."""
    return (
        raises[transition[0]], raises[transition[1]],
        raises[transition[2]], raises[transition[3]],
        raises[transition[4]], raises[transition[5]],
    )


def _largest_valid_start(text: str, position: int) -> int | None:
    """Return the rightmost legal prefix start for a candidate."""
    if position == 0:
        return 0

    preceding = text[position - 1]
    if preceding.isspace() or preceding in _PREFIX_DELIMITERS:
        return position - 1
    if preceding != "/":
        return None

    run_start = position - 1
    while run_start > 0 and not text[run_start - 1].isspace():
        run_start -= 1
    for index in range(position - 2, run_start - 1, -1):
        if text[index] in _PREFIX_DELIMITERS:
            return index
    return run_start - 1 if run_start else 0


def _script_tokens(
        text: str, positions: list[int]) -> dict[int, str | None]:
    """Check suffix validity backward, then lex one first token per valid suffix."""
    if not positions:
        return {}

    # Ordinary characters share one transition; shlex only changes behavior
    # at its ASCII whitespace, quote, and escape characters.
    specials = list(_SHLEX_SPECIAL.finditer(text, positions[0]))
    special_index = len(specials) - 1
    cursor = len(text)
    raises: tuple[bool, ...] = _SHLEX_END_RAISES
    valid: dict[int, bool] = {}
    for position in reversed(positions):
        while special_index >= 0 and specials[special_index].start() >= position:
            special = specials[special_index]
            special_index -= 1
            special_position = special.start()
            if cursor > special_position + 1:
                raises = _advance_shlex(raises, _SHLEX_ORDINARY)
            char = special.group()
            if char in " \t\r\n":
                transition = _SHLEX_WHITESPACE
            elif char == "'":
                transition = _SHLEX_SINGLE_QUOTE
            elif char == '"':
                transition = _SHLEX_DOUBLE_QUOTE
            else:
                transition = _SHLEX_ESCAPE
            raises = _advance_shlex(raises, transition)
            cursor = special_position

        if cursor > position:
            raises = _advance_shlex(raises, _SHLEX_ORDINARY)
        valid[position] = not raises[0]
        cursor = position

    stream = io.StringIO(text)
    tokens: dict[int, str | None] = {}
    for position in positions:
        if not valid[position]:
            tokens[position] = None
            continue
        stream.seek(position)
        lexer = shlex.shlex(stream, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens[position] = lexer.get_token()
    return tokens


def _dash_c_sources(text: str) -> list[str]:
    """Script bodies passed as `python3 -c '<code>'`."""
    positions: list[int] = []
    last_end = 0
    # A selected prefix consumes one character, so the next prefix must begin
    # at or after the preceding match's end.
    for match in _PYTHON_CANDIDATE.finditer(text):
        start = _largest_valid_start(text, match.start())
        if start is None or start < last_end:
            continue
        last_end = match.end()
        positions.append(match.end())

    outcomes = _script_tokens(text, positions)
    out: list[str] = []
    for position in positions:
        token = outcomes[position]
        if token is not None:
            out.append(token)
    return out
