#!/usr/bin/env python3
"""Read, write, report and gate a reparse bench measurement.

The other half of ``reparse_bench.py``: that module MEASURES the CPU work
of one reparse pass and splits it into phases, this one carries the
reading between a process that measured it, the CI step that prints it,
the gate that holds each phase to its recorded share, and the ratchet
that tightens those shares. Split because one module doing both would
outgrow the per-file size ratchet, and an outgrown file moves code
rather than growing a baseline entry (SV-CI-RATCHETS).

A measurement is a small JSON document: the CPU of the pass, each
phase's share of it, and the instruction count when a counter facility
was available. Shares go through the file as STRINGS, not JSON numbers:
they carry exactly one decimal place by contract, and a float would
round-trip them to whatever the nearest double spells. The recorded
value has to be the value measured.

  python3 scripts/ci/reparse_bench.py --write m.json   # measure + write
  python3 scripts/ci/reparse_bench.py --check m.json   # gate it
"""
from __future__ import annotations

import importlib
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import NamedTuple

if __package__:
    # pylint: disable-next=relative-beyond-top-level,no-name-in-module
    from . import thresholds
else:
    thresholds = importlib.import_module('thresholds')


UNIT = thresholds.REPARSE_UNIT
PHASES = thresholds.REPARSE_PHASES
_OVER_BUDGET_REMEDY = (
    'A reparse phase is over its recorded share of the pass: make that '
    'phase cheaper. The recorded budget is never raised by hand.')


class Measurement(NamedTuple):
    """One bench run: the CPU of each phase, and the shares recorded."""
    cpu_s: float
    files: int
    passes: int
    records: int
    phase_cpu_s: dict
    shares: dict
    instructions_per_file: int | None
    instruction_note: str


def measurement_json(measurement: Measurement) -> dict:
    return {
        'unit': UNIT,
        'files': measurement.files,
        'passes': measurement.passes,
        'records': measurement.records,
        'cpu_s': measurement.cpu_s,
        'ms_per_file': measurement.cpu_s
        / (measurement.passes * measurement.files) * 1000,
        'phases': {
            name: {
                'cpu_s': measurement.phase_cpu_s[name],
                'share': str(measurement.shares[name]),
            } for name in PHASES
        },
        'share_sum': str(sum(measurement.shares.values())),
        'instructions_per_file': measurement.instructions_per_file,
        'instructions_note': measurement.instruction_note,
    }


def write_measurement(path, measurement: Measurement) -> None:
    Path(path).write_text(
        json.dumps(measurement_json(measurement), indent=2, sort_keys=True)
        + '\n', encoding='utf-8')


def measurement_from_file(path) -> Measurement:
    """Read a written measurement back, as the gate and ratchet see it."""
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        if not isinstance(data, dict) or 'phases' not in data:
            raise ValueError('the measurement file carries no phases')
        return Measurement(
            data['cpu_s'], data['files'], data['passes'], data['records'],
            {name: data['phases'][name]['cpu_s'] for name in PHASES},
            {name: Decimal(data['phases'][name]['share'])
             for name in PHASES},
            data.get('instructions_per_file'),
            data.get('instructions_note', ''))
    except (KeyError, AttributeError, TypeError) as error:
        raise ValueError(f'unreadable measurement file: {error}') from None
    except OSError as error:
        raise ValueError(f'cannot read the measurement: {error}') from None


def check(path, thresholds_path=None) -> int:
    """Gate a written measurement against the committed floors."""
    measurement = measurement_from_file(path)
    floors = thresholds.reparse_cpu(
        thresholds.load(thresholds_path or thresholds.THRESHOLDS))
    over = []
    for phase in PHASES:
        share = measurement.shares[phase]
        floor = floors[phase]['floor']
        if share > floor:
            over.append(f'{phase}: {share}% of the pass, floor {floor}%')
    if over:
        print('reparse CPU over its recorded budget:', file=sys.stderr)
        for line in over:
            print(f'  {line}', file=sys.stderr)
        print(_OVER_BUDGET_REMEDY, file=sys.stderr)
        return 1
    print('reparse CPU within budget (' + ', '.join(
        f'{name} {measurement.shares[name]}%' for name in PHASES) + ')')
    return 0


# --- reporting ---------------------------------------------------------------

def report(measurement: Measurement) -> str:
    per_file_ms = (measurement.cpu_s
                   / (measurement.passes * measurement.files) * 1000)
    lines = [
        f'reparse CPU {measurement.cpu_s:.4f} s over '
        f'{measurement.files} transcripts x {measurement.passes} passes '
        f'({per_file_ms:.4f} ms/file), split by phase:'
    ]
    for name in PHASES:
        cpu_ms = measurement.phase_cpu_s[name] / (
            measurement.passes * measurement.files) * 1000
        lines.append(f'  {name:<11} {measurement.shares[name]:>5}%  '
                     f'{cpu_ms:.4f} ms/file')
    lines.append(f'  {"sum":<11} {sum(measurement.shares.values()):>5}%')
    if measurement.instructions_per_file is None:
        lines.append('  instructions: NOT MEASURED '
                     f'({measurement.instruction_note})')
    else:
        lines.append(f'  instructions: {measurement.instructions_per_file} '
                     f'per file ({measurement.instruction_note})')
    return '\n'.join(lines)


def machine_line(measurement: Measurement) -> str:
    fields = [
        f'files={measurement.files}',
        f'passes={measurement.passes}',
        f'records={measurement.records}',
        f'cpu_s={measurement.cpu_s:.4f}',
        f'unit={UNIT}',
    ]
    fields += [f'share_{name}={measurement.shares[name]:.1f}'
               for name in PHASES]
    fields.append(f'share_sum={sum(measurement.shares.values()):.1f}')
    if measurement.instructions_per_file is not None:
        fields.append(
            f'instructions_per_file={measurement.instructions_per_file}')
    return 'reparse_bench ' + ' '.join(fields)
