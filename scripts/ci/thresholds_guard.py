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
one-time seed of a family the base predates. The truth of seeded values
is pinned separately, by the committed-document-matches-tree tests, which
run on the same merge ref.

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
    'A reparse CPU floor or measured value is never raised by hand: the '
    'ratchet only tightens it downward on master when a faster run '
    'justifies it, so buy headroom by making the reparse cheaper.')
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
    phase: {'measured': Decimal('2.5'), 'floor': Decimal('4.0')}
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
    """Load a base document, tolerating members it predates.

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
        data[thresholds.REPARSE_FAMILY] = {
            phase: dict(record) for phase, record in _ABSENT_REPARSE.items()}
    if thresholds.SUITE_COST_FAMILY in data:
        established.add(thresholds.SUITE_COST_FAMILY)
    else:
        data.setdefault(thresholds.SUITE_COST_FAMILY, _ABSENT_SUITE_COST)
    return thresholds.normalise(data), established


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
    """A suite cost budget that RISES.

    Each phase's number is a cost ceiling, so its ratchet only ever
    moves down: an upward move is the hand-raise the never-rules forbid,
    a downward one is the tighten. A family the base predates carries no
    record, so the change introducing one is its seed and not a move at
    all.
    """
    if thresholds.SUITE_COST_FAMILY not in established:
        return []
    moves = []
    before = base[thresholds.SUITE_COST_FAMILY]
    after = head[thresholds.SUITE_COST_FAMILY]
    for phase in thresholds.SUITE_COST_PHASES:
        for field in ('measured', 'floor'):
            if after[phase][field] > before[phase][field]:
                moves.append(
                    f'{thresholds.SUITE_COST_FAMILY}.{phase}.{field}: '
                    f'{before[phase][field]} -> {after[phase][field]}')
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
    after = head[thresholds.REPARSE_FAMILY]
    for phase in thresholds.REPARSE_PHASES:
        for field in ('measured', 'floor'):
            if after[phase][field] > before[phase][field]:
                moves.append(
                    f'{thresholds.REPARSE_FAMILY}.{phase}.{field}: '
                    f'{before[phase][field]} -> {after[phase][field]}')
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
