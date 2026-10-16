#!/usr/bin/env python3
"""Read, write and report a suite-bench measurement.

Phase counts travel as one-decimal strings to preserve exact readings;
the total is validated against the phase sum on every read. CPU time
travels beside the instruction count as telemetry.
"""
from __future__ import annotations

import importlib
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import NamedTuple

if __package__:
    # pylint: disable-next=relative-beyond-top-level,no-name-in-module
    from . import suite_phases
else:
    suite_phases = importlib.import_module('suite_phases')

UNIT = 'million_instructions'
PHASES = suite_phases.PHASES
_COUNTS = 'million_instructions'


def _spelling(value, label):
    """One one-decimal string reading, or a refusal that names it."""
    if not isinstance(value, str):
        raise ValueError(f'{label}: must be a one-decimal string')
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError(
            f'{label}: must be a one-decimal string') from None
    if number.as_tuple().exponent != -1:
        raise ValueError(f'{label}: must be a one-decimal string')
    return number


class Measurement(NamedTuple):
    """One bench run's reading, with its phase counts and telemetry.

    ``counts`` carries ALL THREE phases (residual included) as one-place
    Decimals, or None when the instrument was the fallback. ``cpu_s``
    carries the process-time telemetry beside them, as strings, for
    every run whatever the instrument.
    """
    instrument: str
    hash_seed: str
    tests: int
    counts: dict | None
    cpu_s: dict
    fixture: str
    interpreter: str
    tests_tree_lines: int | None = None


def measurement_json(measurement: Measurement) -> dict:
    """The measurement in the shape the file carries."""
    return {
        'instrument': measurement.instrument,
        'unit': UNIT,
        'hash_seed': measurement.hash_seed,
        'tests': measurement.tests,
        'tests_tree_lines': measurement.tests_tree_lines,
        'fixture': measurement.fixture,
        'interpreter': measurement.interpreter,
        'phases': {
            name: {
                _COUNTS: (str(measurement.counts[name])
                          if measurement.counts is not None else None),
                'process_time_s': measurement.cpu_s[name],
            } for name in PHASES
        },
        'total_million_instructions': (
            str(sum(measurement.counts.values()))
            if measurement.counts is not None else None),
    }


def write_measurement(path, measurement: Measurement) -> None:
    Path(path).write_text(
        json.dumps(measurement_json(measurement), indent=2, sort_keys=True)
        + '\n', encoding='utf-8')


def measurement_from_file(path) -> Measurement:
    """Read a written measurement back, with its phase counts and telemetry.

    The total is checked against the phase sum on every read, so a
    partition hole cannot hide behind a missing or stale field.
    """
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        if not isinstance(data, dict) or 'phases' not in data:
            raise ValueError('the measurement file carries no phases')
        counted = data.get('instrument') == 'instruction_count'
        counts = None
        if counted:
            counts = {}
            for name in PHASES:
                spelling = data['phases'][name][_COUNTS]
                if spelling is None:
                    raise ValueError(
                        f'the {name} phase carries no instruction count; '
                        f'the measurement claims instrument '
                        f'{data.get("instrument")!r}')
                counts[name] = _spelling(
                    spelling, f'{name}.{_COUNTS}')
            total = _spelling(data['total_million_instructions'],
                              'total_million_instructions')
            if total != sum(counts.values()):
                raise ValueError(
                    'the total does not equal the phase sum '
                    f'({total} != {sum(counts.values())}): a partition '
                    'hole cannot be ruled out')
        return Measurement(
            instrument=data.get('instrument', 'unknown'),
            hash_seed=str(data.get('hash_seed', 'unknown')),
            tests=int(data.get('tests', 0)),
            counts=counts,
            cpu_s={name: str(data['phases'][name].get('process_time_s'))
                   for name in PHASES},
            fixture=str(data.get('fixture', '')),
            interpreter=str(data.get('interpreter', '')),
            tests_tree_lines=_identity(data),
        )
    except (KeyError, AttributeError, TypeError, json.JSONDecodeError) as error:
        raise ValueError(f'unreadable measurement file: {error}') from None
    except OSError as error:
        raise ValueError(f'cannot read the measurement: {error}') from None


def _identity(data: dict) -> int | None:
    """The measurement's workload identity, or None when it predates it.

    An integer when present — 0 is an honest reading for a tree with
    no tests/. A bool is not an integer here.
    """
    identity = data.get('tests_tree_lines')
    if identity is None:
        return None
    if isinstance(identity, bool) or not isinstance(identity, int):
        raise ValueError('tests_tree_lines must be an integer')
    return identity


def report(measurement: Measurement) -> str:
    """The human report: phases, counts, the CPU telemetry beside them."""
    lines = [
        f'suite bench over {measurement.tests} tests '
        f'({measurement.fixture}):',
        f'  {"phase":<12} {UNIT:>24}  {"process_time_s":>16}',
    ]
    for name in PHASES:
        counted = ('-' if measurement.counts is None
                   else str(measurement.counts[name]))
        lines.append(
            f'  {name:<12} {counted:>24}  {measurement.cpu_s[name]:>16}')
    total = ('-' if measurement.counts is None
             else str(sum(measurement.counts.values())))
    lines.append(f'  {"total":<12} {total:>24}')
    if measurement.counts is None:
        lines.append(f'  instrument: {measurement.instrument} '
                     '(telemetry only, not gateable)')
    else:
        lines.append(f'  instrument: {measurement.instrument}, '
                     f'hash seed {measurement.hash_seed}, '
                     f'interpreter {measurement.interpreter}')
    return '\n'.join(lines)


def summary_markdown(measurement: Measurement) -> str:
    """The step-summary markdown phase table."""
    lines = ['### Suite cost', '',
             '| phase | ' + UNIT + ' | process_time_s |',
             '| --- | --- | --- |']
    for name in PHASES:
        counted = ('-' if measurement.counts is None
                   else str(measurement.counts[name]))
        lines.append(f'| {name} | {counted} | '
                     f'{measurement.cpu_s[name]} |')
    total = ('-' if measurement.counts is None
             else str(sum(measurement.counts.values())))
    lines.append(f'| total | {total} | - |')
    return '\n'.join(lines) + '\n'
