#!/usr/bin/env python3
"""Read, validate, and atomically publish CI threshold state."""
from __future__ import annotations

import argparse
import importlib
import json
import os
import stat
import sys
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Callable

# scripts/ci holds standalone CI entry points, not an importable
# package, so a run from the scripts directory finds its siblings by
# name; a caller that loaded this module BY PATH (the tests do) has not
# put that directory on sys.path, and appending it here is what makes
# the same import work from both.
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.append(str(Path(__file__).resolve().parent))
# pylint: disable-next=import-outside-toplevel
validate = importlib.import_module('thresholds_validate')

# The moved validators, re-bound here so every existing caller and test
# keeps spelling them as they always have (thresholds.coverage_value and
# so on): the split moved WHERE a value is judged, not its name. The
# annotations are load-bearing: the sibling is reached by importlib,
# which is untyped, and a bare alias would leave every value here an
# unconstrained Any.
_Number = Callable[[object, str], Decimal]
_Path = Callable[[str], str]
_number: _Number = validate.number
_module_path: _Path = validate.module_path
coverage_value: _Number = validate.coverage_value

THRESHOLDS = (Path(__file__).resolve().parents[2]
              / '.github' / 'ci-thresholds.json')
# The fixed gap between a recorded measured value and its floor, and the
# hysteresis a new measurement must clear before the floor moves: both
# yardstick values, not tunables (SV-CI-RATCHETS).
CALIBRATION_GAP = Decimal('1.5')
_SCHEMA_VERSION = 1
# Every coverage language the ratchet gates: python from the pytest run
# over backend/, javascript from the node-executing tests over the src
# files they load (src/**/*.js; node parses no JSX).
COVERAGE_LANGUAGES = ('python', 'javascript')
# Every only-shrinks baseline member: recorded numbers never rise and
# entries are never hand-added (SV-CI-RATCHETS); the direction guard
# (thresholds_guard.py) enforces the rule against the base document.
BASELINE_MEMBERS = ('module_size_baseline', 'pylint_suppression_baseline', 'comment_share_baseline')
_TOP_LEVEL_FIELDS = ('schema_version', 'coverage', *BASELINE_MEMBERS)
_COVERAGE_FIELDS = ('measured', 'floor')
_FIELD_LABELS = {
    'thresholds': 'field: {field}',
    'coverage': 'coverage language: {field}',
}


def _required_fields(value: object, expected: tuple[str, ...], name: str) -> None:
    """The moved field check, with this module's family labels."""
    return validate.required_fields(
        value, expected, name, _FIELD_LABELS)


def normalise(data: dict) -> dict:
    """The document in canonical form, or a refusal naming the offender."""
    _required_fields(data, _TOP_LEVEL_FIELDS, 'thresholds')
    schema = _number(data['schema_version'], 'schema_version')
    if schema != _SCHEMA_VERSION or schema != schema.to_integral_value():
        raise ValueError(
            f'unsupported schema_version: {data["schema_version"]}')

    coverage_data = data['coverage']
    _required_fields(coverage_data, COVERAGE_LANGUAGES, 'coverage')
    normalised_coverage = {}
    for language in COVERAGE_LANGUAGES:
        record = coverage_data[language]
        prefix = f'coverage.{language}'
        _required_fields(record, _COVERAGE_FIELDS, prefix)
        measured = coverage_value(
            record['measured'], f'{prefix}.measured')
        floor = coverage_value(record['floor'], f'{prefix}.floor')
        if floor >= measured:
            raise ValueError(f'{prefix}.floor must be below measured')
        if measured - floor != CALIBRATION_GAP:
            raise ValueError(f'{prefix} calibration gap must be 1.5')
        normalised_coverage[language] = {
            'measured': measured,
            'floor': floor,
        }

    normalised = {
        'schema_version': _SCHEMA_VERSION,
        'coverage': normalised_coverage,
    }
    for member in BASELINE_MEMBERS:
        normalised[member] = _baseline(data[member], member)
    return normalised


def _baseline(baseline: dict[str, object], member: str) -> dict:
    if not isinstance(baseline, dict):
        raise ValueError(f'{member} must be an object')
    normalised = {}
    for path, value in baseline.items():
        safe_path = _module_path(path)
        if member == 'comment_share_baseline':
            normalised[safe_path] = validate.comment_share_value(value, f'{member}.{safe_path}')
            continue
        if isinstance(value, int) and value.__class__ is int and value > 0:
            normalised[safe_path] = value
            continue
        count = _number(value, f'{member}.{safe_path}')
        if count <= 0 or count != count.to_integral_value():
            raise ValueError(
                f'{member}.{safe_path} must be a positive integer')
        normalised[safe_path] = int(count)
    return dict(sorted(normalised.items()))


def load(path: str | Path = THRESHOLDS) -> dict:
    """Read and validate a thresholds document."""
    try:
        raw = Path(path).read_bytes()
    except OSError as error:
        raise ValueError(f'cannot read thresholds: {error}') from None
    return importlib.import_module('thresholds_decode').normalise_bytes(raw, normalise)


def coverage(data, language):
    if language not in COVERAGE_LANGUAGES:
        raise ValueError(f'unknown coverage language: {language}')
    normalised = normalise(data)
    record = normalised['coverage'][language]
    return record['measured'], record['floor']


def module_size_baseline(data):
    normalised = normalise(data)
    return dict(normalised['module_size_baseline'])


def comment_share_baseline(data):
    return dict(normalise(data)['comment_share_baseline'])


def _render(data: dict) -> bytes:
    normalised = normalise(data)
    text = json.dumps(normalised, ensure_ascii=True, indent=2, sort_keys=True,
                      allow_nan=False, default=float) + '\n'
    encoded = text.encode('utf-8')
    if normalise(validate.decode(encoded)) != normalised:
        raise ValueError('serialized thresholds failed validation')
    return encoded


def _remove_temp(path):
    try:
        Path(path).unlink()
    except OSError:
        # Cleanup must not hide the publication failure that prompted it.
        pass


def write(path, data):
    """Validate and atomically replace ``path`` with canonical JSON bytes."""
    target = Path(path)
    payload = _render(data)
    mode = None
    try:
        mode = stat.S_IMODE(target.stat().st_mode)
    except FileNotFoundError:
        # A new destination keeps mkstemp's restrictive default mode.
        pass
    parent = target.parent
    fd, temporary = tempfile.mkstemp(
        prefix=f'.{target.name}.', suffix='.tmp', dir=str(parent))
    temporary_path = Path(temporary)
    open_fd = fd
    replaced = False
    try:
        with os.fdopen(fd, 'wb') as handle:
            open_fd = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temporary_path, mode)
        os.replace(temporary_path, target)
        replaced = True
    finally:
        if open_fd is not None:
            try:
                os.close(open_fd)
            except OSError:
                # Preserve the primary failure when redundant close fails.
                pass
        if not replaced:
            _remove_temp(temporary_path)


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--check', action='store_true',
                       help='validate the threshold document')
    modes.add_argument('--coverage-floor', choices=COVERAGE_LANGUAGES,
                       help='print one language floor')
    modes.add_argument('--coverage-measured', choices=COVERAGE_LANGUAGES,
                       help='print one language measured value')
    parser.add_argument('--thresholds', type=Path, default=THRESHOLDS)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        data = load(args.thresholds)
        if args.coverage_floor:
            _measured, floor = coverage(data, args.coverage_floor)
            print(f'{floor:.1f}')
        elif args.coverage_measured:
            measured, _floor = coverage(data, args.coverage_measured)
            print(f'{measured:.1f}')
        else:
            print('thresholds valid')
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
