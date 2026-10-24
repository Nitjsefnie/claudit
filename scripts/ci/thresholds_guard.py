#!/usr/bin/env python3
"""Fail when the CI ratchet data moves backwards by hand.

The bots raise coverage calibrations and tighten shrunk files and
counts. Feature commits may raise measured cost budgets.
This guard pins the DIRECTION of every change a pull request or master
push makes to ``.github/ci-thresholds.json``, so the never-raise/never-lower
rule no longer depends on review:

- a coverage ``floor`` or ``measured`` value never decreases;
- a suite_cost or reparse ``floor`` or ``measured`` value increases only
  when the judged base..head range includes a subject starting with
  ``feat:`` or ``feat(scope):``; missing or unreadable history fails closed;
- suite_cost's workload identity remains seed-only, even for features;
- a baseline entry is never raised;
- an entry is added only when it seeds a brand-new member, or — for a
  member the base already carries — when its path lies OUTSIDE the
  frozen core families (the Python and src/ JavaScript scope) AND
  inside the measured set, the
  one-time family seed of a reviewed gate-definer change. An
  addition under the frozen core, or for a path no ratchet measures, is
  a hand-add and fails.

An entry's removal requires a ``Thresholds-Removed: <entry>=<kind>``
trailer in the judged range, with kind ``bug`` or ``definition``, unless
it is a path-typed baseline entry whose subject is no longer tracked.
A bare baseline family name declares every entry in that family.
Lowers and the one-time seed of a family the base predates pass. Size
and suppression seeds have separate committed-tree tests. Comment
shares permit reductions until master tightens.

The guard runs from the tree under test, like every other gate step: it
shares the steps' threat model, closing the accidental and
quietly-reviewed hand-raise, not a reviewer-subverted change.

  python3 scripts/ci/thresholds_guard.py --base BASE.json --head HEAD.json \\
    --base-rev BASE --head-rev HEAD
"""
from __future__ import annotations

import argparse
import importlib
import json
import re
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

if __package__:
    # pylint: disable-next=relative-beyond-top-level,no-name-in-module
    from . import size_baseline, thresholds, thresholds_removed
else:
    thresholds = importlib.import_module('thresholds')
    size_baseline = importlib.import_module('size_baseline')
    thresholds_removed = importlib.import_module('thresholds_removed')


# The size ratchet's frozen core families: an ADDED baseline entry on
# one is a hand-add, so a new big file cannot seed its own entry. New
# measured families seed outside this list.
CORE_PREFIXES = ('backend/', 'scripts/', 'tests/')

COVERAGE_REMEDY = (
    'A coverage floor or measured value is never lowered by hand: the '
    'ratchet raises them on master when a run justifies it.')
NON_FEATURE_REMEDY = (
    'A non-feature change answers a budget breach by trimming its own tests: '
    'dropping redundant cases, merging parametrisations, deleting tests that '
    'repeat another test\'s assertions.')
REPARSE_REMEDY = (
    'A reparse phase budget is never raised by hand without a feat: or '
    'feat(scope): commit in the judged range. ' + NON_FEATURE_REMEDY +
    ' The seed path cannot bypass a breach for test growth, '
    'refactors or fixes.')
BASELINE_REMEDY = (
    'Baseline entries are never raised or added by hand: split the '
    'module, reduce the complexity, or seed a new family through the '
    'loader\'s writer in a reviewed gate-definer change.')
SUITE_COST_REMEDY = (
    'A suite cost budget is never raised by hand without a feat: or '
    'feat(scope): commit in the judged range. ' + NON_FEATURE_REMEDY)
REMOVAL_REMEDY = (
    'Declare removals in the judged range with Thresholds-Removed: '
    '<entry>=<kind>[, <entry>=<kind>], where kind is bug or definition. '
    'Use the full entry name or a bare baseline family name. A path-typed '
    'baseline entry needs no declaration when its subject path is no longer '
    'a tracked file.')

# A stand-in for a member the base predates, so a base missing it still
# validates: it is never compared against (the family is not in
# ``established``), only carried.
_ABSENT_REPARSE = {
    phase: {metric: {'measured': Decimal('2.5'), 'floor': Decimal('4.0')}
            for metric in thresholds.REPARSE_METRICS}
    for phase in thresholds.REPARSE_PHASES
}
_ABSENT_SUITE_COST = {
    phase: {'measured': Decimal('0.0'), 'floor': Decimal('1.5')}
    for phase in thresholds.SUITE_COST_PHASES
}


def _is_core_family(rel):
    """Whether a path is on one of the frozen original families: Python
    under the original directory prefixes, or src/ JavaScript."""
    return ((rel.startswith(CORE_PREFIXES) and rel.endswith('.py'))
            or (rel.startswith('src/')
                and rel.endswith(('.js', '.jsx'))))


def _load_base(path):
    """Load a base document, tolerating members it predates or omits.

    The base is master's trusted bytes, so a stand-in for an absent cost
    family is never compared against — the family is not in
    ``established``.

    Returns the normalised document and the members its bytes actually
    carried — a member the base predates is a brand-new one, and its
    entries are the introducing change's seed. The base is
    master's (or the parent's) committed data — trusted bytes — so the
    strict duplicate-key/non-finite checks are not re-run here; every
    structural rule still goes through normalise.
    """
    data = json.loads(
        Path(path).read_bytes(),
        parse_float=Decimal, parse_int=Decimal)
    established = {member for member in thresholds.BASELINE_MEMBERS
                   if member in data}
    for member in thresholds.BASELINE_MEMBERS:
        data.setdefault(member, {})
    if thresholds.REPARSE_FAMILY in data:
        established.add(thresholds.REPARSE_FAMILY)
    else:
        # A base without a reparse budget takes the stand-in rather than
        # a family's worth of phases that are not there to compare. An
        # ASSIGNMENT, not a setdefault: the empty spelling has the key
        # already, and phase comparison needs the shape.
        data[thresholds.REPARSE_FAMILY] = {
            phase: dict(record) for phase, record in _ABSENT_REPARSE.items()}
    if data.get(thresholds.SUITE_COST_FAMILY):
        established.add(thresholds.SUITE_COST_FAMILY)
    else:
        # A base that records no budget — the family predates it, or it
        # is present and empty — takes the stand-in rather than a
        # family's worth of phases that are not there to compare. An
        # ASSIGNMENT, not a setdefault: the empty spelling already has
        # the key, and leaving it would hand the phase comparison an
        # empty mapping to index into.
        data[thresholds.SUITE_COST_FAMILY] = {
            phase: dict(record)
            for phase, record in _ABSENT_SUITE_COST.items()}
    # Strict, and by fact: the stand-in above guarantees a family.
    return thresholds.normalise(data), established


def _coverage_moves(base, head):
    """A coverage value that decreases: the ratchet only ever raises."""
    moves = []
    before_coverage = base.get('coverage', {})
    after_coverage = head.get('coverage', {})
    for language in thresholds.COVERAGE_LANGUAGES:
        before = before_coverage.get(language)
        after = after_coverage.get(language)
        if before is None:
            continue
        if after is None:
            continue
        for field in ('measured', 'floor'):
            if after[field] < before[field]:
                moves.append(
                    f'coverage.{language}.{field}: '
                    f'{before[field]} -> {after[field]}')
    return moves


def _suite_cost_moves(base, head, established, feature_present):
    """A suite cost raise without a feature, or any moved workload identity.

    Each phase's number is a cost ceiling, so its ratchet only ever
    moves down: an upward move requires a feature commit in the judged
    range, a downward one is the tighten. The workload identity
    (``tests_tree_lines``, issue #524) binds a seed to the workload it
    measured and arrives with a new family's seed. On an established
    family, an added or changed identity is a hand-edit and fails;
    removal is checked separately. A family the base predates carries
    no record, so the change
    introducing one is its seed and not a move at all.
    """
    if thresholds.SUITE_COST_FAMILY not in established:
        return []
    moves = []
    before = base[thresholds.SUITE_COST_FAMILY]
    after = head.get(thresholds.SUITE_COST_FAMILY)
    if not after:
        return []
    for phase in thresholds.SUITE_COST_PHASES:
        for field in ('measured', 'floor'):
            if not feature_present and after[phase][field] > before[phase][field]:
                moves.append(
                    f'{thresholds.SUITE_COST_FAMILY}.{phase}.{field}: '
                    f'{before[phase][field]} -> {after[phase][field]}')
    before_identity = before.get(thresholds.SUITE_COST_IDENTITY)
    after_identity = after.get(thresholds.SUITE_COST_IDENTITY)
    if after_identity != before_identity:
        moves.append(
            f'{thresholds.SUITE_COST_FAMILY}.'
            f'{thresholds.SUITE_COST_IDENTITY}: '
            f'{before_identity} -> {after_identity}')
    return moves


def _reparse_moves(base, head, established, feature_present):
    """A reparse phase that RISES without a judged feature commit.

    Each phase is a cost ceiling, so its ratchet only ever moves down: an
    upward move requires a feature commit and a downward one is the
    tighten. A family the base predates carries no record, so
    the change introducing one is its seed and not a move at all.
    """
    if feature_present or thresholds.REPARSE_FAMILY not in established:
        return []
    moves = []
    before = base[thresholds.REPARSE_FAMILY]
    after = head.get(thresholds.REPARSE_FAMILY)
    if not after:
        # The head carries no reparse record. Nothing is compared
        # against the stand-in's numbers.
        return []
    for phase in thresholds.REPARSE_PHASES:
        for metric in thresholds.REPARSE_METRICS:
            for field in ('measured', 'floor'):
                if (after[phase][metric][field]
                        > before[phase][metric][field]):
                    moves.append(
                        f'{thresholds.REPARSE_FAMILY}.{phase}.{metric}.'
                        f'{field}: {before[phase][metric][field]} -> '
                        f'{after[phase][metric][field]}')
    return moves


def _baseline_moves(base, head, established):
    """A baseline entry that is raised, or added outside a sanctioned
    seed. The measured set is read once, and only when an addition
    outside the frozen families actually needs it."""
    moves = []
    measured = None
    for member in thresholds.BASELINE_MEMBERS:
        before = base.get(member, {})
        if member not in head:
            continue
        after = head[member]
        if before == after:
            continue
        for path, value in after.items():
            if path not in before:
                if member not in established:
                    continue  # a new member's entries ARE its seed
                # An addition to an established member: only a new
                # measured family's seed can be legitimate.
                if _is_core_family(path):
                    moves.append(f'{member}.{path}: added')
                    continue
                if measured is None:
                    measured = size_baseline.tracked_sizes()
                if path not in measured:
                    moves.append(f'{member}.{path}: added, unmeasured')
            elif value > before[path]:
                moves.append(f'{member}.{path}: {before[path]} -> {value}')
    return moves


def _subject_gone(path: str) -> bool:
    """Only git's unmatched-path status proves the subject is untracked."""
    try:
        proc = subprocess.run(
            ['git', '-C', str(Path(__file__).resolve().parents[2]),
             'ls-files', '--error-unmatch', '--', path],
            capture_output=True, check=False, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 1


def _removal_moves(base, head, established, declarations):
    """One refusal per removed entry lacking either permitted reason."""
    removed = []
    for language in base.get('coverage', {}):
        if language not in head.get('coverage', {}):
            entry = f'coverage.{language}'
            if entry not in declarations:
                removed.append(entry)
    for family in (thresholds.SUITE_COST_FAMILY, thresholds.REPARSE_FAMILY):
        if family in established and not head.get(family) and family not in declarations:
            removed.append(family)
    for family in thresholds.BASELINE_MEMBERS:
        for path in base.get(family, {}):
            entry = f'{family}.{path}'
            if (path not in head.get(family, {})
                    and entry not in declarations and family not in declarations
                    and not _subject_gone(path)):
                removed.append(entry)
    return [f'removed without a reason: {entry}' for entry in removed]


def forbidden_moves(base, head, established, feature_present=False,
                    removal_declarations: set[str] | frozenset[str] = frozenset()):
    """Return the data's forbidden moves from base to head, labelled."""
    return (_coverage_moves(base, head)
            + _reparse_moves(base, head, established, feature_present)
            + _suite_cost_moves(base, head, established, feature_present)
            + _baseline_moves(base, head, established)
            + _removal_moves(base, head, established, removal_declarations))


def _has_feature_commits(base_rev: str | None, head_rev: str) -> bool:
    """One bounded git read; only a readable judged range proves a feature."""
    if not base_rev:
        return False
    try:
        proc = subprocess.run(
            ['git', '-C', str(Path(__file__).resolve().parents[2]),
             'log', '--format=%s', f'{base_rev}..{head_rev}'],
            capture_output=True, check=False, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return False
    # Git separates subjects with LF; other separators can be subject content.
    return any(re.match(r'^feat:|^feat\([^)]*\):', subject)
               for subject in proc.stdout.decode('utf-8', 'replace').split('\n'))


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, required=True,
                        help='the base document (JSON file)')
    parser.add_argument('--head', type=Path, default=thresholds.THRESHOLDS,
                        help='the head document (JSON file)')
    parser.add_argument('--base-rev', help='exclusive base of the judged commit range')
    parser.add_argument('--head-rev', default='HEAD',
                        help='inclusive head of the judged commit range (default HEAD)')
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        head = thresholds.load(args.head)
        base, established = _load_base(args.base)
        moves = forbidden_moves(
            base, head, established,
            feature_present=_has_feature_commits(args.base_rev, args.head_rev),
            removal_declarations=thresholds_removed.declared(
                args.base_rev, args.head_rev, Path(__file__).resolve().parents[2]))
        if not moves:
            print('no forbidden threshold moves')
            return 0
        for move in moves:
            print(f'forbidden move: {move}', file=sys.stderr)
        if any(move.startswith('removed without a reason:') for move in moves):
            print(REMOVAL_REMEDY, file=sys.stderr)
        if any(move.startswith(f'{thresholds.REPARSE_FAMILY}.')
               for move in moves):
            print(REPARSE_REMEDY, file=sys.stderr)
        print(COVERAGE_REMEDY, file=sys.stderr)
        print(BASELINE_REMEDY, file=sys.stderr)
        print(SUITE_COST_REMEDY, file=sys.stderr)
        return 1
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
