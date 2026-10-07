"""File operands of one Bash command, minus flags and pattern operands.

The classifier behind `bash_reads`: which bare words of a command line
name a file at all, and, for the reading commands, which operand is the
PATTERN rather than a path. Only paths the command text names outright
are recognised; a token is a candidate path when it carries a short
extension or a separator, and bare words are rejected so `grep TODO
notes.md` does not book "TODO" and `diff --git` does not book a file.
"""
from __future__ import annotations

import re

from backend.bash_effects import sed_parts
from backend.bash_literals import literal_path
from backend.target_paths import windows_absolute

# Commands taking a PATTERN (or program text) operand before their file
# operands. Skipping one non-path operand for these is what stops
# `grep config.py *.txt` from booking the pattern as a file.
PATTERN_CMDS = frozenset({
    "grep", "egrep", "fgrep", "rg", "ag", "sed", "awk", "jq",
})

# Flags whose VALUE is the following token. Without this, `grep -e
# foo.py file.txt` books the pattern as a path, and `head -n 50 f`
# books "50".
VALUE_FLAGS = frozenset({
    "-e", "-f", "--regexp", "--file", "-n", "--lines", "-c", "--bytes",
    "-m", "--max-count", "-A", "-B", "-C", "--after-context",
    "--before-context", "--context", "--include", "--exclude",
    "--exclude-dir", "-d", "--delimiter", "-t",
})

# A token is a candidate path when it carries a short extension or a
# separator. Bare words are rejected: `grep TODO notes.md` must not book
# "TODO", and a subcommand like `diff --git` is not a file.
_PATH_RE = re.compile(r"\.[A-Za-z0-9_]{1,8}$")


def _looks_like_path(token: str, windows: bool = False, *, windows_roots: bool = True) -> bool:
    """True when a bare operand is plausibly a filename."""
    if not token or token.startswith("-"):
        return False
    if token in (".", "..", "*"):
        return False
    if windows_roots and windows_absolute(token):
        return True
    # An unexpanded glob names no single file; refusing it here is the
    # same rule as bash_churn's — the shell would have to run for this
    # to become a path.
    if any(ch in token for ch in "*?["):
        return False
    return bool("/" in token or (windows and "\\" in token) or _PATH_RE.search(token))


def _operand_paths(name: str, args: list[str], windows: bool = False) -> list[str]:
    """File operands of one command, minus flags and pattern operands."""
    if name == "sed":
        return [p for p in sed_parts(args)[1] if literal_path(p)]
    pending_pattern = 1 if name in PATTERN_CMDS else 0
    paths: list[str] = []
    idx = 0
    while idx < len(args):
        tok = args[idx]
        if tok in VALUE_FLAGS:
            idx += 2
            continue
        if tok.startswith("-"):
            idx += 1
            continue
        if pending_pattern and not _looks_like_path(tok, windows_roots=False):
            # The pattern operand. A pattern that DOES look like a path
            # (`grep config.py *.log`) is ambiguous; treating it as the
            # pattern would lose a real file more often than it invents
            # one, so path-shaped tokens fall through to the path branch.
            pending_pattern = 0
            idx += 1
            continue
        pending_pattern = 0
        if _looks_like_path(tok, windows):
            paths.append(tok)
        idx += 1
    return paths
