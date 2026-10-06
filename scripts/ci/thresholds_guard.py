#!/usr/bin/env python3
"""Fail when the CI ratchet data moves backwards by hand.

The bots are the only writers the data needs (issue #388): ratchet.py
raises a coverage calibration, and the two --tighten passes follow
shrunk files and counts down. This guard pins the DIRECTION of every
change a pull request or master push makes to
``.github/ci-thresholds.json``, so the never-raise/never-lower rule no
longer depends on review:

- a coverage ``floor`` or ``measured`` value never decreases;
- a reparse ``floor`` or ``measured`` value never INCREASES (that family
  is a cost ceiling, so its ratchet only ever tightens downward);
- a baseline entry is never raised;
- an entry is added only when it seeds a brand-new member, or — for a
  member the base already carries — when its path lies OUTSIDE the
  frozen core families (the Python and src/ JavaScript scope the size
  ratchet had when this guard landed) AND inside the measured set, the
  one-time family seed of a reviewed gate-definer change (#393). An
  addition under the frozen core, or for a path no ratchet measures, is
  a hand-add and fails.

Removals and lowers are the bots' own direction and pass, as does the
one-time seed of a family the base predates, and as does a cost
family's removal — the sanctioned re-seed's delete step, whose family
the loader honors as absent anywhere in the bounded walk's span from
HEAD back to the last family-present commit (reseed.py), under that
family's own marker — suite_cost under ``[suite-cost-re-seed]``,
reparse under ``[reparse-re-seed]`` — so commits landing on the
delete before the seed read the absence through the delete's marker.
The truth of seeded values is pinned separately, by the
committed-document-matches-tree tests, which run on the same merge ref.

The guard runs from the tree under test, like every other gate step: it
shares the steps' threat model, closing the accidental and
quietly-reviewed hand-raise, not a reviewer-subverted change.

  python3 scripts/ci/thresholds_guard.py --base BASE.json --head HEAD.json
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
from decimal import Decimal
from pathlib import Path

if __package__:
    # pylint: disable-next=relative-beyond-top-level,no-name-in-module
    from . import size_baseline, thresholds
else:
    thresholds = importlib.import_module('thresholds')
    size_baseline = importlib.import_module('size_baseline')


# The size ratchet's original families, frozen at the guard's landing:
# an ADDED baseline entry on one is a hand-add (the audit's exploit — a
# new big file seeding its own entry), whatever it claims. Future
# measured families seed outside this list (#393's did).
CORE_PREFIXES = ('backend/', 'scripts/', 'tests/')

COVERAGE_REMEDY = (
    'A coverage floor or measured value is never lowered by hand: the '
    'ratchet raises them on master when a run justifies it.')
REPARSE_REMEDY = (
    'A reparse phase budget is never raised by hand: the ratchet only '
    'tightens it downward on master when a cheaper run justifies it, so '
    'buy headroom by making the reparse cheaper, or by the doctrine\'s '
    'own-marker re-seed path when a parse-semantics change legitimately '
    'spends it.')
BASELINE_REMEDY = (
    'Baseline entries are never raised or added by hand: split the '
    'module, reduce the complexity, or seed a new family through the '
    'loader\'s writer in a reviewed gate-definer change.')
SUITE_COST_REMEDY = (
    'A suite cost budget is never raised by hand: the ratchet only '
    'tightens it downward on master when a cheaper run justifies it, so '
    'buy headroom by making the suite cheaper or by the doctrine\'s '
    're-seed path when the fixture list or interpreter pin changes.')

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
    """Load a base document, tolerating members it predates AND members
    a re-seed window has deleted (the loader admits the absence only on
    the declaring lineage; the base is master's trusted bytes, so the
    stand-in is never compared against — the family is not in
    ``established``).

    Returns the normalised document and the members its bytes actually
    carried — a member the base predates is a brand-new one, and its
    entries are the introducing change's seed (#389's is). The base is
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
        # A base that records no reparse budget — the family predates
        # it, or a [reparse-re-seed] delete merged — takes the stand-in
        # rather than a family's worth of phases that are not there to
        # compare. An ASSIGNMENT, not a setdefault: the empty spelling
        # has the key already, and the phase comparison needs the shape.
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
    return thresholds.normalise(data, False), established


def _coverage_moves(base, head):
    """A coverage value that decreases: the ratchet only ever raises."""
    moves = []
    for language in thresholds.COVERAGE_LANGUAGES:
        before = base['coverage'][language]
        after = head['coverage'][language]
        for field in ('measured', 'floor'):
            if after[field] < before[field]:
                moves.append(
                    f'coverage.{language}.{field}: '
                    f'{before[field]} -> {after[field]}')
    return moves


def _suite_cost_moves(base, head, established):
    """A suite cost budget that RISES, or an identity that moved by hand.

    Each phase's number is a cost ceiling, so its ratchet only ever
    moves down: an upward move is the hand-raise the never-rules forbid,
    a downward one is the tighten. The workload identity
    (``tests_tree_lines``, issue #524) is the seed's binding to the
    workload it measured: only the sanctioned re-seed writes it, and
    that path deletes the family first -- the marker's commit is the
    one whose base lacks the family, so the identity always arrives as
    part of a new family's seed. On an established family, an added or
    changed identity is a hand-edit and fails; a family the HEAD omits
    is the sanctioned delete and leaves with the family wholesale. A
    family the base predates carries no record, so the change
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
            if after[phase][field] > before[phase][field]:
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


def _reparse_moves(base, head, established):
    """A reparse phase that RISES.

    Each phase is a cost ceiling, so its ratchet only ever moves down: an
    upward move is the hand-raise the never-rules forbid and a downward
    one is the tighten. A family the base predates carries no record, so
    the change introducing one is its seed and not a move at all.
    """
    if thresholds.REPARSE_FAMILY not in established:
        return []
    moves = []
    before = base[thresholds.REPARSE_FAMILY]
    after = head.get(thresholds.REPARSE_FAMILY)
    if not after:
        # The head carries no reparse record — the sanctioned delete,
        # which the loader admits only inside the family's own marker's
        # window (reseed.py); the family arrives whole in the seed
        # commit. Nothing to compare against a stand-in's numbers.
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
        before = base[member]
        after = head[member]
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


def forbidden_moves(base, head, established):
    """Return the data's forbidden moves from base to head, labelled."""
    return (_coverage_moves(base, head)
            + _reparse_moves(base, head, established)
            + _suite_cost_moves(base, head, established)
            + _baseline_moves(base, head, established))


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, required=True,
                        help='the base document (JSON file)')
    parser.add_argument('--head', type=Path, default=thresholds.THRESHOLDS,
                        help='the head document (JSON file)')
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        head = thresholds.load(args.head)
        base, established = _load_base(args.base)
        moves = forbidden_moves(base, head, established)
        if not moves:
            print('no forbidden threshold moves')
            return 0
        for move in moves:
            print(f'forbidden move: {move}', file=sys.stderr)
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
