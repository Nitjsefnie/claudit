"""Heredoc-context classification and diff-body churn.

What an opener's command line means for the body that follows it —
`patch`, inline Python, or a verbatim file write — and the +/− hunk
count of a diff body. Split from `bash_churn` for size; the shared
syntax scan lives in `bash_literals`/`bash_dash_c`.
"""
from __future__ import annotations

import re

from backend.bash_dash_c import _PYTHON_STDIN

# A redirect to a path. The first branch matches an unprefixed operator
# (plain, clobber, both-streams) and the lookbehind keeps the fd-prefixed
# forms (`2>`, `2>&1`, `2&>`) out; the second admits the fd-1 STDOUT
# spellings (`1>`, `1>>`, `1>|`, `1>& file`) — fd 1 IS stdout, where a
# heredoc body lands (#819) — and the digit only reads as an fd when it
# STARTS the token, so a word-adjacent one (`f1>`) stays a word; `21>`
# and friends stay blocked, and any all-digits or `-` `>&` target is
# filtered as a descriptor by _FD_TARGET.
_REDIRECT = re.compile(
    r"(?:(?<![0-9<>&])(?:>>|>\|?|&>>?|>&)|(?<![0-9A-Za-z_])1(?:>>|>\|?|>&))\s*"
    r"(?:'([^']+)'|\"([^\"]+)\"|([^\s'\";&|<>()]+))")

# A `>&` dup/close target: digits or a lone `-` name descriptors.
_FD_TARGET = re.compile(r"[0-9]+|-")

_CAT = re.compile(r"(?:^|[|;&(]|\s)cat\b")
_TEE = re.compile(r"(?:^|[|;&(]|\s)tee\b")
_PATCH = re.compile(r"(?:^|[|;&(]|\s)(?:git\s+apply|patch)\b")

_NULL_SINKS = ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty")


def _writes_to_a_file(context: str) -> bool:
    """True when this heredoc's body lands in a file verbatim."""
    if _TEE.search(context):
        return True
    if not _CAT.search(context):
        return False
    targets = [m.group(1) or m.group(2) or m.group(3)
               for m in _REDIRECT.finditer(context)]
    return any(t and t not in _NULL_SINKS and not _FD_TARGET.fullmatch(t)
               for t in targets)


def heredoc_context_kind(context: str) -> str | None:
    """Classify an opener context with churn's existing precedence."""
    if _PATCH.search(context):
        return "patch"
    if _PYTHON_STDIN.search(context):
        return "python"
    if _writes_to_a_file(context):
        return "file"
    return None


def diff_churn(body: str) -> tuple[int, int]:
    """+/- hunk lines of a unified diff. The `+++`/`---` file headers
    name files, they do not change lines."""
    added = deleted = 0
    for line in body.split("\n"):
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            deleted += 1
    return added, deleted
