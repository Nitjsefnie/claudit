"""Skip guard for tests that read the tree's own git metadata.

CONTRIBUTING.md documents ``pytest -m "not db"`` as the portable subset
and states no other prerequisite, but several of its tests shell out to
git over the tree under test (``git ls-files``, ``git cat-file``,
``git check-ignore``). From a plain source archive — a tree with no
``.git`` — git exits 128 and the test errors instead of skipping
(issue #392). The guard asks one question: would git answer for THIS
tree? ``git rev-parse --show-toplevel`` run inside the tree must
resolve to the tree itself; an enclosing repository's toplevel is not
this tree's metadata, so an archive unpacked inside another checkout
skips too instead of letting git answer for the outer repository.

A git checkout always runs the guarded test: the guard never weakens a
check a checkout enforces today.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

SKIP_REASON = (
    "the tree under test has no git metadata of its own (not a git "
    "checkout): this check reads git's index or objects")


def has_own_git_metadata(root: Path) -> bool:
    """Would git answer for `root` itself, not an enclosing repository?

    False when git is absent, fails, or names a foreign toplevel.
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=root, capture_output=True, check=False)
    except OSError:
        return False
    if proc.returncode != 0:
        return False
    toplevel = os.path.normcase(
        os.path.realpath(proc.stdout.decode().strip()))
    return toplevel == os.path.normcase(os.path.realpath(root))


def require_own_git_metadata(root: Path) -> None:
    """Skip the calling test when `root` has no git metadata of its own.

    The reason names the situation, per issue #392's expectation.
    """
    if not has_own_git_metadata(root):
        pytest.skip(SKIP_REASON)
