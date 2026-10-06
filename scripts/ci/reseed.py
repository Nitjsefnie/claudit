#!/usr/bin/env python3
"""The documented re-seed markers (SV-CI-RATCHETS).

Each cost family whose recorded numbers are a measured workload has the
same sanctioned re-seed: ``suite_cost`` (its pinned bench fixture, its
interpreter pin) and ``reparse`` (a parse-semantics change that
legitimately spends the headroom, issue #698). SV-CI-RATCHETS forbids
raising a recorded budget by hand, and the direction guard refuses an
upward move whenever the base document already carries the family, so a
re-seed whose counts are HIGHER than the recorded ones cannot land as
one change; the delete-then-seed sequence needs an intermediate commit
whose document carries no budget at all, and the loader refuses such a
document.

This module is the one sanctioned way across that intermediate step: a
commit whose MESSAGE carries a family's marker

    [suite-cost-re-seed]        [reparse-re-seed]

may carry a document that family is absent from. Nothing else about the
document changes. Every other field is validated exactly as before, the
guard's upward-move refusal is untouched, and the marker buys the
ABSENCE only — never a raised budget, which is the next commit's seed,
taken from a runner measurement artifact and never hand-derived — and
never another family's absence: the markers do not cross.

WHICH RUN SUPPLIES THAT ARTIFACT. The window a marker opens is the
open one — the seed commit is the change that restores the family, so
its own tree is the one the seed describes — and the seed's source is
a run of exactly that tree: the seed PR's own run on the commit
carrying its final code and tests, or, once the window's changes have
merged, the newest in-window master commit whose run measured. A run
that skipped the tests leg, or whose suite failed, carries no
measurement at all, so neither can be the source. The tree must not
change between that run and the merge, the seed's own write of the
thresholds file excepted; when it does, the source is a fresh run of
the final tree. Recorded counts are never adjusted to fit any tree
(SV-CI-RATCHETS).

WHERE THE MARKER IS READ. A bounded walk per family, never a single
read (issue #511: a tolerance scoped to HEAD/HEAD^2 lasted exactly one
commit — the #509 incident, where the hourly pricing bot landed on the
delete before the seed):

- The walk starts at HEAD and reads back over the ancestry — every
  parent, with the pull-request head's lineage first (a GitHub merge
  has two parents and the head is the second; an octopus merge queues
  them all): a pull-request run checks out the MERGE commit, whose own
  message is GitHub's and carries no marker, and the pull request's
  head — the lineage that declared the marker — hangs off it as the
  second parent.
- The walk ends at the last family-present commit: a commit whose
  committed document carries the family is one where the window the
  marker opens is closed, whatever the commit's message says — a
  marker is honored only where the family it authorises absence from
  is actually gone — so an old declaration below it can never exempt a
  later family-absent document. Presence is judged on the committed
  `.github/ci-thresholds.json` at each visited commit — presence of
  the key only, never its shape: the loader judges a present family's
  shape at the tip, exactly as it does today (an empty family is
  refused, marker or no marker).
- The whole walk is additionally bounded at _MAX_VISITS commits,
  fail-closed: a history with no family-present commit within ten
  visits of HEAD — no thresholds file at all, or a history longer than
  the cap — reads as no marker, and the tree is gated exactly as it
  always was.
- Every commit read fails closed individually: a missing git, an
  unreadable revision (a shallow CI checkout's boundary among them) and
  a message without the marker all read the same way — that commit
  contributes nothing, and the walk continues only where git can still
  answer.

The walks of the two families are independent and run the same shape:
one marker's window says nothing about the other family, whose absence
stays a refusal.
"""
from __future__ import annotations

import functools
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Each cost family's marker, spelled as the doctrine documents it. A
# bracketed token so it stands out in a subject line and cannot be
# reached by an ordinary word.
MARKER = '[suite-cost-re-seed]'
REPARSE_MARKER = '[reparse-re-seed]'

# The re-seedable families, spelled here because importing thresholds
# for them would put the git-reading module on every loader consumer's
# import graph — the same reason thresholds itself reaches this module
# only lazily, inside verdict(). Mirrors thresholds.SUITE_COST_FAMILY /
# thresholds.REPARSE_FAMILY; a test pins the pairs together.
SUITE_COST_FAMILY = 'suite_cost'
REPARSE_FAMILY = 'reparse'
# The pairing the whole mechanism turns on: a marker admits the absence
# of ITS OWN family and no other's, so a suite-cost delete can never
# ride a reparse marker and vice versa.
MARKER_FOR = {
    SUITE_COST_FAMILY: MARKER,
    REPARSE_FAMILY: REPARSE_MARKER,
}
# Where the document sits at any commit, relative to the repository
# root: the file thresholds.THRESHOLDS resolves to. The walk asks git
# for THIS path at each visited commit — the committed history, never
# the working tree.
_THRESHOLDS_AT = '.github/ci-thresholds.json'

# A message is READ here, not executed: git is asked for text, with a
# bound so an unresponsive repository cannot hang a gate step.
_TIMEOUT = 10

# The walk's second bound, after the family-present one: the re-seed
# series is short by construction — the base, the delete, the commits
# that land on the delete before the seed (the hourly bot among them),
# back to the base — and ten visits bound it with room to spare. A
# longer window than that fails closed and needs the walk widened, not
# silently tolerated.
_MAX_VISITS = 10


def _run(root: Path, *args: str) -> subprocess.CompletedProcess | None:
    """One bounded git read, or None when git cannot answer.

    Everything the walk learns comes through here, so the fail-closed
    shape is one helper's shape: a missing git, a signal, a timeout and
    a nonzero exit all read as "no answer".
    """
    try:
        proc = subprocess.run(
            ['git', '-C', str(root), *args],
            capture_output=True, check=False, timeout=_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc


def _revision_message(root: Path, revision: str) -> str | None:
    """One revision's message, or None when git cannot answer for it.

    ``-1`` bounds the read to the revision named and nothing before it,
    which is what keeps this a per-commit read rather than the log of
    the branch.
    """
    proc = _run(root, 'log', '-1', '--format=%B', revision)
    if proc is None:
        return None
    return proc.stdout.decode('utf-8', 'replace')


def _parents(root: Path, revision: str) -> list[str]:
    """A revision's parents, or [] when git cannot answer for it."""
    proc = _run(root, 'show', '-s', '--format=%P', revision)
    if proc is None:
        return []
    return proc.stdout.decode('ascii', 'replace').split()


def _family_present(root: Path, revision: str, family: str) -> bool:
    """Whether the document committed at ``revision`` carries ``family``.

    Presence only. A commit whose document git cannot produce or json
    cannot parse is NOT family-present: the window stays open and the
    walk continues — the visit cap and the shallow boundary of a CI
    checkout are what close it. That is deliberate, not lax: a missing
    file cannot close a window defined on the file's own content, and
    the loader still judges the tip's document on its own bytes.
    """
    proc = _run(root, 'show', f'{revision}:{_THRESHOLDS_AT}')
    if proc is None:
        return False
    try:
        document = json.loads(proc.stdout)
    except ValueError:
        return False
    return isinstance(document, dict) and family in document


def _declares(root: Path, marker: str, family: str) -> bool:
    """Whether any commit the bounded walk reads carries ``marker`` —
    honored only where ``family`` is absent, the one state it authorises."""
    seen: set[str] = set()
    queue: list[str] = ['HEAD']
    while queue and len(seen) < _MAX_VISITS:
        revision = queue.pop(0)
        if revision in seen:
            continue
        seen.add(revision)
        message = _revision_message(root, revision)
        if message is None:
            continue
        # The bound is evaluated FIRST, so a family-present commit
        # closes the window whatever its message says — a marker is
        # honored only where the family it authorises absence from is
        # actually gone (the sanctioned delete is family-absent).
        if _family_present(root, revision, family):
            continue
        if marker in message:
            return True
        # Every parent, reversed: at a pull-request merge the LAST
        # listed parent is the pull request's head — the lineage that
        # declares — so it is visited first.
        queue.extend(reversed(_parents(root, revision)))
    return False


@functools.lru_cache(maxsize=16)
def in_flight(root: Path | None = None,
              family: str = SUITE_COST_FAMILY) -> bool:
    """Whether this tree declares a re-seed in flight for ``family``.

    Cached because the loader asks on every read and the suite reads it
    hundreds of times: one walk per tree and family per process. A
    caller that changes the tree underneath itself (a test building a
    repository in a tmp path) calls ``clear_cache()`` first.

    The default family is the suite-cost one, the shape every caller
    predating the second marker spelled; the loader asks for each
    family in turn and the two walks are independent — a reparse
    marker opens no suite-cost window and vice versa.
    """
    return _declares(ROOT if root is None else root,
                     MARKER_FOR[family], family)


def clear_cache() -> None:
    """Drop the cached probe so a caller may read another tree."""
    in_flight.cache_clear()
