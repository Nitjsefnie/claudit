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

from typing import cast

from backend.bash_churn import _NULL_SINKS, BashCommand, MAX_COMMAND_CHARS
from backend.bash_segments import (_ASSIGN, _DECLARATION_BUILTINS,
                                   MAX_NESTED_GROUP_DEPTH,
                                   _capture_split, _matching_paren,
                                   _segments, _split_redirects,
                                   _unmatched_close)
from backend.bash_literals import ShellWord, literal_path
from backend.bash_operand import _looks_like_path, _operand_paths
from backend.bash_effects import destination_paths, perl_paths, sed_parts
from backend.bash_directories import directory_targets
from backend.target_paths import resolve_target as _resolve, windows_absolute

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

# `$NAME` / `${NAME}` — expanded when NAME is assigned in the same
# command text, dropped otherwise.
_VAR_REF = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")

# Bash's declaration builtins: `NAME=$( … )` behind one runs the same
# substitution a bare capture does, so the capture scan looks past
# them, and a value they assign carries into later segments exactly
# like a bare assignment's.
# Variable values can grow exponentially across chained assignments. Bound
# each generated value and the sum generated while scanning one command.
MAX_MATERIALIZED_EXPANSION_CHARS = MAX_COMMAND_CHARS
MAX_SCAN_MATERIALIZED_EXPANSION_CHARS = 4 * MAX_COMMAND_CHARS

# Tools whose operands are files they write, not read.
_WRITE_CMDS = frozenset({"tee"})


def _strip_env_prefix(segment: list[str], env: dict[str, str | None],
                      state: _Scan) -> list[str] | None:
    """Drop leading `VAR=value` assignments before the command word,
    recording each so a later `$VAR` in the same command resolves.

    A declaration builtin leads only when an assignment argument follows
    it: the builtin's remaining words are identifier arguments bash
    rejects, never a command that runs, so `export cat f.py` refuses the
    whole segment (None) instead of reading `cat` as the command word.
    Its option flags (`local -r`, `declare -rx`, `--`) are transparent
    (#770): skipped like the builtin, so a chained assignment behind
    them still carries into the capture's inner scan.
    """
    idx = 0
    builtin_lead = False
    while idx < len(segment):
        tok = segment[idx]
        if tok in _DECLARATION_BUILTINS and "=" not in tok:
            builtin_lead = True
            idx += 1
            continue
        if builtin_lead and tok.startswith("-"):
            idx += 1
            continue
        if tok.startswith("-") or "=" not in tok.split("/")[0]:
            break
        m = _ASSIGN.match(tok)
        if m:
            value = ShellWord(m.group(2), getattr(tok, "literal", True))
            value.expansion = getattr(tok, "expansion", tok)[m.start(2):]
            # Assignment values do not undergo field splitting. Retain unknown
            # bindings explicitly so an unknown IFS still disables inference.
            env[m.group(1)] = _expand(value, env, state)
        idx += 1
    rest = segment[idx:]
    if builtin_lead and rest:
        return None
    return rest


def _expansion_size(token: str, env: dict[str, str | None],
                    source: str) -> tuple[int | None, bool]:
    """Measure a valid expansion without materializing its substituted value."""
    expansion_length = 0
    cursor = 0
    has_reference = False
    valid = True
    for match in _VAR_REF.finditer(source):
        literal = source[cursor:match.start()]
        if any(char in literal for char in "$`*?["):
            valid = False
            break
        expansion_length += len(literal)
        value = env.get(match.group(1) or match.group(2) or "")
        if getattr(token, "unquoted_expansion", False) and (
                not value or "IFS" in env or any(c.isspace() for c in value)):
            valid = False
            break
        if value is None or any(char in value for char in "`*?["):
            valid = False
            break
        expansion_length += len(value)
        has_reference = True
        cursor = match.end()

    if valid:
        literal = source[cursor:]
        if any(char in literal for char in "$`*?["):
            valid = False
        else:
            expansion_length += len(literal)
    return (expansion_length if valid else None), has_reference


def _expand(token: str, env: dict[str, str | None],
            state: _Scan) -> str | None:
    """`token` with every `$VAR` replaced from `env`, or None when any
    `$` survives — `$1`, `$(cmd)`, a variable this command did not
    assign: a path built at runtime."""
    if isinstance(token, ShellWord) and token.literal:
        return str(token)
    if state.expansion_budget_exceeded:
        return None

    source = getattr(token, "expansion", token)
    expansion_length, has_reference = _expansion_size(token, env, source)
    if expansion_length is None:
        return None
    materialized = has_reference or "\x00" in source
    if materialized and (
            expansion_length > MAX_MATERIALIZED_EXPANSION_CHARS
            or not state.reserve_expansion(expansion_length)):
        return None

    if has_reference:
        def _sub(match: re.Match[str]) -> str:
            value = env.get(match.group(1) or match.group(2) or "")
            if value is None:
                return match[0]
            # Parameter expansion does not evaluate dollars from the value again.
            return value.replace("$", "\x00")

        out = _VAR_REF.sub(_sub, source)
    else:
        out = source
    return out.replace("\x00", "$")


def _sed_in_place(args: list[str]) -> bool:
    return sed_parts(args)[2]


class _Scan:
    """One command's running state: the cwd as `cd` moves it, the
    variables the command itself assigned, and the three answers."""

    def __init__(self, cwd: str, depth: int = 0) -> None:
        self.base: str | None = cwd or ""
        self.depth = depth
        self.env: dict[str, str | None] = {}
        self.expansion_chars = 0
        self.expansion_budget_exceeded = False
        self.kind: str | None = None
        self.reads: list[str] = []
        self.writes: list[str] = []

    def reserve_expansion(self, size: int) -> bool:
        """Reserve characters for one accepted materialized expansion."""
        if self.expansion_budget_exceeded:
            return False
        if self.expansion_chars + size > MAX_SCAN_MATERIALIZED_EXPANSION_CHARS:
            self.expansion_budget_exceeded = True
            return False
        self.expansion_chars += size
        return True

    def add(self, bucket: list[str], path: str) -> None:
        expanded = _expand(path, self.env, self)
        if not expanded or expanded in _NULL_SINKS:
            return
        resolved = _resolve(expanded, self.base)
        if resolved is not None and resolved not in bucket:
            bucket.append(resolved)

    def record_targets(self, targets: list[str]) -> None:
        """Book paths a command put on disk as write targets."""
        for path in targets:
            self.add(self.writes, path)

    def segment(self, raw_segment: list[str]) -> None:
        """One command segment: a capture, a group, or a plain command.

        A segment whose first token is the `{` reserved word has that
        word stripped before anything else — `{ cat f.py; }`'s commands
        scan as plainly as unbraced ones. A segment whose first token is
        the group's `(` is a subshell (`_subshell`); a capture never
        starts with an operator, so the two do not collide.
        """
        body = raw_segment
        if body and getattr(body[0], "operator", False) and body[0] == "{":
            body = body[1:]
        if body and getattr(body[0], "operator", False) and body[0] == "(":
            self._subshell(body)
            return
        if (capture := _capture_split(body)) is not None:
            self._captured(*capture)
            return
        segment = _strip_env_prefix(body, self.env, self)
        if not segment:
            return
        name = posixpath.basename(segment[0])
        try:
            operands, redirected, inputs = _split_redirects(segment[1:])
        except ValueError:
            return
        self.command_effects(name, operands, redirected, inputs)

    def _subshell(self, raw_segment: list[str]) -> None:
        """Reads and writes of one `( … )` subshell group.

        The commands inside run for real with this scan's cwd, so —
        like a `{ …; }` group's, which reach the scanner as plain
        segments — their reads, slice/whole kind and writes join this
        call's. A `cd` (and every assignment) moves only a nested scan,
        like a capture's, so none of it leaks past the closing paren.
        The group's stdout is not captured, so unlike a capture its
        intake books. A redirect on the closing paren books its target;
        words after the close would not parse in bash at all, so they
        refuse the whole group. An adjacent `((` books nothing only when
        the construct's parens balance — close doubled, expression free
        of an unmatched `)`: that is the one spelling bash terminates as
        its arithmetic command, whose `>` compares rather than
        redirects. Its tail redirect still books its write: bash opens
        the file before the (failing) evaluation, though the compound
        itself stays unmodelled (issue #785).
        An expression carrying an unmatched `)` — or an undoubled close —
        never balances, and bash re-lexes and RUNS the construct as
        nested subshells; the general body scan below models exactly
        that, bounded by the same depth guard as any other group.
        """
        if self.depth >= MAX_NESTED_GROUP_DEPTH:
            return
        close = _matching_paren(raw_segment, 0)
        if close is None:
            return
        body = raw_segment[1:close]
        balanced_arith = (getattr(raw_segment[0], "double_paren", False)
                          and getattr(raw_segment[close], "double_paren", False)
                          and not _unmatched_close(raw_segment[2:close - 1]))
        try:
            tail_operands, tail_writes, tail_inputs = _split_redirects(
                raw_segment[close + 1:])
        except ValueError:
            return
        if tail_operands:
            return
        if balanced_arith:
            self._group_tail(tail_writes, [], had_reads=False)
            return
        nested = _Scan(self.base or "", self.depth + 1)
        nested.env = dict(self.env)
        for sub in _segments(cast("list[ShellWord]", body)):
            nested.segment(sub)
        for path in nested.reads:
            if path not in self.reads:
                self.reads.append(path)
        if nested.reads:
            self.kind = ("slice"
                         if "slice" in (self.kind, nested.kind)
                         else self.kind if self.kind is not None else nested.kind)
        for path in nested.writes:
            self.add(self.writes, path)
        self._group_tail(tail_writes, tail_inputs, had_reads=bool(nested.reads))

    def _group_tail(self, tail_writes: list[str], tail_inputs: list[str],
                    had_reads: bool) -> None:
        """Booking for what follows a group's closing paren.

        A redirect books its target — a write always, an input as a
        read when the group itself read content. Words after the close
        would not parse in bash at all; their group refused earlier.
        """
        for path in tail_writes:
            if literal_path(path) or _looks_like_path(path):
                self.add(self.writes, path)
        if had_reads:
            for path in tail_inputs:
                if _looks_like_path(path, windows_absolute(self.base)):
                    self.add(self.reads, path)

    def _captured(self, prefix: list[str], inner: list[ShellWord]) -> None:
        """Writes of one `NAME=$( … )` capture's inner command.

        The commands inside a substitution run for real, so the paths they
        put on disk are recorded — a `mktemp` template, a `mkdir` operand,
        a redirect — but the substitution's stdout is captured, not
        emitted, so its reads and slice/whole kind are not this scan's
        intake and stay unbooked. A private `_Scan` inherits the parent's
        cwd and assignments (a `cd` inside stays inside) and its writes
        join the parent's.
        """
        if self.depth >= MAX_NESTED_GROUP_DEPTH:
            return
        nested = _Scan(self.base or "", self.depth + 1)
        nested.env = dict(self.env)
        _strip_env_prefix(prefix, nested.env, nested)
        _strip_env_prefix(prefix, self.env, self)
        for sub in _segments(inner):
            nested.segment(sub)
        for path in nested.writes:
            self.add(self.writes, path)

    def command_effects(self, name: str, operands: list[str],
                        redirected: list[str], inputs: list[str]) -> None:
        """Classify a command after redirection syntax has been separated."""
        if name in ("sed", "cp", "install", "mv", "mkdir", "mktemp", "git"):
            # Preserve positions and unknown words; dropping an unresolved
            # option value would shift the following file into its place.
            operands = [ShellWord(value, operator=getattr(arg, "operator", False))
                        if (value := _expand(arg, self.env, self)) is not None else arg
                        for arg in operands]
        for path in redirected:
            if literal_path(path) or _looks_like_path(path):
                self.add(self.writes, path)
        if name in ("sed", "cp", "install", "mv", "mkdir", "mktemp", "git") and any(
                getattr(arg, "unquoted_expansion", False) for arg in operands):
            # One unresolved/splittable operand can shift every option position.
            return
        if name == "cd" and operands:
            target = _expand(operands[0], self.env, self)
            self.base = _resolve(target, self.base) if target is not None else None
            return
        if name in ("cp", "install", "mv", "perl"):
            paths = perl_paths(operands) if name == "perl" else destination_paths(name, operands, base=self.base)
            for path in paths:
                self.add(self.writes, path)
            return
        if targets := directory_targets(name, operands):
            # A command that CREATES reports what it puts on disk: the
            # directories `mkdir` makes, the checkout `git worktree add`
            # materialises, the clone's directory (or the one the URL
            # derives), and a `mktemp` template named outright.
            self.record_targets(targets)
            return
        if name in _WRITE_CMDS or (name == "sed" and _sed_in_place(operands)):
            for path in _operand_paths(name, operands, windows_absolute(self.base)):
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
        windows = windows_absolute(self.base)
        paths = _operand_paths(name, operands, windows)
        paths.extend(path for path in inputs if _looks_like_path(path, windows))
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
    if not command.command or len(command.command) > MAX_COMMAND_CHARS:
        return None, [], []
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
