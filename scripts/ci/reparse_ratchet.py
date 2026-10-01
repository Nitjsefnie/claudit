#!/usr/bin/env python3
"""Tighten the recorded reparse CPU budgets from a measured bench run.

The mirror of ``ratchet.py`` with the direction inverted. Coverage is a
quality number, so its ratchet only ever raises; a reparse phase's share
of the pass is a cost, so this one only ever TIGHTENS, one phase at a
time: a phase whose measured share beats the recorded ``measured`` by
more than the hysteresis has both its fields rewritten downward, and
anything slower — or within the hysteresis — justifies no change at all
and leaves the file untouched. The yardsticks are the same 1.5 constants
the coverage ratchet uses, in the bench's own unit (percent of the
pass's CPU).

The gap sits ABOVE the measured value because a phase's ``floor`` is a
ceiling a run's share must stay under, not a lower bound a run's quality
must clear; writing it the coverage way round would put the ceiling
below the measurement that recorded it, and every later run at that
measurement would fail a gate no change could satisfy.

Phases are independent ceilings, so a run that tightens one of them
leaves the others exactly where they were: a share only means something
against the total it was measured in, and the totals are the whole run
every time.

A separate file from ``ratchet.py`` on purpose: the two move opposite
ways, and one module with two directions would make every reader ask
which way a given family goes. This one imports nothing but the loader,
so the data operation stays free of the parse path the bench measures.

  python3 scripts/ci/reparse_ratchet.py --measured-file reparse.json
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
    reversed. The two metrics of one phase are independent: a cheaper
    share says nothing about the count.
    """
    candidate = thresholds.normalise(data)
    moves = {}
    for phase in thresholds.REPARSE_PHASES:
        if phase not in readings:
            raise ValueError(f'the measurement carries no {phase} phase')
        for metric in thresholds.REPARSE_METRICS:
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
    candidate = thresholds.normalise(data)
    for _label, (phase, metric, measured) in moves.items():
        candidate[thresholds.REPARSE_FAMILY][phase][metric] = {
            'measured': measured,
            'floor': measured + CALIBRATION_GAP,
        }
    return thresholds.normalise(candidate)


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--measured-file', required=True, type=Path,
                        help='the bench measurement (reparse_bench --write)')
    parser.add_argument(
        '--thresholds', type=Path, default=thresholds.THRESHOLDS)
    return parser


def _readings(path: Path) -> dict:
    """The measurement file's per-phase, per-metric readings.

    A phase with no count (an interpreter without sys.monitoring) is left
    out rather than read as zero: a zero would tighten a floor that no
    run can justify.
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


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        data = thresholds.load(args.thresholds)
        readings = _readings(args.measured_file)
        recorded = read_calibration(data)
        candidate = update(data, readings)
        if candidate is None:
            print('no reparse phase beat its recorded budget by more than '
                  f'{TIGHTEN_HYSTERESIS}: nothing to tighten')
            return 0
        for phase in thresholds.REPARSE_PHASES:
            for metric in thresholds.REPARSE_METRICS:
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
