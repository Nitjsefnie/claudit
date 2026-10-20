"""Bounded comma-list expansion for write targets, preserving raw quoting."""
from __future__ import annotations

import re

_PROTECTED = re.compile(r'''(?:'[^']*'|"(?:\\.|[^"\\])*"|\\[\s\S])''')
_RANGE = re.compile(r"(?:-?[0-9]+\.\.-?[0-9]+|[a-zA-Z]\.\.[a-zA-Z])(?:\.\.-?[0-9]+)?")
MAX_BRACE_WORDS = 256
MAX_BRACE_CHARS = 100_000


def _choices(raw: str) -> list[tuple[int, int, list[int]]]:
    syntax = _PROTECTED.sub(lambda match: "_" * len(match[0]), raw)
    opens: list[tuple[int, list[int]]] = []
    choices: list[tuple[int, int, list[int]]] = []
    for idx, char in enumerate(syntax):
        if char == "{":
            opens.append((idx, []))
        elif char == "," and opens:
            opens[-1][1].append(idx)
        elif char == "}" and opens:
            start, commas = opens.pop()
            if commas or _RANGE.fullmatch(syntax, start + 1, idx):
                choices.append((start, idx, commas))
    choices.sort(key=lambda choice: choice[0])
    return choices


def brace_words(raw: str) -> list[str] | None:
    """Expand finite lists in shell order; None keeps one unknown operand."""
    pending = [raw]
    words: list[str] = []
    materialized = len(raw)
    while pending:
        word = pending.pop()
        choices = _choices(word)
        if not choices:
            words.append(word)
            continue
        start, end, commas = choices[0]
        body = _PROTECTED.sub("", word[start:end])
        if not commas or any(char in body for char in "$`*?["):
            return None
        boundaries = [start, *commas, end]
        size = sum(len(word) - (end - start + 1) + right - left - 1
                   for left, right in zip(boundaries, boundaries[1:]))
        materialized += size
        if (len(words) + len(pending) + len(boundaries) - 1 > MAX_BRACE_WORDS
                or materialized > MAX_BRACE_CHARS):
            return None
        expanded = [word[:start] + word[left + 1:right] + word[end + 1:]
                    for left, right in zip(boundaries, boundaries[1:])]
        pending.extend(reversed(expanded))
    return words
