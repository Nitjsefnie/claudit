#!/usr/bin/env python3
"""Read, write, report and gate a reparse bench measurement.

The other half of ``reparse_bench.py``: that module runs the corpus and
takes the reading, this one carries it between the process that measured
it, the CI step that prints it, the gate that holds every phase to its
recorded budgets, and the ratchet that tightens them. Split because one
module doing both would outgrow the per-file size ratchet, and an
outgrown file moves code rather than growing a baseline entry
(SV-CI-RATCHETS).

A measurement is a small JSON document carrying TWO instruments over the
same phases, and only one of them gates (issue #513):

- ``bytecodes_<phase>`` — the phase's bytecode instructions per file, in
  thousands. Exact, so it needs no amplification and no tolerance, and
  it is what the gate holds a phase to: it catches the parse getting
  slower as a WHOLE, where every phase grows together.
- ``share_<phase>`` — the phase's percent of the pass's own CPU time.
  TELEMETRY: it shows where the pass spends its CPU, and it is measured,
  printed and recorded like the count, but nothing compares it against
  the recorded share budgets. A proportion of a timed run is not a count
  of work — its runner-to-runner spread (1.9-2.7 points, #500/#506) is
  wider than the 1.5-point gap it would be judged against, and it moves
  when the corpus mix shifts between formats of different parse cost even
  with no code path slower and every count under its own ceiling
  (PR #512).

Because the count is the only instrument the gate holds, an ABSENT count
fails it (fail closed): without ``sys.monitoring`` the step would
otherwise report a pass it never measured.

Numbers go through the file as STRINGS, not JSON numbers: they carry
exactly one decimal place by contract, and a float would round-trip them
to whatever the nearest double spells. The recorded value has to be the
value measured.

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
COUNT_UNIT = thresholds.REPARSE_COUNT_UNIT
PHASES = thresholds.REPARSE_PHASES
_OVER_BUDGET_REMEDY = (
    'A reparse phase is over its recorded budget: make that phase '
    'cheaper. A recorded budget is never raised by hand.')


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
    """Read a written measurement back, as the gate and ratchet see it."""
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


# --- the gate ----------------------------------------------------------------

def _counted(measurement: Measurement, phase: str) -> Decimal | None:
    """A phase's counted cost, or None where the instrument was absent."""
    per_file = measurement.instruction_per_file
    return per_file[phase] if per_file else None


def _over_budget(measurement: Measurement, floors: dict) -> list:
    """Every phase over a GATED recorded budget, and every phase the gate
    could not read.

    The bytecode count is the whole of the enforced set (issue #513), so
    an absent count is an absent gate and is listed as a failure with the
    reason the bench recorded — fail closed, never a silent pass on an
    instrument that never ran.
    """
    over = []
    reason = measurement.instruction_note
    for phase in PHASES:
        counted = _counted(measurement, phase)
        if counted is None:
            over.append(f'{phase}: NOT MEASURED'
                        f'{f" ({reason})" if reason else ""}')
            continue
        count_floor = floors[phase]['bytecodes']['floor']
        if counted > count_floor:
            over.append(f'{phase}: {counted} {COUNT_UNIT}, '
                        f'floor {count_floor}')
    return over


def check(path, thresholds_path=None) -> int:
    """Gate a written measurement against the committed budgets."""
    measurement = measurement_from_file(path)
    floors = thresholds.reparse(
        thresholds.load(thresholds_path or thresholds.THRESHOLDS))
    over = _over_budget(measurement, floors)
    if over:
        print('reparse work over its recorded budget:', file=sys.stderr)
        for line in over:
            print(f'  {line}', file=sys.stderr)
        print(_OVER_BUDGET_REMEDY, file=sys.stderr)
        return 1
    print(summary_line(measurement))
    return 0


def summary_line(measurement: Measurement) -> str:
    """One line naming every phase under its gated budget, with the
    share beside it as the telemetry it is, for the gate's own voice when
    it passes."""
    parts = []
    for name in PHASES:
        counted = _counted(measurement, name)
        part = f'{name} {measurement.shares[name]}%'
        if counted is not None:
            part += f'/{counted}'
        parts.append(part)
    return ('reparse work within budget (' + ', '.join(parts)
            + '; the share is telemetry, the count after it gates)')


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
        f'{COUNT_UNIT} column is the gate, the share column is telemetry:',
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
