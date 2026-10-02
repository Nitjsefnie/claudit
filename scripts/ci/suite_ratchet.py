#!/usr/bin/env python3
"""Seed and tighten the recorded suite cost budgets.

The mirror of ``ratchet.py`` with the direction inverted, next to the
size and suppression tighten passes. A suite phase's number is a COST:
its ratchet only ever TIGHTENS -- a phase whose measured count beats the
recorded value by more than the hysteresis has both its fields rewritten
downward, and anything slower -- or within the hysteresis -- justifies
no change at all and leaves the file untouched. The yardsticks are the
same 1.5 constants the coverage ratchet uses, in the bench's own unit
(millions of instructions).

The gap sits ABOVE the measured value because a phase's ``floor`` is a
ceiling a run must stay under, not a lower bound a run's quality must
clear; writing it the coverage way round would put the ceiling below the
measurement that recorded it, and every later run at that measurement
would fail a gate no change could satisfy.

The gap's SIZE deserves its own sentence, because the unit is exact: a
count is a count, so the 1.5-unit (1.5M-instruction) gap prices
INTERPRETER AND LIBRARY DRIFT, not measurement noise -- there is none.
A 3.13.x micro release, or a pinned pytest patch release, can move a
hot path's bytecode count by more than the gap; the interpreter is
pinned to the exact micro version in the workflows that run the bench,
and a legitimately moved workload (fixture list or interpreter pin) is
the doctrine's sanctioned re-seed path: two reviewed gate-definer
changes — the first deletes the stale member under the
``[suite-cost-re-seed]`` marker, the second seeds the new counts
through the loader's writer from a RUNNER measurement. One change
cannot do both, because the guard refuses an upward move the base
already carries.

A separate file from ``ratchet.py`` on purpose: the two move opposite
ways. It imports nothing but the loader, so the data operation stays
free of the suite the bench measures.

  python3 scripts/ci/suite_ratchet.py --seed m.json
  python3 scripts/ci/suite_ratchet.py --tighten m.json
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

if __package__:
    # pylint: disable-next=relative-beyond-top-level,no-name-in-module
    from . import reseed, thresholds
else:
    thresholds = importlib.import_module('thresholds')
    reseed = importlib.import_module('reseed')

CALIBRATION_GAP = Decimal('1.5')
TIGHTEN_HYSTERESIS = Decimal('1.5')
UNIT = thresholds.SUITE_COST_UNIT
PHASES = thresholds.SUITE_COST_PHASES
_family = thresholds.SUITE_COST_FAMILY
_COUNTS = 'million_instructions'


def _phase_counts(path: Path) -> dict:
    """The measurement file's per-phase counts, as one-place Decimals.

    A counts-less measurement (the process_time fallback) is refused:
    a budget operation has nothing to seed or tighten FROM, and an
    absent measurement must never read as a cheap one.
    """
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        phases = data['phases']
        counts = {}
        for phase in PHASES:
            spelling = phases[phase][_COUNTS]
            if not isinstance(spelling, str):
                raise ValueError(
                    f'{path}: the {phase} phase carries no '
                    f'{_COUNTS} (the process_time fallback is not '
                    'gateable or seedable)')
            try:
                counts[phase] = Decimal(spelling)
            except InvalidOperation:
                raise ValueError(
                    f'{path}: {phase}.{_COUNTS} is not a number'
                ) from None
        return counts
    except (OSError, KeyError, TypeError,
            json.JSONDecodeError) as error:
        raise ValueError(
            f'cannot read the bench measurement {path}: {error}') from None


def seed(data, counts):
    """Return a document with the family seeded, or raise when present.

    ``data`` is the RAW document as its bytes carry it: the seed's
    target is exactly the document the member is ABSENT from, which the
    loader only reads on the re-seed marker commit that deleted it
    (reseed.py). Seeding over a recorded family is refused: overwriting
    would need its own justification, and the sanctioned one is the
    doctrine's re-seed -- delete the stale member first, commit the new
    counts second, both reviewed gate-definers -- not a quiet rewrite by
    a bot.
    """
    if data.get(_family):
        raise ValueError(
            f'{_family} is already recorded: seeding would overwrite '
            'recorded budgets. Delete the stale member under the '
            f'{reseed.MARKER} marker first if the workload '
            'legitimately changed.')
    candidate = dict(data)
    candidate[_family] = {
        phase: {
            'measured': thresholds.instruction_value(
                counts[phase], f'{_family}.{phase}.measured'),
            'floor': thresholds.instruction_value(
                counts[phase], f'{_family}.{phase}.measured')
            + CALIBRATION_GAP,
        }
        for phase in PHASES
    }
    # Strict, by fact and not by default: the candidate this builds
    # always carries the family, so there is nothing for the marker to
    # excuse. The re-seed's tolerance is the loader's, not the seed's.
    return thresholds.normalise(candidate, False)


def load_for_seed(path):
    """The raw committed bytes a seed may target.

    A document carrying the family is loaded through the loader's
    strict validation; one the family predates is parsed leniently --
    trusted committed bytes -- and handed to seed() with the member
    absent, which is the one state seed() accepts.
    """
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if data.get(_family):
        return thresholds.load(path), False
    return data, True


def tightenable(data, counts):
    """Every phase whose measurement justifies a tighten.

    A phase tightens when it beats its recorded value by more than the
    hysteresis -- the same rule the coverage ratchet applies to a raise,
    with every comparison reversed. Phases are independent ceilings: a
    run that tightens one leaves the others exactly where they were.
    """
    candidate = thresholds.normalise(data, thresholds.verdict())
    moves = {}
    for phase in PHASES:
        if phase not in counts:
            raise ValueError(f'the measurement carries no {phase} phase')
        recorded = candidate[_family][phase]['measured']
        measured = thresholds.instruction_value(
            counts[phase], f'{_family}.{phase}.measured')
        if recorded - measured > TIGHTEN_HYSTERESIS:
            moves[phase] = measured
    return moves


def update(data, counts):
    """Return an updated document, or ``None`` when no tighten is due."""
    moves = tightenable(data, counts)
    if not moves:
        return None
    candidate = thresholds.normalise(data, thresholds.verdict())
    for phase, measured in moves.items():
        candidate[_family][phase] = {
            'measured': measured,
            'floor': measured + CALIBRATION_GAP,
        }
    return thresholds.normalise(candidate, thresholds.verdict())


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--seed', type=Path, metavar='MEASUREMENT',
                       help='write the committed seed for the family '
                            'from this measurement (refuses when the '
                            'family is already recorded)')
    modes.add_argument('--tighten', type=Path, metavar='MEASUREMENT',
                       help='lower the budgets of phases this '
                            'measurement beats by more than the '
                            'hysteresis; never raises')
    parser.add_argument('--thresholds', type=Path,
                        default=thresholds.THRESHOLDS)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        counts = _phase_counts(args.seed or args.tighten)
        if args.seed is not None:
            data, _seeded_fresh = load_for_seed(args.thresholds)
            candidate = seed(data, counts)
            thresholds.write(args.thresholds, candidate)
            for phase in PHASES:
                print(f'seeded {phase} '
                      f'{candidate[_family][phase]["measured"]} {UNIT} '
                      f'(ceiling {candidate[_family][phase]["floor"]})')
            return 0
        data = thresholds.load(args.thresholds)
        if not data.get(_family):
            # The sanctioned re-seed's intermediate commit carries no
            # budget to tighten, by declaration. Tightening nothing is
            # the whole answer, and saying so beats a crash on an
            # absent family the marker made legal.
            print('no suite_cost budget — re-seed in flight: '
                  'nothing to tighten')
            return 0
        candidate = update(data, counts)
        if candidate is None:
            print('no suite phase beat its recorded budget by more than '
                  f'{TIGHTEN_HYSTERESIS}: nothing to tighten')
            return 0
        for phase in PHASES:
            before = data[_family][phase]
            after = candidate[_family][phase]
            if after != before:
                print(f'tightened {phase} '
                      f'{before["floor"]} -> {after["floor"]} {UNIT} '
                      f'(measured {after["measured"]})')
        thresholds.write(args.thresholds, candidate)
        return 0
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
