#!/usr/bin/env python3
"""The documented suite-cost re-seed marker (SV-CI-RATCHETS).

SV-CI-RATCHETS sanctions a ``suite_cost`` re-seed when the pinned
workload legitimately changes — the fixture list, the interpreter pin,
or the code those tests execute — and forbids raising a recorded budget
by hand. The direction guard refuses an upward move whenever the base
document already carries the family, so a re-seed whose counts are
HIGHER than the recorded ones cannot land as one change; the
delete-then-seed sequence needs an intermediate commit whose document
carries no family at all, and the loader refuses such a document.

This module is the one sanctioned way across that intermediate step: a
commit whose MESSAGE carries the marker

    [suite-cost-re-seed]

may carry a document the family is absent from. Nothing else about the
document changes. Every other field is validated exactly as before, the
guard's upward-move refusal is untouched, and the marker buys the
ABSENCE only — never a raised budget, which is the next commit's seed,
taken from a runner measurement artifact and never hand-derived.

WHERE THE MARKER IS READ. Two revisions, never a walk:

- ``HEAD``. A push checkout, and a master tip after its rebase, IS the
  declaring commit.
- ``HEAD^2``, which resolves only for a merge commit. A pull-request
  run checks out the MERGE commit, whose own message is GitHub's and
  carries no marker; its second parent is the pull request's head,
  which is what declared it. One level, and only when the object is
  there: a shallow checkout that lacks the parent simply does not see
  the marker, which fails CLOSED — the tree is gated exactly as it is
  today — never open.

A missing git, an unreadable revision and a marker two commits back all
read the same way: no marker, and the tree is gated as it always was.
"""
from __future__ import annotations

import functools
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# The marker's own spelling, as the doctrine documents it. It is a
# bracketed token so it stands out in a subject line and cannot be
# reached by an ordinary word.
MARKER = '[suite-cost-re-seed]'

# A message is READ here, not executed: git is asked for text, with a
# bound so an unresponsive repository cannot hang a gate step.
_TIMEOUT = 10


def _revision_message(root: Path, revision: str) -> str | None:
    """One revision's message, or None when git cannot answer for it.

    ``-1`` bounds the walk to the revision named and nothing before it,
    which is what makes this a two-revision read rather than the log of
    the branch.
    """
    try:
        proc = subprocess.run(
            ['git', '-C', str(root), 'log', '-1', '--format=%B', revision],
            capture_output=True, check=False, timeout=_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.decode('utf-8', 'replace')


@functools.lru_cache(maxsize=8)
def declared(root: Path | None = None) -> tuple[str, ...]:
    """The messages this tree's declaring commit is read from.

    Cached because the loader asks on every read and the suite reads it
    hundreds of times: one probe per tree per process. A caller that
    changes the tree underneath itself (a test building a repository in
    a tmp path) calls ``clear_cache()`` first.
    """
    root = ROOT if root is None else root
    head = _revision_message(root, 'HEAD')
    if head is None:
        return ()
    # HEAD^2 resolves only for a merge commit, so asking for it IS the
    # merge test: a push checkout's HEAD has one parent, the lookup
    # fails, and that failure is the answer we want.
    second = _revision_message(root, 'HEAD^2')
    if second is None or second == head:
        return (head,)
    return (head, second)


def in_flight(root: Path | None = None) -> bool:
    """Whether this tree declares a suite-cost re-seed in flight."""
    return any(MARKER in message for message in declared(root))


def clear_cache() -> None:
    """Drop the cached probe so a caller may read another tree."""
    declared.cache_clear()
