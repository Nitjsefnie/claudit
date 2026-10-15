"""Directory targets: what mkdir and mktemp put on disk.

Each mkdir operand names a directory to create; mktemp contributes only
an explicit template. The caller resolves these targets against the
command's cwd.

`command_options` refuses any flag the command's modes dict does not
list, so an unparsable command names nothing rather than a guessed path.
Captured substitutions (`d=$(mktemp …)`) are scanned by bash_reads,
which re-runs the inner command through this module's dispatch; the
tokenizer's operator refusal is unchanged.
"""
from __future__ import annotations

from collections.abc import Callable

from backend.bash_literals import command_options

_MKDIR_MODES: dict[str, int] = dict.fromkeys(
    ("-p", "--parents", "-v", "--verbose", "-Z", "--context"), 0)
_MKDIR_MODES.update(dict.fromkeys(("-m", "--mode"), 1))

_MKTEMP_MODES: dict[str, int] = dict.fromkeys(
    ("-d", "--directory", "-q", "--quiet", "-u", "--dry-run"), 0)
_MKTEMP_MODES.update({"-p": 1, "--tmpdir": 2})


def _no_glob_chars(token: str) -> bool:
    return not any(ch in token for ch in "*?[")


def _mkdir_targets(operands: list[str]) -> list[str]:
    """Every operand of a `mkdir` is a directory it creates."""
    try:
        _, operands = command_options(operands, _MKDIR_MODES)
    except ValueError:
        return []
    return [tok for tok in operands if _no_glob_chars(tok)]


def _mktemp_targets(operands: list[str]) -> list[str]:
    """The template operand of a `mktemp`, verbatim.

    Without a template the name is runtime (`mktemp -d` → tmp.XXXX under
    $TMPDIR) and yields nothing; a `-p`/`--tmpdir` moves a relative
    template's base, and is refused rather than re-derived. A
    `--dry-run`/`-u` prints a name and creates nothing, so it books no
    target.
    """
    try:
        options, operands = command_options(operands, _MKTEMP_MODES)
    except ValueError:
        return []
    if any(flag in ("-u", "--dry-run", "-p", "--tmpdir")
           for flag, _ in options) or not operands:
        return []
    template = operands[0]
    return [template] if _no_glob_chars(template) else []


# The directory-creating commands, each mapping to the operands it puts
# on disk.
_DIRECTORY_TARGETS: dict[str, Callable[[list[str]], list[str]]] = {
    "mkdir": _mkdir_targets,
    "mktemp": _mktemp_targets,
}


def directory_targets(name: str, operands: list[str]) -> list[str]:
    """Paths one directory-creating command puts on disk, else empty."""
    if targets := _DIRECTORY_TARGETS.get(name):
        return targets(operands)
    return []
