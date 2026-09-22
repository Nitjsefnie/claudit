"""How many times a heredoc body runs, read off the loops around it.

A heredoc inside `for x in a b c; do … done` is written once per
iteration. When the word list is literal the count is in the command
text, so it can be recovered without running anything — the rule
bash_churn keeps for everything else. Split out of bash_churn so that
module stays one concern: what a body writes, not how often.
"""
from __future__ import annotations

import math

from backend.bash_literals import ShellWord

# Words after which the next word is in command position again.
_COMMAND_START_WORDS = frozenset({"do", "then", "else", "elif", "{", "!"})
_COMMAND_SEPARATORS = frozenset({";", "&&", "||", "|", "&", "|&"})


def _for_count(tokens: list[ShellWord], i: int) -> tuple[int | None, int]:
    """Iterations of the `for` header starting at tokens[i], and the index
    just past its word list.

    Known only for `for NAME in W1 W2 …` whose every word is literal: a
    runtime expansion (`$x`, `$(…)`, an unquoted glob, `"$@"`) makes the
    count a property of the run, and a bare `for NAME` iterates the
    positional parameters. None means unknown."""
    j = i + 2
    if j >= len(tokens) or tokens[j].operator or tokens[j] != "in":
        return None, j
    j += 1
    words: list[ShellWord] = []
    while j < len(tokens) and not tokens[j].operator and tokens[j] != "do":
        words.append(tokens[j])
        j += 1
    if not all(w.literal for w in words):
        return None, j
    return len(words), j


def heredoc_repeats(tokens: list[ShellWord], n_heredocs: int) -> list[int]:
    """How many times each heredoc body runs, one entry per heredoc.

    The body of a heredoc inside `for x in a b c; do … done` is written
    once per iteration, and a literal word list puts that count in the
    command text — no execution needed. Nested loops multiply.

    Walks the already-tokenized command once: `_split_heredocs` leaves a
    `<&-` marker where each opener stood, so the loop stack at a marker
    is the stack around that heredoc. Anything uncertain counts ONE
    iteration — an unknown word list, `while`/`until`/`select`, a
    tokenizer that rejected the command (it returns no tokens), or a
    marker count that disagrees with the heredoc count (a literal `<&-`
    in the user's own command would shift the pairing). A floor, like
    every other estimate here.
    """
    ones = [1] * n_heredocs
    if not tokens:
        return ones
    stack: list[int] = []
    pending: int | None = None
    command_position = True
    repeats: list[int] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.operator:
            if tok == "<&" and i + 1 < len(tokens) and tokens[i + 1] == "-":
                repeats.append(math.prod(stack))
                i += 2
                command_position = False
                continue
            command_position = tok in _COMMAND_SEPARATORS
            i += 1
            continue
        if command_position and tok == "for":
            pending, i = _for_count(tokens, i)
            continue
        if command_position and tok in ("while", "until", "select"):
            pending = None
        elif command_position and tok == "do":
            stack.append(1 if pending is None else pending)
            pending = None
        elif command_position and tok == "done" and stack:
            stack.pop()
        command_position = tok in _COMMAND_START_WORDS
        i += 1
    return repeats if len(repeats) == n_heredocs else ones
