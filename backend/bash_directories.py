"""Directory targets: what mkdir, mktemp and git put on disk.

The writer family behind issue #655: a seat's worktrees, clones and
scratch directories are usually the largest things it leaves behind, and
only file writes were recorded. Each command here maps to the operands
it CREATES, resolved against the command's cwd by the caller.

`command_options` refuses any flag the command's modes dict does not
list, so an unparsable command names nothing rather than a guessed path.
Command-substitution forms (`d=$(mktemp …)`) are not scanned: the
tokenizer's operator refusal is unchanged.
"""
from __future__ import annotations

from collections.abc import Callable

from backend.bash_literals import ShellWord, command_options

_MKDIR_MODES: dict[str, int] = dict.fromkeys(
    ("-p", "--parents", "-v", "--verbose", "-Z", "--context"), 0)
_MKDIR_MODES.update(dict.fromkeys(("-m", "--mode"), 1))

_MKTEMP_MODES: dict[str, int] = dict.fromkeys(
    ("-d", "--directory", "-q", "--quiet", "-u", "--dry-run"), 0)
_MKTEMP_MODES.update({"-p": 1, "--tmpdir": 2})

_WORKTREE_MODES: dict[str, int] = dict.fromkeys(
    ("-f", "--force", "--detach", "--checkout", "--lock", "--no-track",
     "--guess-remote", "--overwrite", "--orphan", "--bare",
     "-q", "--quiet", "--verbose"), 0)
_WORKTREE_MODES.update({"--track": 2, "--reason": 1})
_WORKTREE_MODES.update(dict.fromkeys(("-b", "-B"), 1))

_CLONE_MODES: dict[str, int] = dict.fromkeys(
    ("-l", "--local", "--no-hardlinks", "-s", "--shared", "-n",
     "--no-checkout", "--bare", "--mirror", "-q", "--quiet", "--verbose",
     "--progress", "--no-single-branch", "--no-tags", "--sparse",
     "--dissociate", "--also-filter-submodules"), 0)
_CLONE_MODES.update({"-c": 1, "--recurse-submodules": 2})
_CLONE_MODES.update(dict.fromkeys(
    ("-b", "-B", "--branch", "-o", "--origin", "--template", "--reference",
     "-u", "--upload-pack", "--depth", "--deepen", "-j", "--jobs",
     "--filter", "--bundle-uri", "--separate-git-dir", "--ref-format",
     "--server-option", "--shallow-exclude", "--shallow-since"), 1))


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


def _clone_name(url: str) -> str | None:
    """The directory name a bare `git clone URL` derives from the URL."""
    name = url.rstrip("/").rsplit("/", 1)[-1]
    if name.endswith(".git"):
        name = name[:-len(".git")]
    return name if name and _no_glob_chars(name) else None


def _worktree_add_targets(args: list[str]) -> list[str]:
    """The <path> operand of a `git worktree add`, its first non-flag."""
    try:
        _, operands = command_options(args, _WORKTREE_MODES)
    except ValueError:
        return []
    for tok in operands[:1]:
        if _no_glob_chars(tok):
            return [tok]
    return []


def _clone_targets(args: list[str]) -> list[str]:
    """`git clone`'s directory operand, or the name derived from the URL.

    A derived name keeps the URL token's literal provenance, so a quoted
    `'$NAME'` books the verbatim text and an unresolvable one is dropped.
    """
    try:
        _, operands = command_options(args, _CLONE_MODES)
    except ValueError:
        return []
    if len(operands) >= 2:
        return [operands[1]] if _no_glob_chars(operands[1]) else []
    if operands:
        name = _clone_name(operands[0])
        if name and getattr(operands[0], "literal", True):
            return [ShellWord(name)]
    return []


def _git_write_targets(operands: list[str]) -> list[str]:
    """The checkout a `git worktree add` or `git clone` creates.

    `git -C …` and other global options before the subcommand refuse,
    because they move the repository the subcommand acts on.
    """
    if not operands or operands[0].startswith("-"):
        return []
    subcommand, args = operands[0], operands[1:]
    if subcommand == "worktree" and args and args[0] == "add":
        return _worktree_add_targets(args[1:])
    if subcommand == "clone":
        return _clone_targets(args)
    return []


# The directory-creating commands, each mapping to the operands it puts
# on disk.
_DIRECTORY_TARGETS: dict[str, Callable[[list[str]], list[str]]] = {
    "mkdir": _mkdir_targets,
    "mktemp": _mktemp_targets,
    "git": _git_write_targets,
}


def directory_targets(name: str, operands: list[str]) -> list[str]:
    """Paths one directory-creating command puts on disk, else empty."""
    if targets := _DIRECTORY_TARGETS.get(name):
        return targets(operands)
    return []
