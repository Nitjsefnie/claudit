"""Retain counted heredoc writes when a later shell stage fails."""
from __future__ import annotations

import posixpath
import re
import shlex

from backend.bash_churn import BashCommand, MAX_COMMAND_CHARS
from backend.bash_heredocs import (
    _FD_TARGET,
    _NULL_SINKS,
    _REDIRECT,
    _TEE,
)


# Shell-level failures of the write itself. The first two poison every
# write in the command; the rest name the path they refused.
_DISK_FAILURE = re.compile(r"No space left on device|Read-only file system")
_TARGET_FAILURE = ("No such file or directory", "Permission denied",
                   "Is a directory", "cannot create")
_CANNOT_CREATE_DIR = re.compile(r"cannot create directory [`'\"]([^'`\"]+)")
_STAGE_SPLIT = re.compile(r"&&|\|\||;|\||\n")

# A shell operator or heredoc marker as one raw shlex token — never a
# path, so the tee arm never books it (#820). The char classes carry a
# redirect's fd prefix and its digit/`-` target (`>&2`, `2>&1`); `<<`
# absorbs the marker's tag and a `<`-led form its input target (#855).
_REDIRECT_TOKEN = re.compile(r"[0-9]*(?:>>?|<&?)[&|]?[0-9-]*|<<\S*|<[^\s;&|]*")


def _verbatim_targets(context: str) -> list[str]:
    """Files a `cat > F` / `tee F` heredoc opener lands its body in."""
    targets = [m.group(1) or m.group(2) or m.group(3)
               for m in _REDIRECT.finditer(context)]
    if _TEE.search(context):
        try:
            tokens = shlex.split(context, posix=True)
        except ValueError:
            tokens = []
        seen_tee = False
        for tok in tokens:
            if seen_tee and tok and not tok.startswith("-"):
                if tok in ("|", "||", "&&", ";"):
                    break
                if not _REDIRECT_TOKEN.fullmatch(tok):
                    targets.append(tok)
            seen_tee = seen_tee or posixpath.basename(tok) == "tee"
    return [t for t in targets if t and t not in _NULL_SINKS
            and not _FD_TARGET.fullmatch(t)]


def churn_survives_error(command: str, error_text: str) -> bool:
    """Whether an errored result leaves the command's counted churn
    standing.

    The result's exit status is the LAST stage's. A `cat > f <<EOF`
    heredoc followed by `python3 f` that exits 1 wrote f all the same,
    and that shape — write, then run what was written — is how most
    editing under bypass permissions happens, so zeroing every errored
    call throws those writes away.

    Only a VERBATIM heredoc write (`cat`/`tee`) survives, and only when
    the command has some other stage to fail in and the error text does
    not report the write itself failing (a missing directory, a denied
    path, a full disk). A python body that raised may have raised before
    its write — `assert old in t` is put there to do exactly that — so
    it never survives; a patch that did not apply is the same.
    Approximation: a preceding `&&` stage failing without naming the
    target (a `mkdir` denied on a parent) still counts the heredoc.
    """
    if not command or len(command) > MAX_COMMAND_CHARS:
        return False
    parsed = BashCommand(command)
    targets: list[str] = []
    seen_contexts: set[str] = set()
    for (context, body), kind in zip(parsed.parts[0], parsed.heredoc_kinds):
        if body and kind == "file" and context not in seen_contexts:
            seen_contexts.add(context)
            targets.extend(_verbatim_targets(context))
    if not targets:
        return False
    stages = [s for s in _STAGE_SPLIT.split(parsed.parts[1]) if s.strip()]
    return len(stages) >= 2 and not _write_reported_failed(targets, error_text)


def _write_reported_failed(targets: list[str], error_text: str) -> bool:
    """Does the error text say the write to one of `targets` failed?"""
    if _DISK_FAILURE.search(error_text):
        return True
    names = {posixpath.basename(t) for t in targets}
    for line in error_text.splitlines():
        if not any(marker in line for marker in _TARGET_FAILURE):
            continue
        if any(t in line for t in targets) or any(n in line for n in names):
            return True
        m = _CANNOT_CREATE_DIR.search(line)
        if m and any(t.startswith(m.group(1).rstrip("/") + "/")
                     for t in targets):
            return True
    return False
