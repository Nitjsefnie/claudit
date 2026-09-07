"""Read TARGETS recovered from a Bash command's TEXT.

The sibling of `bash_churn`: that module asks what a command WROTE, this
one asks what it put INTO the transcript. Under bypass permissions most
file reading goes through Bash rather than the `Read` tool — measured
over the live corpus, `grep`/`sed`/`cat`/`head`/`tail` account for
~138k path-bearing calls — so a reader that only understands `Read`
sees almost none of the intake.

Two questions, and the second is why `kind` exists:

  target(s)  which file(s) this call named, absolute where a `cd` in the
             same command line makes that resolvable.
  kind       `whole` when the command emits the file entire, `slice`
             when any stage narrows it.

Conflating those INVERTS the metric. `grep -n foo big.py` and
`cat big.py` both name one file; only the second put the file in
context. Score them alike and the frugal grep-then-narrow workflow
reads as more wasteful than one indiscriminate `cat`, which is
backwards. So a `whole` classification survives only when NOTHING in
the pipeline narrows it: `cat f | grep x` is a slice.

Same refusal to estimate as `bash_churn`: only what the command text
names outright is returned. A path built at runtime — `glob.glob(...)`,
`$1`, an unexpanded `*.py` — yields nothing rather than a guess, and a
command whose reads are performed by an interpreter body is not this
module's business at all.
"""
from __future__ import annotations

import posixpath
import re
import shlex

# Commands that put file CONTENT into the transcript, split by whether
# they emit the file entire.
#
# `wc`, `ls` and `stat` are deliberately absent from both: they return a
# MEASUREMENT of a file, not the file, so counting them as reads would
# attribute a 12-byte line count to a 4 MB target.
WHOLE_CMDS = frozenset({"cat", "bat", "less", "more"})
SLICE_CMDS = frozenset({
    "head", "tail", "sed", "grep", "egrep", "fgrep", "rg", "ag",
    "awk", "jq", "diff", "cut", "column",
})
READ_CMDS = WHOLE_CMDS | SLICE_CMDS

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

# Shell operators shlex hands back as punctuation, each of which ends a
# command segment.
_OPERATORS = frozenset({"|", "||", "&&", ";", "&", "|&"})

# Redirections whose following token is a file the command WRITES.
_WRITE_REDIRECTS = frozenset({">", ">>"})

# Tools whose operands are files they write, not read.
_WRITE_CMDS = frozenset({"tee"})


def _tokenize(command: str) -> list[list[str]]:
    """Split a command line into segments, honouring quotes.

    `shlex` is used rather than a regex split on `|;&` because those
    characters appear constantly INSIDE quoted arguments — a
    `grep 'a\\|b' f.py` alternation is one token, not two segments, and
    splitting it textually both loses the file and invents a segment.
    A command shlex cannot parse (an unbalanced quote, a heredoc body
    spliced in) yields no segments rather than a partial misreading.
    """
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return []
    segments: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok in _OPERATORS:
            if current:
                segments.append(current)
            current = []
        else:
            current.append(tok)
    if current:
        segments.append(current)
    return segments


def _looks_like_path(token: str) -> bool:
    """True when a bare operand is plausibly a filename."""
    if not token or token.startswith("-"):
        return False
    if token in (".", "..", "*"):
        return False
    # An unexpanded glob names no single file; refusing it here is the
    # same rule as bash_churn's — the shell would have to run for this
    # to become a path.
    if any(ch in token for ch in "*?["):
        return False
    return bool("/" in token or _PATH_RE.search(token))


def _strip_env_prefix(segment: list[str]) -> list[str]:
    """Drop leading `VAR=value` assignments before the command word."""
    idx = 0
    while idx < len(segment):
        tok = segment[idx]
        if tok.startswith("-") or "=" not in tok.split("/")[0]:
            break
        idx += 1
    return segment[idx:]


def _split_redirects(args: list[str]) -> tuple[list[str], list[str]]:
    """(operands, redirect targets) for one segment's argument list.

    A redirect target is a file the command WRITES, so it must be pulled
    out before operand scanning — otherwise `grep x src.py > out.txt`
    books out.txt as something that was read.
    """
    operands: list[str] = []
    writes: list[str] = []
    idx = 0
    while idx < len(args):
        tok = args[idx]
        if tok in _WRITE_REDIRECTS:
            if idx + 1 < len(args):
                writes.append(args[idx + 1])
            idx += 2
            continue
        operands.append(tok)
        idx += 1
    return operands, writes


def _operand_paths(name: str, args: list[str]) -> list[str]:
    """File operands of one command, minus flags and pattern operands."""
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
        if pending_pattern and not _looks_like_path(tok):
            # The pattern operand. A pattern that DOES look like a path
            # (`grep config.py *.log`) is ambiguous; treating it as the
            # pattern would lose a real file more often than it invents
            # one, so path-shaped tokens fall through to the path branch.
            pending_pattern = 0
            idx += 1
            continue
        pending_pattern = 0
        if _looks_like_path(tok):
            paths.append(tok)
        idx += 1
    return paths


def _resolve(path: str, base: str) -> str:
    """Absolute form of `path` under `base`, or `path` when there is no
    usable base. A consistent key is worth more than a fabricated
    prefix, so an unresolvable relative path is kept verbatim."""
    if path.startswith("/") or not base:
        return path
    return posixpath.normpath(posixpath.join(base, path))


def scan(command: str, cwd: str = "") -> tuple[str | None, list[str],
                                               list[str]]:
    """(kind, read paths, written paths) for one Bash call.

    `kind` is "whole", "slice", or None when the command named no file
    this module is willing to claim as read. The written list exists so
    a caller can tell a re-read from a re-read of something that
    CHANGED — without it, "read, edit, read again" is indistinguishable
    from reading the same unchanged bytes twice, and the second is the
    only one that wasted anything.
    """
    base = cwd or ""
    kind: str | None = None
    reads: list[str] = []
    writes: list[str] = []

    def _add(bucket: list[str], path: str) -> None:
        resolved = _resolve(path, base)
        if resolved not in bucket:
            bucket.append(resolved)

    for raw_segment in _tokenize(command):
        segment = _strip_env_prefix(raw_segment)
        if not segment:
            continue
        name = posixpath.basename(segment[0])
        operands, redirected = _split_redirects(segment[1:])
        for path in redirected:
            if _looks_like_path(path):
                _add(writes, path)
        if name == "cd" and operands:
            base = _resolve(operands[0], base)
            continue
        if name in _WRITE_CMDS:
            for path in _operand_paths(name, operands):
                _add(writes, path)
            continue
        if name not in READ_CMDS:
            continue
        # Narrowing is a property of the PIPELINE, so it is recorded even
        # for a stage that names no file of its own — that is exactly the
        # `cat f | grep x` shape, where the narrowing stage is the one
        # without the path.
        stage = "whole" if name in WHOLE_CMDS else "slice"
        kind = "slice" if (kind == "slice" or stage == "slice") else "whole"
        for path in _operand_paths(name, operands):
            _add(reads, path)
    if not reads:
        return None, [], writes
    return kind, reads, writes
