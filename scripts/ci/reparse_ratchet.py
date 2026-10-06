#!/usr/bin/env python3
"""Tighten the recorded reparse CPU budgets from a measured bench run.

The mirror of ``ratchet.py`` with the direction inverted. Coverage is a
quality number, so its ratchet only ever raises; a reparse phase's
recorded bytecodes are a cost, so this one only ever TIGHTENS, one phase
at a time: a phase whose measured count beats the recorded ``measured``
by more than the hysteresis has both its fields rewritten downward, and
anything slower — or within the hysteresis — justifies no change at all
and leaves the file untouched. The yardsticks are the same 1.5 constants
the coverage ratchet uses, in the bench's own unit (hundreds of
bytecodes per file).

Only the GATED instruments are tightened (issue #513), which is the
bytecode count alone. The per-phase share is telemetry — measured and
recorded, compared against by nothing — and its runner-to-runner spread
is wider than the gap a tighten would move, so chasing it would spend a
master's commit on the machine's distribution of CPU rather than on the
parse.

The gap sits ABOVE the measured value because a phase's ``floor`` is a
ceiling a run's count must stay under, not a lower bound a run's quality
must clear; writing it the coverage way round would put the ceiling
below the measurement that recorded it, and every later run at that
measurement would fail a gate no change could satisfy.

Phases are independent ceilings, so a run that tightens one of them
leaves the others exactly where they were.

SEEDING is the landing half of the doctrine's re-seed (issue #703):
``--seed MEASUREMENT`` writes the committed family from a runner
measurement's per-phase readings, both metrics, only onto a document
the family is ABSENT from — the shape the marker's delete committed —
and refuses over a recorded family, which is the hand-raise's doorway.
The seed is the second reviewed gate-definer; a counts-less measurement
is refused rather than seeded, because an unmeasured instrument must
never enter the document as a number.

A separate file from ``ratchet.py`` on purpose: the two move opposite
ways, and one module with two directions would make every reader ask
which way a given family goes. This one imports nothing but the loader,
so the data operation stays free of the parse path the bench measures.

  python3 scripts/ci/reparse_ratchet.py --measured-file reparse.json
  python3 scripts/ci/reparse_ratchet.py --seed reparse.json
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
    from . import thresholds
else:
    thresholds = importlib.import_module('thresholds')


CALIBRATION_GAP = Decimal('1.5')
TIGHTEN_HYSTERESIS = Decimal('1.5')


_VALIDATE = {'share': thresholds.share_value,
             'bytecodes': thresholds.count_value}


def measurement(value, label):
    if isinstance(value, bool):
        raise ValueError(f'{label}: measured must be a finite JSON number')
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(
            f'{label}: measured must be a finite JSON number') from None
    if not result.is_finite():
        raise ValueError(f'{label}: measured must be finite')
    return result


def floor_for(measured, metric='share'):
    """The ceiling a phase's cost may be exceeded by: the gap ABOVE it,
    because this floor is an upper bound on a cost, not a lower bound on
    a quality."""
    return _VALIDATE[metric](measured, metric) + CALIBRATION_GAP


def read_calibration(data):
    return thresholds.reparse(data)


def tightenable(data, readings):
    """Every phase-metric whose measurement justifies a tighten.

    ``readings`` is ``{phase: {metric: value}}``. A pair tightens when it
    beats its recorded value by more than the hysteresis — the same rule
    the coverage ratchet applies to a raise, with every comparison
    reversed. Only the GATED metrics are read (issue #513): the share
    rides along in every measurement, and nothing here moves it. A tip
    inside the reparse re-seed window carries no budget to tighten
    against: tightening nothing is the whole answer, and the CLI says
    so.
    """
    candidate = thresholds.normalise(data, thresholds.verdict())
    if thresholds.REPARSE_FAMILY not in candidate:
        return {}
    moves = {}
    for phase in thresholds.REPARSE_PHASES:
        if phase not in readings:
            raise ValueError(f'the measurement carries no {phase} phase')
        for metric in thresholds.REPARSE_GATED_METRICS:
            if metric not in readings[phase]:
                raise ValueError(
                    f'the measurement carries no {metric} for {phase}')
            label = f'{phase}.{metric}'
            recorded = (candidate[thresholds.REPARSE_FAMILY][phase]
                        [metric]['measured'])
            measured = _VALIDATE[metric](
                measurement(readings[phase][metric], label), label)
            if recorded - measured > TIGHTEN_HYSTERESIS:
                moves[label] = (phase, metric, measured)
    return moves


def update(data, readings):
    """Return an updated document, or ``None`` when no tighten is due."""
    moves = tightenable(data, readings)
    if not moves:
        return None
    candidate = thresholds.normalise(data, thresholds.verdict())
    for _label, (phase, metric, measured) in moves.items():
        candidate[thresholds.REPARSE_FAMILY][phase][metric] = {
            'measured': measured,
            'floor': measured + CALIBRATION_GAP,
        }
    return thresholds.normalise(candidate, thresholds.verdict())


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--measured-file', type=Path,
                       help='the bench measurement (reparse_bench --write); '
                            'tighten the phases it beats, never raises')
    modes.add_argument('--seed', type=Path, metavar='MEASUREMENT',
                       help='write the committed reparse family from this '
                            'runner measurement (refuses when the family '
                            'is already recorded)')
    parser.add_argument(
        '--thresholds', type=Path, default=thresholds.THRESHOLDS)
    return parser


def _readings(path: Path) -> dict:
    """The measurement file's per-phase, per-metric readings.

    The share is carried because every measurement records it, not
    because anything here acts on it. A phase with no count (an
    interpreter without sys.monitoring) is left out rather than read as
    zero: a zero would tighten a floor that no run can justify.
    """
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        readings = {}
        for phase, record in data['phases'].items():
            entry = {'share': record['share']}
            counted = record.get('bytecode_hundreds_per_file')
            if counted is not None:
                entry['bytecodes'] = counted
            readings[phase] = entry
        return readings
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError(
            f'cannot read the bench measurement {path}: {error}') from None


def _seed_readings(path: Path) -> dict:
    """The measurement's full per-phase readings, counts included.

    The seed restores the WHOLE family — the gated count and the share
    it was recorded beside — so a counts-less phase is refused, never
    seeded as a zero or an absence. The tighten mode tolerates an
    uncounted phase because moving a recorded number is not what an
    uncounted phase can justify; a seed would be FABRICATING one.
    """
    readings = _readings(path)
    for phase in thresholds.REPARSE_PHASES:
        if readings.get(phase, {}).get('bytecodes') is None:
            raise ValueError(
                f'{path}: the {phase} phase carries no bytecode count '
                '(the process_time fallback is not gateable or seedable)')
    return readings


def load_for_seed(path):
    """The raw committed bytes a seed may target.

    A document carrying the family is loaded through the loader's
    strict validation, and seed() refuses it; one the family is absent
    from — the shape the marker's delete committed — is parsed leniently
    (trusted committed bytes) and handed to seed() with the member
    gone, the one state seed() accepts.
    """
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if data.get(thresholds.REPARSE_FAMILY):
        return thresholds.load(path), False
    return data, True


def seed(data, readings):
    """Return a document with the reparse family seeded, or raise when
    it is already recorded.

    ``data`` is the RAW document as its bytes carry it: the seed's
    target is exactly the document the family is ABSENT from, which the
    loader accepts while the delete's marker sits inside the bounded
    walk's span (reseed.py) — so the seed commit need not carry the
    marker itself. Seeding over a recorded family is refused: the
    sanctioned way to replace recorded budgets is the doctrine's
    re-seed — delete the stale member under the marker first, commit
    the runner-measured counts second, both reviewed gate-definers.
    ``readings`` is ``{phase: {metric: value}}`` from a runner
    measurement (``reparse_bench.py --write``); both metrics are
    written per phase, the share as the reading it was recorded beside.
    """
    if data.get(thresholds.REPARSE_FAMILY):
        # Imported HERE, the way the loader's verdict does it: the
        # refusal is the one place this module names the marker, and
        # the git-reading module stays off the tighten path's imports.
        # pylint: disable-next=import-outside-toplevel
        reseed = importlib.import_module('reseed')
        raise ValueError(
            f'{thresholds.REPARSE_FAMILY} is already recorded: seeding '
            'would overwrite recorded budgets. Delete the stale member '
            f'under the {reseed.REPARSE_MARKER} marker first if the '
            'workload legitimately changed.')
    candidate = dict(data)
    family = {}
    for phase in thresholds.REPARSE_PHASES:
        family[phase] = {}
        for metric in thresholds.REPARSE_METRICS:
            label = f'{phase}.{metric}'
            measured = _VALIDATE[metric](
                measurement(readings[phase][metric], label), label)
            family[phase][metric] = {
                'measured': measured,
                'floor': measured + CALIBRATION_GAP,
            }
    candidate[thresholds.REPARSE_FAMILY] = family
    # Strict, by fact and not by default: the candidate this builds
    # always carries the family, so there is nothing for the marker to
    # excuse. The re-seed's tolerance is the loader's, not the seed's.
    return thresholds.normalise(candidate, False)


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        if args.seed is not None:
            data, _seeded_fresh = load_for_seed(args.thresholds)
            readings = _seed_readings(args.seed)
            candidate = seed(data, readings)
            thresholds.write(args.thresholds, candidate)
            for phase in thresholds.REPARSE_PHASES:
                record = candidate[thresholds.REPARSE_FAMILY][phase]
                print(f'seeded the {phase} budgets '
                      f'{record["share"]["floor"]} {thresholds.REPARSE_UNIT} '
                      f'/ {record["bytecodes"]["floor"]} '
                      f'{thresholds.REPARSE_COUNT_UNIT} '
                      f'(measured {record["share"]["measured"]} / '
                      f'{record["bytecodes"]["measured"]})')
            return 0
        data = thresholds.load(args.thresholds)
        recorded = read_calibration(data)
        if not recorded:
            # The re-seed window: no budget exists to tighten. Saying so
            # beats a crash on an absent family the walk found the
            # family's own marker for.
            print(f'no {thresholds.REPARSE_FAMILY} budget — re-seed in '
                  'flight: nothing to tighten')
            return 0
        readings = _readings(args.measured_file)
        candidate = update(data, readings)
        if candidate is None:
            print('no reparse phase beat its recorded budget by more than '
                  f'{TIGHTEN_HYSTERESIS}: nothing to tighten')
            return 0
        for phase in thresholds.REPARSE_PHASES:
            for metric in thresholds.REPARSE_GATED_METRICS:
                before = recorded[phase][metric]
                after = candidate[thresholds.REPARSE_FAMILY][phase][metric]
                if after != before:
                    unit = (thresholds.REPARSE_COUNT_UNIT if metric
                            == 'bytecodes' else thresholds.REPARSE_UNIT)
                    print(f'tightened the {phase} {metric} budget '
                          f'{before["floor"]} -> {after["floor"]} {unit} '
                          f'(measured {after["measured"]})')
        thresholds.write(args.thresholds, candidate)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
