#!/usr/bin/env python3
"""Read, validate, and atomically publish CI threshold state."""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

THRESHOLDS = (Path(__file__).resolve().parents[2]
              / '.github' / 'ci-thresholds.json')
# The fixed gap between a recorded measured value and its floor, and the
# hysteresis a new measurement must clear before the floor moves: both
# yardstick values, not tunables (SV-CI-RATCHETS).
CALIBRATION_GAP = Decimal('1.5')
_SCHEMA_VERSION = 1
_COVERAGE_LANGUAGES = ('python',)
_BASELINE_FIELDS = ('module_size_baseline',)
_TOP_LEVEL_FIELDS = ('schema_version', 'coverage', *_BASELINE_FIELDS)
_COVERAGE_FIELDS = ('measured', 'floor')
_FIELD_LABELS = {
    'thresholds': 'field: {field}',
    'coverage': 'coverage language: {field}',
}
_INVALID_PATH_CHARS = set('<>:"|?*')
_DEVICE_NAMES = {
    'CON', 'PRN', 'AUX', 'NUL',
    *(f'COM{number}' for number in range(1, 10)),
    *(f'LPT{number}' for number in range(1, 10)),
}


def _reject_constant(value):
    raise ValueError(f'non-finite JSON number: {value}')


def _object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate JSON key: {key}')
        result[key] = value
    return result


def _decode(raw):
    try:
        text = raw.decode('utf-8') if isinstance(raw, bytes) else raw
        return json.loads(
            text, parse_float=Decimal, parse_int=Decimal,
            parse_constant=_reject_constant, object_pairs_hook=_object_pairs)
    except UnicodeDecodeError as error:
        raise ValueError(f'invalid thresholds JSON: {error}') from None
    except json.JSONDecodeError as error:
        raise ValueError(f'invalid thresholds JSON: {error}') from None


def _number(value, name):
    if isinstance(value, bool) or not isinstance(
            value, (int, float, Decimal)):
        raise ValueError(f'{name} must be a JSON number')
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f'{name} must be a JSON number') from None
    if not result.is_finite():
        raise ValueError(f'{name} must be finite')
    return result


def _required_fields(value, expected, name):
    if not isinstance(value, dict):
        raise ValueError(f'{name} must be an object')
    label = _FIELD_LABELS.get(name, f'field: {name}.{{field}}')
    for field in expected:
        if field not in value:
            raise ValueError(f'missing {label.format(field=field)}')
    expected_set = set(expected)
    for field in value:
        if field not in expected_set:
            raise ValueError(f'unknown {label.format(field=field)}')


def coverage_value(value, name):
    result = _number(value, name)
    if result < 0 or result > 100:
        raise ValueError(f'{name} must be between 0.0 and 100.0')
    exponent = result.as_tuple().exponent
    # The canonical spelling of a coverage number carries exactly one
    # decimal place (92.0, never 92 or 92.00): it is what the ratchet
    # writes and what coverage --precision=1 measures.
    if not isinstance(exponent, int) or exponent != -1:
        raise ValueError(f'{name} must have exactly one decimal place')
    return result


def _path_component_safe(component):
    safe = bool(component) and component not in ('.', '..')
    safe = safe and component.rstrip(' .') == component
    safe = safe and component.upper().split('.', 1)[0] not in _DEVICE_NAMES
    safe = safe and not any(char in _INVALID_PATH_CHARS
                            for char in component)
    safe = safe and not any(
        ord(char) < 32 or 127 <= ord(char) <= 159
        or 0xD800 <= ord(char) <= 0xDFFF for char in component)
    if not safe:
        return False
    try:
        return len(component.encode('utf-8')) <= 240
    except UnicodeEncodeError:
        return False


def _module_path(value):
    if not isinstance(value, str) or not value or '\\' in value:
        raise ValueError(f'unsafe module path: {value!r}')
    if value.startswith('/') or value.startswith('//'):
        raise ValueError(f'unsafe module path: {value!r}')
    try:
        encoded_length = len(value.encode('utf-8'))
    except UnicodeEncodeError:
        raise ValueError(f'unsafe module path: {value!r}') from None
    if encoded_length > 240:
        raise ValueError(f'unsafe module path: {value!r}')
    components = value.split('/')
    if not all(_path_component_safe(c) for c in components):
        raise ValueError(f'unsafe module path: {value!r}')
    return value


def normalise(data):
    _required_fields(data, _TOP_LEVEL_FIELDS, 'thresholds')
    schema = _number(data['schema_version'], 'schema_version')
    if schema != _SCHEMA_VERSION or schema != schema.to_integral_value():
        raise ValueError(
            f'unsupported schema_version: {data["schema_version"]}')

    coverage_data = data['coverage']
    _required_fields(coverage_data, _COVERAGE_LANGUAGES, 'coverage')
    normalised_coverage = {}
    for language in _COVERAGE_LANGUAGES:
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
    for member in _BASELINE_FIELDS:
        normalised[member] = _baseline(data[member], member)
    return normalised


def _baseline(baseline, member):
    if not isinstance(baseline, dict):
        raise ValueError(f'{member} must be an object')
    normalised = {}
    for path, value in baseline.items():
        safe_path = _module_path(path)
        count = _number(value, f'{member}.{safe_path}')
        if count <= 0 or count != count.to_integral_value():
            raise ValueError(
                f'{member}.{safe_path} must be a positive integer')
        normalised[safe_path] = int(count)
    return dict(sorted(normalised.items()))


def load(path=THRESHOLDS):
    target = Path(path)
    try:
        raw = target.read_bytes()
    except OSError as error:
        raise ValueError(f'cannot read thresholds: {error}') from None
    return normalise(_decode(raw))


def coverage(data, language):
    if language not in _COVERAGE_LANGUAGES:
        raise ValueError(f'unknown coverage language: {language}')
    normalised = normalise(data)
    record = normalised['coverage'][language]
    return record['measured'], record['floor']


def module_size_baseline(data):
    return dict(normalise(data)['module_size_baseline'])


def _json_ready(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    return value


def _render(data):
    normalised = normalise(data)
    ready = _json_ready(normalised)
    text = json.dumps(
        ready, ensure_ascii=True, indent=2, sort_keys=True,
        allow_nan=False) + '\n'
    encoded = text.encode('utf-8')
    if normalise(_decode(encoded)) != normalised:
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
    modes.add_argument('--coverage-floor', choices=_COVERAGE_LANGUAGES,
                       help='print one language floor')
    modes.add_argument('--coverage-measured', choices=_COVERAGE_LANGUAGES,
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
