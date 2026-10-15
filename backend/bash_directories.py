"""mkdir operands and explicit mktemp templates; callers resolve against cwd.

Unknown options/operators refuse. bash_reads also scans captured commands.
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
    """mkdir directory operands without globs."""
    try:
        _, operands = command_options(operands, _MKDIR_MODES)
    except ValueError:
        return []
    return [tok for tok in operands if _no_glob_chars(tok)]


def _mktemp_targets(operands: list[str]) -> list[str]:
    """Explicit mktemp template, verbatim; default names depend on $TMPDIR.

    Refuse missing templates, -p/--tmpdir (relative-base relocation) and
    -u/--dry-run (print without creating).
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


_DIRECTORY_TARGETS: dict[str, Callable[[list[str]], list[str]]] = {
    "mkdir": _mkdir_targets,
    "mktemp": _mktemp_targets,
}


def directory_targets(name: str, operands: list[str]) -> list[str]:
    """Paths one directory-creating command puts on disk, else empty."""
    if targets := _DIRECTORY_TARGETS.get(name):
        return targets(operands)
    return []
