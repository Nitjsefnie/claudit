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

Only paths the command text names outright are returned; churn estimates
do not manufacture target paths. A path built at runtime — `glob.glob(...)`,
`$1`, an unexpanded `*.py` — yields nothing rather than a guess. A
variable ASSIGNED in the same command (`S=/tmp/s && cat > $S/f.py`) is
text, not runtime, and is expanded. Reads performed by an interpreter
body are not this module's business; the paths a python body opens for
WRITING are, and come from `bash_churn.python_write_paths`.
"""
from __future__ import annotations

import posixpath
import re

from backend.bash_churn import _NULL_SINKS, BashCommand
from backend.bash_literals import ShellWord, destination_paths, literal_path, perl_paths, sed_parts

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


def _segments(tokens: list[ShellWord]) -> list[list[str]]:
    """Split decoded shell words into command segments, honouring quotes.

    Quote-preserving shell tokens are used instead of splitting on `|;&`: those
    characters appear constantly INSIDE quoted arguments — a
    `grep 'a\\|b' f.py` alternation is one token, not two segments, and
    splitting it textually both loses the file and invents a segment.
    A command the tokenizer cannot parse (an unbalanced quote, a heredoc body
    spliced in) yields no segments rather than a partial misreading.
    """
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
                      env: dict[str, str | None]) -> list[str]:
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
            # Assignment values do not undergo field splitting. Retain unknown
            # bindings explicitly so an unknown IFS still disables inference.
            env[m.group(1)] = _expand(value, env)
        idx += 1
    return segment[idx:]


def _expand(token: str, env: dict[str, str | None]) -> str | None:
    """`token` with every `$VAR` replaced from `env`, or None when any
    `$` survives — `$1`, `$(cmd)`, a variable this command did not
    assign: a path built at runtime."""
    if isinstance(token, ShellWord) and token.literal:
        return str(token)

    def _sub(m: re.Match[str]) -> str:
        value = env.get(m.group(1) or m.group(2) or "")
        if getattr(token, "unquoted_expansion", False) and (
                not value or "IFS" in env or any(c.isspace() for c in value)):
            # Refuse uncertain arity instead of guessing shell field splitting.
            return m[0]
        # Parameter expansion does not evaluate dollars from the value again.
        return m[0] if value is None else value.replace("$", "\x00")
    out = _VAR_REF.sub(_sub, getattr(token, "expansion", token))
    return None if any(c in out for c in "$`*?[") else out.replace("\x00", "$")


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
        self.env: dict[str, str | None] = {}
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
        try:
            operands, redirected, inputs = _split_redirects(segment[1:])
        except ValueError:
            return
        self.command_effects(name, operands, redirected, inputs)

    def command_effects(self, name: str, operands: list[str],
                        redirected: list[str], inputs: list[str]) -> None:
        """Classify a command after redirection syntax has been separated."""
        if name in ("sed", "cp", "install", "mv"):
            # Preserve positions and unknown words; dropping an unresolved
            # option value would shift the following file into its place.
            operands = [ShellWord(value, operator=getattr(arg, "operator", False))
                        if (value := _expand(arg, self.env)) is not None else arg
                        for arg in operands]
        for path in redirected:
            if literal_path(path) or _looks_like_path(path):
                self.add(self.writes, path)
        if name in ("sed", "cp", "install", "mv") and any(
                getattr(arg, "unquoted_expansion", False) for arg in operands):
            # One unresolved/splittable operand can shift every option position.
            return
        if name == "cd" and operands:
            target = _expand(operands[0], self.env) or operands[0]
            self.base = _resolve(target, self.base)
            return
        if name in ("cp", "install", "mv", "perl"):
            paths = perl_paths(operands) if name == "perl" else destination_paths(name, operands)
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
        paths = _operand_paths(name, operands)
        paths.extend(path for path in inputs if _looks_like_path(path))
        for path in paths:
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
    return scan_command(BashCommand(command), cwd)


def scan_command(command: BashCommand, cwd: str = "") -> tuple[str | None, list[str], list[str]]:
    """Read/write access using the syntax already parsed for this Bash call."""
    state = _Scan(cwd)
    # Heredoc bodies are payload, not command line: a docstring that
    # mentions INDEX.md did not read it.
    for raw_segment in _segments(command.tokens):
        state.segment(raw_segment)
    for path in command.write_paths():
        state.add(state.writes, path)
    if not state.reads:
        return None, [], state.writes
    return state.kind, state.reads, state.writes
