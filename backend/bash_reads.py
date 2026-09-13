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
`$1`, an unexpanded `*.py` — yields nothing rather than a guess. A
variable ASSIGNED in the same command (`S=/tmp/s && cat > $S/f.py`) is
text, not runtime, and is expanded. Reads performed by an interpreter
body are not this module's business; the paths a python body opens for
WRITING are, and come from `bash_churn.python_write_paths`.
"""
from __future__ import annotations

import posixpath
import re

from backend.bash_churn import _NULL_SINKS, _split_heredocs, python_write_paths
from backend.bash_literals import ShellWord, command_options, literal_path, sed_parts, shell_tokens

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

# `$NAME` / `${NAME}` — expanded when NAME is assigned in the same
# command text, dropped otherwise.
_VAR_REF = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
_ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.S)

# A token is a candidate path when it carries a short extension or a
# separator. Bare words are rejected: `grep TODO notes.md` must not book
# "TODO", and a subcommand like `diff --git` is not a file.
_PATH_RE = re.compile(r"\.[A-Za-z0-9_]{1,8}$")

# Shell operators the tokenizer identifies, each of which ends a
# command segment.
_OPERATORS = frozenset({"|", "||", "&&", ";", "&", "|&"})

# Tools whose operands are files they write, not read.
_WRITE_CMDS = frozenset({"tee"})


def _tokenize(command: str) -> list[list[str]]:
    """Split a command line into segments, honouring quotes.

    Quote-preserving shell tokens are used instead of splitting on `|;&`: those
    characters appear constantly INSIDE quoted arguments — a
    `grep 'a\\|b' f.py` alternation is one token, not two segments, and
    splitting it textually both loses the file and invents a segment.
    A command the tokenizer cannot parse (an unbalanced quote, a heredoc body
    spliced in) yields no segments rather than a partial misreading.
    """
    tokens = shell_tokens(command)
    segments: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok.operator and tok in _OPERATORS:
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


def _strip_env_prefix(segment: list[str],
                      env: dict[str, str]) -> list[str]:
    """Drop leading `VAR=value` assignments before the command word,
    recording each so a later `$VAR` in the same command resolves."""
    idx = 0
    while idx < len(segment):
        tok = segment[idx]
        if tok.startswith("-") or "=" not in tok.split("/")[0]:
            break
        m = _ASSIGN.match(tok)
        if m:
            value = ShellWord(m.group(2), getattr(tok, "literal", True))
            value.expansion = getattr(tok, "expansion", tok)[m.start(2):]
            env[m.group(1)] = _expand(value, env) or ""
        idx += 1
    return segment[idx:]


def _expand(token: str, env: dict[str, str]) -> str | None:
    """`token` with every `$VAR` replaced from `env`, or None when any
    `$` survives — `$1`, `$(cmd)`, a variable this command did not
    assign: a path built at runtime."""
    if isinstance(token, ShellWord) and token.literal:
        return str(token)

    def _sub(m: re.Match[str]) -> str:
        return env.get(m.group(1) or m.group(2) or "", m[0])
    out = _VAR_REF.sub(_sub, getattr(token, "expansion", token))
    return None if any(c in out for c in "$`*?[") else out.replace("\x00", "$")


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
        if getattr(tok, "operator", True) and re.fullmatch(r"[0-9]*>>?", tok):
            if idx + 1 < len(args):
                writes.append(args[idx + 1])
            idx += 2
            continue
        operands.append(tok)
        idx += 1
    return operands, writes


def _operand_paths(name: str, args: list[str]) -> list[str]:
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


def _sed_in_place(args: list[str]) -> bool:
    return sed_parts(args)[2]


def _destination_paths(name: str, args: list[str]) -> list[str]:
    """cp/install/mv destinations; directory facts must be in the text."""
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
        return []
    flags = dict(options)
    if name == "install" and any(f in flags for f in ("-d", "--directory")):
        return []
    no_directory = any(f in flags for f in ("-T", "--no-target-directory"))
    directory = any(f in flags for f in ("-t", "--target-directory"))
    target = flags.get("-t", flags.get("--target-directory"))
    if directory:
        sources = operands
    elif len(operands) >= 2:
        *sources, target = operands
    else:
        return []
    if target is None or not literal_path(target) or not sources or (directory and no_directory):
        return []
    directory |= not no_directory and (target.endswith("/") or target in (".", "..") or len(sources) > 1)
    if directory:
        return [ShellWord(posixpath.join(target, posixpath.basename(s.rstrip("/"))))
                for s in sources if literal_path(s) and s.rstrip("/") not in (".", "..")]
    return [target] if len(sources) == 1 else []


def _perl_paths(args: list[str]) -> list[str]:
    """In-place Perl one-liners; program and option arguments are not files."""
    modes = {"-" + c: 0 for c in "pnwWl"}
    modes.update({"-" + c: 1 for c in "eEIMmF"})
    modes["-i"] = 2
    try:
        options, files = command_options(args, modes)
    except ValueError:
        return []
    flags = dict(options)
    return [p for p in files if literal_path(p)] if "-i" in flags and ("-e" in flags or "-E" in flags) else []


def _resolve(path: str, base: str) -> str:
    """Absolute form of `path` under `base`, or `path` when there is no
    usable base. A consistent key is worth more than a fabricated
    prefix, so an unresolvable relative path is kept verbatim."""
    if path.startswith("/") or not base:
        return path
    return posixpath.normpath(posixpath.join(base, path))


class _Scan:
    """One command's running state: the cwd as `cd` moves it, the
    variables the command itself assigned, and the three answers."""

    def __init__(self, cwd: str) -> None:
        self.base = cwd or ""
        self.env: dict[str, str] = {}
        self.kind: str | None = None
        self.reads: list[str] = []
        self.writes: list[str] = []

    def add(self, bucket: list[str], path: str) -> None:
        expanded = _expand(path, self.env)
        if not expanded or expanded in _NULL_SINKS:
            return
        resolved = _resolve(expanded, self.base)
        if resolved not in bucket:
            bucket.append(resolved)

    def segment(self, raw_segment: list[str]) -> None:
        segment = _strip_env_prefix(raw_segment, self.env)
        if not segment:
            return
        name = posixpath.basename(segment[0])
        operands, redirected = _split_redirects(segment[1:])
        for path in redirected:
            if literal_path(path) or _looks_like_path(path):
                self.add(self.writes, path)
        if name == "cd" and operands:
            target = _expand(operands[0], self.env) or operands[0]
            self.base = _resolve(target, self.base)
            return
        if name in ("cp", "install", "mv", "perl"):
            paths = _perl_paths(operands) if name == "perl" else _destination_paths(name, operands)
            for path in paths:
                self.add(self.writes, path)
            return
        if name in _WRITE_CMDS or (name == "sed" and _sed_in_place(operands)):
            for path in _operand_paths(name, operands):
                self.add(self.writes, path)
            return
        if name not in READ_CMDS:
            return
        # Narrowing is a property of the PIPELINE, so it is recorded even
        # for a stage that names no file of its own — that is exactly the
        # `cat f | grep x` shape, where the narrowing stage is the one
        # without the path.
        stage = "whole" if name in WHOLE_CMDS else "slice"
        narrowed = self.kind == "slice" or stage == "slice"
        self.kind = "slice" if narrowed else "whole"
        for path in _operand_paths(name, operands):
            self.add(self.reads, path)


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
    state = _Scan(cwd)
    # Heredoc bodies are payload, not command line: a docstring that
    # mentions INDEX.md did not read it.
    _, outside = _split_heredocs(command)
    for raw_segment in _tokenize(outside):
        state.segment(raw_segment)
    for path in python_write_paths(command):
        state.add(state.writes, path)
    if not state.reads:
        return None, [], state.writes
    return state.kind, state.reads, state.writes
