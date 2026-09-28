"""Recognize Python shell commands and decode their inline scripts."""
from __future__ import annotations

import re


_PREFIX_DELIMITERS = "|;&("
_PYTHON_CANDIDATE = re.compile(r"python(?:3(?:\.\d+)?)?\s+-c\s")
# The one-character lookbehind admits every legal prefix, including `/` paths.
_PYTHON_STDIN = re.compile(
    r"(?<![^\s|;&(/])python(?:3(?:\.\d+)?)?\s+-(?:\s|$)")
_SHLEX_SPECIAL = re.compile(r"[ \t\r\n'\"\\]")
MAX_SCRIPT_SPAN_CHARS = 1_000_000  # Bounds script text taken from one command.

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


def _advance_token_ends(
        ends: tuple[int, ...], transition: tuple[int, ...],
        whitespace_position: int | None = None,
) -> tuple[int, ...]:
    """Carry first-token end offsets through one state transition."""
    return tuple(
        whitespace_position + 1
        if state == 1 and whitespace_position is not None
        else ends[transition[state]]
        for state in range(6)
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


def _first_token(text: str, position: int, span_chars: int) -> str | None:
    """Avoid shlex.get_token's quadratic concatenation on long tokens."""
    # Keep shlex state transitions explicit for review.
    # pylint: disable=too-many-branches
    token_chars: list[str] = []
    state = 0
    started = False
    for index in range(position, position + span_chars):
        char = text[index]
        if state == 0:
            if char in " \t\r\n":
                continue
            started = True
            if char == "'":
                state = 2
            elif char == '"':
                state = 3
            elif char == "\\":
                state = 4
            else:
                token_chars.append(char)
                state = 1
        elif state == 1:
            if char in " \t\r\n":
                break
            if char == "'":
                state = 2
            elif char == '"':
                state = 3
            elif char == "\\":
                state = 4
            else:
                token_chars.append(char)
        elif state == 2:
            if char == "'":
                state = 1
            else:
                token_chars.append(char)
        elif state == 3:
            if char == '"':
                state = 1
            elif char == "\\":
                state = 5
            else:
                token_chars.append(char)
        elif state == 4:
            token_chars.append(char)
            state = 1
        else:
            if char not in ('"', "\\"):
                token_chars.append("\\")
            token_chars.append(char)
            state = 3
    return "".join(token_chars) if started else None


def _script_tokens(
        text: str, positions: list[int], *,
        spans: dict[int, int] | None = None) -> dict[int, str | None]:
    """Check suffix validity and token spans backward, then lex within budget."""
    # Keep the backward automaton and budget accounting in one linear pass.
    # pylint: disable=too-many-branches,too-many-locals,too-many-statements
    if not positions:
        return {}

    # Ordinary characters share one transition; shlex only changes behavior
    # at its ASCII whitespace, quote, and escape characters.
    specials = list(_SHLEX_SPECIAL.finditer(text, positions[0]))
    special_index = len(specials) - 1
    cursor = len(text)
    raises: tuple[bool, ...] = _SHLEX_END_RAISES
    valid: dict[int, bool] = {}
    span_chars: dict[int, int] = {}
    token_ends = (len(text),) * 6
    for position in reversed(positions):
        while special_index >= 0 and specials[special_index].start() >= position:
            special = specials[special_index]
            special_index -= 1
            special_position = special.start()
            if cursor > special_position + 1:
                raises = _advance_shlex(raises, _SHLEX_ORDINARY)
                token_ends = _advance_token_ends(token_ends, _SHLEX_ORDINARY)
            char = special.group()
            if char in " \t\r\n":
                transition = _SHLEX_WHITESPACE
                whitespace_position = special_position
            elif char == "'":
                transition = _SHLEX_SINGLE_QUOTE
                whitespace_position = None
            elif char == '"':
                transition = _SHLEX_DOUBLE_QUOTE
                whitespace_position = None
            else:
                transition = _SHLEX_ESCAPE
                whitespace_position = None
            raises = _advance_shlex(raises, transition)
            token_ends = _advance_token_ends(
                token_ends, transition, whitespace_position)
            cursor = special_position

        if cursor > position:
            raises = _advance_shlex(raises, _SHLEX_ORDINARY)
            token_ends = _advance_token_ends(token_ends, _SHLEX_ORDINARY)
        valid[position] = not raises[0]
        span_chars[position] = token_ends[0] - position
        cursor = position

    if spans is not None:
        spans.update(span_chars)

    tokens: dict[int, str | None] = {}
    kept_span_chars = 0
    for position in positions:
        if not valid[position]:
            tokens[position] = None
            continue
        candidate_span = span_chars[position]
        if kept_span_chars + candidate_span > MAX_SCRIPT_SPAN_CHARS:
            tokens[position] = None
            continue
        token = _first_token(text, position, candidate_span)
        tokens[position] = token
        if token is not None:
            kept_span_chars += candidate_span
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
