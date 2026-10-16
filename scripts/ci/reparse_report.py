#!/usr/bin/env python3
"""Read, write and report a reparse bench measurement.

Each phase carries bytecode instructions per file and its share of the
pass's CPU time. Shares are telemetry. Decimal readings travel as strings
to preserve the exact measured value across serialization.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import NamedTuple

UNIT = 'percent_of_pass_cpu'
COUNT_UNIT = 'bytecode_hundreds_per_file'
PHASES = ('sniff', 'parse_body', 'sidecar', 'residual')


class Measurement(NamedTuple):
    """One bench run: both instruments, phase by phase, over one corpus."""
    cpu_s: float
    files: int
    passes: int
    records: int
    phase_cpu_s: dict
    shares: dict
    instruction_counts: dict | None
    instruction_per_file: dict | None
    instruction_note: str
    perf_per_file: int | None
    perf_note: str


def counts_payload(counts) -> dict | None:
    """The counting instrument in the shape the measurement file carries.

    A plain dict on purpose: the measurement survives a round trip through
    the file, and a shape that changed across that trip would make the
    report read a live object in one run and a dict in the next.
    """
    if counts is None:
        return None
    if isinstance(counts, dict):
        return dict(counts)          # already shaped: a re-write of a read
    return {
        'available': bool(counts.available),
        'reason': counts.reason,
        'total_bytecodes': counts.total_bytecodes,
        'overhead_per_call': counts.overhead_per_call,
        'phase_bytecodes': dict(counts.phase_bytecodes),
    }


def measurement_json(measurement: Measurement) -> dict:
    return {
        'unit': UNIT,
        'count_unit': COUNT_UNIT,
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
                # A phase with no count is None, not "None": a string
                # the loader would have to refuse, and a zero that would
                # read as a phase that retires nothing.
                'bytecode_hundreds_per_file': (
                    str(measurement.instruction_per_file[name])
                    if (measurement.instruction_per_file
                        and measurement.instruction_per_file[name] is not None)
                    else None),
            } for name in PHASES
        },
        'share_sum': str(sum(measurement.shares.values())),
        'counts': counts_payload(measurement.instruction_counts),
        'perf_instructions_per_file': measurement.perf_per_file,
        'perf_note': measurement.perf_note,
    }


def write_measurement(path, measurement: Measurement) -> None:
    Path(path).write_text(
        json.dumps(measurement_json(measurement), indent=2, sort_keys=True)
        + '\n', encoding='utf-8')


def measurement_from_file(path) -> Measurement:
    """Read a written measurement back with both instruments intact."""
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        if not isinstance(data, dict) or 'phases' not in data:
            raise ValueError('the measurement file carries no phases')
        counts = data.get('counts') or None
        per_file = {
            name: (Decimal(data['phases'][name]['bytecode_hundreds_per_file'])
                   if data['phases'][name].get('bytecode_hundreds_per_file') is not None
                   else None)
            for name in PHASES
        }
        return Measurement(
            data['cpu_s'], data['files'], data['passes'], data['records'],
            {name: data['phases'][name]['cpu_s'] for name in PHASES},
            {name: Decimal(data['phases'][name]['share']) for name in PHASES},
            counts, per_file, (counts or {}).get('reason', ''),
            data.get('perf_instructions_per_file'),
            data.get('perf_note', ''))
    except (KeyError, AttributeError, TypeError) as error:
        raise ValueError(f'unreadable measurement file: {error}') from None
    except OSError as error:
        raise ValueError(f'cannot read the measurement: {error}') from None


def _counted(measurement: Measurement, phase: str) -> Decimal | None:
    """A phase's counted cost, or None where the instrument was absent."""
    per_file = measurement.instruction_per_file
    return per_file[phase] if per_file else None


# --- reporting ---------------------------------------------------------------

_REPORT_COLUMNS = ('phase', 'share', 'ms/file',
                   'bytecode_hundreds_per_file')
_RIGHT_ALIGN = (False, True, True, True)


def _md_row(cells, widths) -> str:
    """One padded markdown row: the phase column flush left, the numeric
    columns flush right, so the CI job log keeps its columns aligned and
    the table still renders pasted anywhere else (issue #490)."""
    return '| ' + ' | '.join(
        cell.rjust(width) if right else cell.ljust(width)
        for cell, width, right in zip(cells, widths, _RIGHT_ALIGN)) + ' |'


def _phase_table(measurement: Measurement) -> list:
    """The per-phase block as a padded markdown table. A phase the
    counting instrument missed carries a dash."""
    rows = []
    for name in PHASES:
        counted = _counted(measurement, name)
        cpu_ms = measurement.phase_cpu_s[name] / (
            measurement.passes * measurement.files) * 1000
        rows.append((
            name,
            f'{measurement.shares[name]}%',
            f'{cpu_ms:.4f}',
            '-' if counted is None else f'{counted}',
        ))
    widths = [max([len(_REPORT_COLUMNS[i])]
                  + [len(row[i]) for row in rows])
              for i in range(len(_REPORT_COLUMNS))]
    return [
        _md_row(_REPORT_COLUMNS, widths),
        '| ' + ' | '.join(
            (':' + '-' * (width - 1)) if not right
            else ('-' * (width - 1) + ':')
            for width, right in zip(widths, _RIGHT_ALIGN)) + ' |',
        *[_md_row(row, widths) for row in rows],
    ]


def report(measurement: Measurement) -> str:
    per_file_ms = (measurement.cpu_s
                   / (measurement.passes * measurement.files) * 1000)
    lines = [
        f'reparse CPU {measurement.cpu_s:.4f} s over '
        f'{measurement.files} transcripts x {measurement.passes} passes '
        f'({per_file_ms:.4f} ms/file), split by phase — the '
        f'{COUNT_UNIT} column is the counted work; the share is telemetry:',
        '',
        *_phase_table(measurement),
        f'  {"sum":<11} {sum(measurement.shares.values()):>5}%',
    ]
    if measurement.instruction_per_file is None:
        lines.append('  bytecodes: NOT MEASURED '
                     f'({measurement.instruction_note})')
    else:
        counts = measurement.instruction_counts or {}
        lines.append(
            f'  bytecodes: {counts.get("total_bytecodes")} over the counted '
            f'run, {counts.get("overhead_per_call")} per instrumented call '
            f'(both included in the phases above)')
    if measurement.perf_per_file is None:
        lines.append(f'  perf: NOT MEASURED ({measurement.perf_note})')
    else:
        lines.append(f'  perf: {measurement.perf_per_file} machine '
                     f'instructions per file ({measurement.perf_note})')
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
    if measurement.instruction_per_file is not None:
        fields += [f'bytecodes_{name}={_counted(measurement, name)}'
                   for name in PHASES
                   if _counted(measurement, name) is not None]
        counts = measurement.instruction_counts or {}
        fields.append(
            f'bytecodes_total={counts.get("total_bytecodes")}')
    if measurement.perf_per_file is not None:
        fields.append(f'perf_instructions_per_file={measurement.perf_per_file}')
    return 'reparse_bench ' + ' '.join(fields)
