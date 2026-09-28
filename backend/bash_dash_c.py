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
        text: str, positions: list[int]) -> dict[int, tuple[str | None, bool]]:
    """Memoize suffix validity at boundaries and first tokens for candidates."""
    candidate_starts = set(positions)
    outcomes: dict[int, tuple[str | None, bool]] = {}
    for position in positions:
        if position in outcomes:
            continue

        stream = io.StringIO(text)
        stream.seek(position)
        lexer = shlex.shlex(stream, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = ""

        path: list[tuple[int, str | None]] = []
        cursor = position
        while True:
            # Each cursor follows a complete token and its consumed separator.
            if cursor in outcomes:
                valid = outcomes[cursor][1]
                break
            try:
                token = lexer.get_token()
            except ValueError:
                outcomes[cursor] = (None, False)
                valid = False
                break
            if token is None:
                outcomes[cursor] = (None, True)
                valid = True
                break
            first_token = token if cursor in candidate_starts else None
            path.append((cursor, first_token))
            # get_token consumes one separator after a token; this is the next
            # position from which a fresh lexer has the same state and result.
            cursor = stream.tell()

        for token_start, first_token in path:
            outcomes[token_start] = (first_token, valid)
    return outcomes


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
        token, valid = outcomes[position]
        if valid and token is not None:
            out.append(token)
    return out
