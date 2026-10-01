#!/usr/bin/env python3
"""Read, write, report and gate a suite-bench measurement.

The other half of ``suite_bench.py``: that module runs the fixture and
takes the reading, this one carries it between the process that measured
it, the CI step that prints it, the gate that holds every phase to its
recorded budget, and the ratchet that tightens them. Split because one
module doing both would outgrow the per-file size ratchet, and an
outgrown file moves code rather than growing a baseline entry
(SV-CI-RATCHETS).

A measurement is a small JSON document carrying the run's phase
partition:

- ``instrument`` -- ``instruction_count`` when the phases are counted,
  ``process_time`` for the portable fallback. A counts-less measurement
  is NOT gateable: ``--check`` on one fails closed (nonzero, loud), so
  a measurement that is absent can never read as a measurement that
  passed.
- ``phases.<name>.million_instructions`` -- the phase's cost in
  millions of bytecode instructions. Numbers go through the file as
  STRINGS, not JSON numbers: they carry exactly one decimal place by
  contract, and a float would round-trip them to whatever the nearest
  double spells. The recorded value has to be the value measured.
- ``total_million_instructions`` -- the phase sum, carried so a
  partition hole cannot hide behind a missing field: it is checked
  against the phases on every read.
"""
from __future__ import annotations

import importlib
import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import NamedTuple

if __package__:
    # pylint: disable-next=relative-beyond-top-level,no-name-in-module
    from . import thresholds
else:
    thresholds = importlib.import_module('thresholds')

UNIT = thresholds.SUITE_COST_UNIT
PHASES = thresholds.SUITE_COST_PHASES
_COUNTS = 'million_instructions'
_OVER_BUDGET_REMEDY = (
    'A suite phase is over its recorded budget: make that phase '
    'cheaper, or use the doctrine\'s re-seed path when the fixture list '
    'or the interpreter pin legitimately changed. A recorded budget is '
    'never raised by hand.')


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
    """One bench run's reading, as the gate and ratchet see it.

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


def measurement_json(measurement: Measurement) -> dict:
    """The measurement in the shape the file carries."""
    return {
        'instrument': measurement.instrument,
        'unit': UNIT,
        'hash_seed': measurement.hash_seed,
        'tests': measurement.tests,
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
    """Read a written measurement back, as the gate and ratchet see it.

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
        )
    except (KeyError, AttributeError, TypeError, json.JSONDecodeError) as error:
        raise ValueError(f'unreadable measurement file: {error}') from None
    except OSError as error:
        raise ValueError(f'cannot read the measurement: {error}') from None


def _over_budget(counts: dict, budgets: dict) -> list:
    """Every phase over its recorded ceiling. Counts only: a wall or
    CPU reading may never move this verdict."""
    over = []
    for phase in PHASES:
        measured = counts[phase]
        ceiling = budgets[phase]['floor']
        if measured > ceiling:
            over.append(f'{phase}: {measured} {UNIT}, '
                        f'ceiling {ceiling}')
    return over


def check(path, thresholds_path=None) -> int:
    """Gate a written measurement against the committed budgets.

    A counts-less measurement (the process_time fallback) fails closed:
    the absence of an instruction count is not a pass.
    """
    measurement = measurement_from_file(path)
    if measurement.counts is None:
        print('the measurement carries no instruction count '
              f'(instrument {measurement.instrument!r}, '
              'the portable fallback): not gateable', file=sys.stderr)
        return 1
    budgets = thresholds.suite_cost(
        thresholds.load(thresholds_path or thresholds.THRESHOLDS))
    over = _over_budget(measurement.counts, budgets)
    if over:
        print('suite cost over its recorded budget:', file=sys.stderr)
        for line in over:
            print(f'  {line}', file=sys.stderr)
        print(_OVER_BUDGET_REMEDY, file=sys.stderr)
        return 1
    print(summary_line(measurement.counts))
    return 0


def summary_line(counts: dict) -> str:
    """One line naming every phase under its ceiling, for the gate's
    own voice when it passes."""
    parts = [f'{name} {counts[name]}' for name in PHASES]
    return (f'suite cost within budget '
            f'({", ".join(parts)} {UNIT}, '
            f'total {sum(counts.values())})')


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


def summary_markdown(measurement: Measurement, verdict=None) -> str:
    """The step-summary markdown: the phase table and the verdict."""
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
    if verdict is not None:
        lines += ['', verdict]
    return '\n'.join(lines) + '\n'
