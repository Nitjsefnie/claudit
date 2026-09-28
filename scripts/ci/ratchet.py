#!/usr/bin/env python3
"""Raise the recorded coverage calibration from a measured run.

The recorded ``measured`` value only ever moves up, and only when a new
measurement beats it by more than the hysteresis (both the fixed gap and
the hysteresis are yardstick constants of 1.5 points, shared with
scripts/ci/thresholds.py): a raise rewrites the document so
``floor = measured - fixed gap``. A lower measurement — or one within the
hysteresis — justifies no raise, and the document stays untouched.
"""
from __future__ import annotations

import argparse
import importlib
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

if __package__:
    # pylint: disable-next=relative-beyond-top-level,no-name-in-module
    from . import thresholds
else:
    thresholds = importlib.import_module('thresholds')


CALIBRATION_GAP = Decimal('1.5')
RAISE_HYSTERESIS = Decimal('1.5')
_LABELS = {'python': 'Python', 'javascript': 'JavaScript'}


def _measurement(value):
    if isinstance(value, bool):
        raise ValueError('measured must be a finite JSON number')
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError('measured must be a finite JSON number') from None
    if not result.is_finite():
        raise ValueError('measured must be finite')
    return thresholds.coverage_value(result, 'measured')


def floor_for(measured):
    return _measurement(measured) - CALIBRATION_GAP


def read_calibration(data, language='python'):
    return thresholds.coverage(data, language)


def update(data, measured, language='python'):
    """Return an updated document, or ``None`` when no raise is justified."""
    candidate = thresholds.normalise(data)
    measured = _measurement(measured)
    recorded_measured, _floor = read_calibration(candidate, language)
    should_raise = measured - recorded_measured > RAISE_HYSTERESIS
    if not should_raise:
        return None
    candidate['coverage'][language] = {
        'measured': measured,
        'floor': measured - CALIBRATION_GAP,
    }
    return thresholds.normalise(candidate)


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--measured', required=True,
                        help='coverage measured by this run, e.g. 92.6')
    parser.add_argument('--language', choices=thresholds.COVERAGE_LANGUAGES,
                        default='python',
                        help='which language calibration to raise')
    parser.add_argument(
        '--thresholds', type=Path, default=thresholds.THRESHOLDS)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    label = _LABELS.get(args.language, args.language)
    try:
        data = thresholds.load(args.thresholds)
        measured = _measurement(args.measured)
        floor = read_calibration(data, args.language)[1]
        candidate = update(data, measured, language=args.language)
        if candidate is None:
            print(f'{label} coverage {measured:.1f}% justifies no raise '
                  f'above the {floor:.1f} floor')
            return 0
        thresholds.write(args.thresholds, candidate)
        new_floor = candidate['coverage'][args.language]['floor']
        print(f'raised the {label} coverage floor {floor:.1f} -> '
              f'{new_floor:.1f} (measured {measured:.1f}%)')
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
