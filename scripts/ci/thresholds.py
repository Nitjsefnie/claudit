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
instruction_value: _Number = validate.instruction_value
share_value: _Number = validate.share_value
count_value: _Number = validate.count_value

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
# The unit the reparse bench records (scripts/ci/reparse_bench.py): each
# phase's share of one reparse pass's own CPU work, in percent. A share
# rather than an absolute cost because a ratio inside one run does not
# drift with the machine or the co-tenant load the way an absolute
# total does — see the docstring of _reparse for the rest.
REPARSE_UNIT = 'percent_of_pass_cpu'
# The second instrument's unit: hundreds of bytecode instructions per
# file, counted with sys.monitoring's INSTRUCTION event. Exact, so it
# needs no amplification and no tolerance — which is also why its gap is
# sized for interpreter drift rather than for noise.
REPARSE_COUNT_UNIT = 'bytecode_hundreds_per_file'
# The reparse bench's calibration: a required member, like a coverage
# language, because a document without one leaves the bench's gate step
# with nothing to check the measurement against. Unlike coverage, each
# of its records is a COST, so its ratchet only ever TIGHTENS (both
# fields move down) and its gap sits above the measured value; see
# _reparse, and _suite_cost for the same rule stated for suite_cost.
REPARSE_FAMILY = 'reparse'
# The two instruments recorded per phase, and why both are here: a share
# is scale-free inside one run and catches work MOVING between phases; a
# bytecode count is exact and catches the pass getting slower as a WHOLE,
# which a share cannot see because every phase grows together.
REPARSE_METRICS = ('bytecodes', 'share')
# The phases the reparse bench splits a pass into — the same names it
# instruments (scripts/ci/reparse_bench.py reads them from here, so the
# document and the measurement cannot disagree about what a phase is).
REPARSE_PHASES = ('sniff', 'parse_body', 'sidecar', 'residual')
# Every only-shrinks baseline member: recorded numbers never rise and
# entries are never hand-added (SV-CI-RATCHETS); the direction guard
# (thresholds_guard.py) enforces the rule against the base document.
BASELINE_MEMBERS = ('module_size_baseline', 'pylint_suppression_baseline')
# The suite-cost family: one instruction budget per phase of a pytest run
# over the pinned bench fixture (scripts/ci/suite_bench.py). A cost
# ceiling only ever TIGHTENS downward; the direction guard forbids the
# upward move. The phase names live here, with the loader that validates
# the document carrying them, so the bench cannot measure under one name
# and record under another.
SUITE_COST_FAMILY = 'suite_cost'
SUITE_COST_PHASES = ('collection', 'run', 'residual')
SUITE_COST_UNIT = 'million_instructions'
_SUITE_COST_FIELDS = ('measured', 'floor')
_TOP_LEVEL_FIELDS = ('schema_version', 'coverage',
                     REPARSE_FAMILY, *BASELINE_MEMBERS)
# The suite-cost family is required like the rest, EXCEPT on a commit
# that declares the sanctioned re-seed in flight (reseed.py): the
# delete-then-seed sequence needs one commit whose document carries no
# budget at all, and that commit names itself with the marker. It is the
# only absence the loader tolerates, and only for this family.
_RESEED_OPTIONAL_FIELDS = (SUITE_COST_FAMILY,)
_COVERAGE_FIELDS = ('measured', 'floor')
_FIELD_LABELS = {
    'thresholds': 'field: {field}',
    'coverage': 'coverage language: {field}',
    SUITE_COST_FAMILY: 'suite cost phase: {field}',
    **{f'{SUITE_COST_FAMILY}.{phase}': f'suite cost {phase}: {{field}}'
       for phase in SUITE_COST_PHASES},
    REPARSE_FAMILY: 'reparse CPU phase: {field}',
    # A label per phase and per phase-metric, so a refusal inside one
    # phase's record names the phase (and the metric) rather than a bare
    # field.
    **{f'{REPARSE_FAMILY}.{phase}': f'reparse CPU {phase}: {{field}}'
       for phase in REPARSE_PHASES},
    **{f'{REPARSE_FAMILY}.{phase}.{metric}':
       f'reparse CPU {phase} {metric}: {{field}}'
       for phase in REPARSE_PHASES for metric in REPARSE_METRICS},
}


def verdict(reseed_in_flight=None):
    """The re-seed verdict to validate under: asked of the TREE unless a
    caller states one.

    This and ``load`` are the only places the tree is consulted, and
    every helper that re-validates a document another helper produced
    asks through here. ``normalise`` takes NO default for exactly that
    reason: with one, a helper two calls deep could silently read strict
    while its caller read the tree, and a document the loader accepted
    would be refused one frame lower -- on the master-only ratchet step,
    on every push.
    """
    if reseed_in_flight is not None:
        return reseed_in_flight
    # Imported HERE rather than at module scope: reseed shells out to
    # git, and every consumer of this loader — the reparse bench among
    # them, whose measured CPU share is the gate's own instrument —
    # would otherwise carry a module that reads a commit message to
    # answer a question it never asks.
    # pylint: disable-next=import-outside-toplevel
    reseed = importlib.import_module('reseed')
    return reseed.in_flight()


def _required_fields(value, expected, name, optional=()):
    """The moved field check, with this module's family labels."""
    return validate.required_fields(
        value, expected, name, _FIELD_LABELS, optional)


def normalise(data, reseed_in_flight):
    """The document in canonical form, or a refusal naming the offender.

    ``reseed_in_flight`` is REQUIRED and is the re-seed marker's verdict
    (reseed.py), never a mode a caller picks for its own convenience:
    with it, the suite-cost family may be ABSENT, and stays absent in the
    result, so the document's bytes round-trip unchanged. Everything else
    — the family's own validation when it is present, the required
    fields, the unknown-key refusal — is unchanged, so a marker can buy
    the absence and nothing else.

    No default: the caller is the only one who knows whether it is
    holding a document the loader accepted or one it just built. ``None``
    is not accepted here either — ask through ``verdict()``, so the tree
    is consulted in one named place rather than by omission.
    """
    if not isinstance(reseed_in_flight, bool):
        raise ValueError('reseed_in_flight must be a bool, not None: ask '
                         'thresholds.verdict() for the tree answer')
    required = (_TOP_LEVEL_FIELDS if reseed_in_flight
                else _TOP_LEVEL_FIELDS + _RESEED_OPTIONAL_FIELDS)
    _required_fields(data, required, 'thresholds',
                     _RESEED_OPTIONAL_FIELDS if reseed_in_flight else ())
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
        REPARSE_FAMILY: _reparse(data[REPARSE_FAMILY]),
    }
    if SUITE_COST_FAMILY in data:
        # PRESENCE, never emptiness: `"suite_cost": {}` is a family that
        # is present and malformed, and validating on emptiness would
        # normalise it to the same absent family only the marker can
        # authorise — a hand-deleted budget reaching the no-budget
        # state on any tree. Here it is refused, marker or no marker,
        # exactly as master refused it.
        normalised[SUITE_COST_FAMILY] = _suite_cost(data[SUITE_COST_FAMILY])
    for member in BASELINE_MEMBERS:
        normalised[member] = _baseline(data[member], member)
    return normalised


def _suite_cost(family):
    """Validate one suite_cost member: three phases, ceilings above.

    Each phase record carries measured and floor, the floor the ceiling
    a run must stay under and so exactly the calibration gap ABOVE the
    measured value — the mirror of coverage, whose floor sits below its
    quality number.
    """
    if not isinstance(family, dict):
        raise ValueError(f'{SUITE_COST_FAMILY} must be an object')
    _required_fields(family, SUITE_COST_PHASES, SUITE_COST_FAMILY)
    normalised = {}
    for phase in SUITE_COST_PHASES:
        prefix = f'{SUITE_COST_FAMILY}.{phase}'
        record = family[phase]
        _required_fields(record, _SUITE_COST_FIELDS, prefix)
        measured = instruction_value(record['measured'],
                                     f'{prefix}.measured')
        floor = instruction_value(record['floor'], f'{prefix}.floor')
        if floor <= measured:
            raise ValueError(f'{prefix}.floor must be above measured')
        if floor - measured != CALIBRATION_GAP:
            raise ValueError(f'{prefix} calibration gap must be 1.5')
        normalised[phase] = {'measured': measured, 'floor': floor}
    return normalised


def _reparse(family):
    """Validate the reparse bench's per-phase calibrations.

    Two records per phase — a percent share of the pass's own CPU and a
    bytecode count — each with its gap ABOVE the measured value, because
    a cost's floor is the ceiling it may be exceeded by. Writing it the
    coverage way round (floor = measured - gap) would put the ceiling
    BELOW the measurement that recorded it, and every later run at that
    measurement would fail a gate no change could satisfy.
    """
    _required_fields(family, REPARSE_PHASES, REPARSE_FAMILY)
    by_metric = {'share': share_value, 'bytecodes': count_value}
    normalised = {}
    for phase in REPARSE_PHASES:
        record = family[phase]
        prefix = f'{REPARSE_FAMILY}.{phase}'
        _required_fields(record, REPARSE_METRICS, prefix)
        normalised[phase] = {}
        for metric in REPARSE_METRICS:
            pair = record[metric]
            label = f'{prefix}.{metric}'
            _required_fields(pair, _COVERAGE_FIELDS, label)
            measured = by_metric[metric](pair['measured'], f'{label}.measured')
            floor = by_metric[metric](pair['floor'], f'{label}.floor')
            if floor <= measured:
                raise ValueError(f'{label}.floor must be above measured')
            if floor - measured != CALIBRATION_GAP:
                raise ValueError(f'{label} calibration gap must be 1.5')
            normalised[phase][metric] = {
                'measured': measured, 'floor': floor}
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


def load(path=THRESHOLDS, reseed_in_flight=None):
    """Read and validate a thresholds document.

    ``reseed_in_flight`` defaults to the tree's own verdict
    (reseed.in_flight), so every reader — the gates, the ratchets, the
    tests that pin the committed document against the tree — gets the
    one sanctioned tolerance without having to remember to ask for it,
    and a caller that means to be strict passes False explicitly. An
    unreadable tree is not a marker: the probe fails closed, and the
    document is gated exactly as it is today.
    """
    target = Path(path)
    try:
        raw = target.read_bytes()
    except OSError as error:
        raise ValueError(f'cannot read thresholds: {error}') from None
    return normalise(validate.decode(raw),
                     verdict(reseed_in_flight))


# The family readers below re-normalise the document they are handed, so
# each asks the SAME question load() asks, and defaults to the same
# answer: a reader handed a document read under the re-seed tolerance
# must not then refuse it, and a caller that means to be strict says so.
def coverage(data, language, reseed_in_flight=None):
    if language not in COVERAGE_LANGUAGES:
        raise ValueError(f'unknown coverage language: {language}')
    normalised = normalise(data, verdict(reseed_in_flight))
    record = normalised['coverage'][language]
    return record['measured'], record['floor']


def suite_cost(data, reseed_in_flight=None):
    """The committed suite-cost budgets: {phase: {measured, floor}}.

    Empty on a re-seed commit, which declares the family absent: the
    caller is then the seed step that reads counts from a measurement,
    not a gate reading a budget.
    """
    family = normalise(data, verdict(reseed_in_flight))
    return dict(family.get(SUITE_COST_FAMILY, {}))


def module_size_baseline(data, reseed_in_flight=None):
    normalised = normalise(data, verdict(reseed_in_flight))
    return dict(normalised['module_size_baseline'])


def reparse(data, reseed_in_flight=None):
    """The reparse bench's records: phase -> metric -> measured/floor."""
    return dict(normalise(data, verdict(reseed_in_flight))[REPARSE_FAMILY])


def _json_ready(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    return value


def _render(data, reseed_in_flight):
    reseed_in_flight = verdict(reseed_in_flight)
    normalised = normalise(data, reseed_in_flight)
    ready = _json_ready(normalised)
    text = json.dumps(
        ready, ensure_ascii=True, indent=2, sort_keys=True,
        allow_nan=False) + '\n'
    encoded = text.encode('utf-8')
    if normalise(validate.decode(encoded), verdict(
            reseed_in_flight)) != normalised:
        raise ValueError('serialized thresholds failed validation')
    return encoded


def _remove_temp(path):
    try:
        Path(path).unlink()
    except OSError:
        # Cleanup must not hide the publication failure that prompted it.
        pass


def write(path, data, reseed_in_flight=None):
    """Validate and atomically replace ``path`` with canonical JSON bytes.

    A re-seed commit's document round-trips like any other: the absent
    family is written back absent, and the round-trip check validates
    under the same verdict the reader will.
    """
    target = Path(path)
    payload = _render(data, verdict(reseed_in_flight))
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


def _reparse_pair(data, phase_metric, field, reseed_in_flight=None):
    """One recorded number for one phase and metric, named in the error
    it raises: an unknown phase or metric is a caller mistake, and the
    loader's own messages speak of the document."""
    phase, metric = phase_metric
    records = reparse(data, reseed_in_flight)
    if phase not in records:
        raise ValueError(f'unknown reparse phase: {phase}')
    if metric not in records[phase]:
        raise ValueError(
            f'unknown reparse metric for {phase}: {metric}')
    return records[phase][metric][field]


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--check', action='store_true',
                       help='validate the threshold document')
    modes.add_argument('--coverage-floor', choices=COVERAGE_LANGUAGES,
                       help='print one language floor')
    modes.add_argument('--coverage-measured', choices=COVERAGE_LANGUAGES,
                       help='print one language measured value')
    modes.add_argument('--reparse-floor', nargs=2, metavar=('PHASE', 'METRIC'),
                       help='print one reparse phase floor')
    modes.add_argument('--reparse-measured', nargs=2,
                       metavar=('PHASE', 'METRIC'),
                       help='print one reparse phase measured value')
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
        elif args.reparse_floor:
            print(f'{_reparse_pair(data, args.reparse_floor, "floor"):.1f}')
        elif args.reparse_measured:
            print(f'{_reparse_pair(data, args.reparse_measured, "measured"):.1f}')
        else:
            print('thresholds valid')
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
