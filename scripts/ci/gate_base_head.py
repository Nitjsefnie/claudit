#!/usr/bin/env python3
"""Compare same-run suite and reparse draws; no stored measured budgets."""
from __future__ import annotations

import argparse
import contextlib
import importlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SUITE_PHASES = ('collection', 'run', 'residual')
REPARSE_PHASES = ('sniff', 'parse_body', 'sidecar', 'residual')
# Observed scan classes: 40, 304, ~1,150 instructions/tests-tree line.
# This is an ABSOLUTE CAP, not a head-vs-base comparison. Both this cap
# and FIXED_MARGIN are the maintainer's dials.
SCAN_RATE_CAP = 2000
# Million instructions; the old gap's calibration, pending proof draws.
FIXED_MARGIN = Decimal('1.5')
# Million instructions; provisional. The committed value must not exceed
# 2x the base-vs-base deltas observed in --self-check; lead finalizes it.
NOISE = Decimal('0.2')
# Same pinned corpus and bytecodes instrument: provisional 2% headroom;
# the lead calibrates against same-tree proof draws, never CPU shares.
REPARSE_MARGIN_PCT = Decimal('0.02')
MILLION = Decimal(1_000_000)


def git(repo: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    """Run git without shell interpolation, retaining its failure reason."""
    done = subprocess.run(['git', '-C', str(repo), *args],
                          input=input_bytes, capture_output=True, check=False)
    if done.returncode:
        raise ValueError(f'git {" ".join(args)}: '
                         f'{done.stderr.decode(errors="replace").strip()}')
    return done.stdout


@contextlib.contextmanager
def trees(repo: Path, scratch: Path, base_ref: str | None,
          head_ref: str | None, head_tree: Path, self_check: bool = False):
    """Materialize detached draws and unregister them even after a failure."""
    scratch.mkdir(parents=True, exist_ok=True)
    added = []

    def add(name, ref):
        path = scratch / name
        # Register cleanup before add: interrupted creation may have already
        # registered the worktree even though the command did not succeed.
        added.append(path)
        git(repo, 'worktree', 'add', '--detach', str(path), ref)
        return path

    try:
        if self_check:
            ref = (head_ref or base_ref
                   or git(head_tree, 'rev-parse', 'HEAD').decode().strip())
            yield [add('draw1', ref), add('draw2', ref)]
        else:
            if base_ref is None:
                raise ValueError('--base-ref is required without --self-check')
            first = add('draw1', base_ref)
            second = add('draw2', base_ref)
            patch = git(repo if head_ref else head_tree,
                        'diff', '--binary', '--no-ext-diff', '--no-renames',
                        f'{base_ref}..{head_ref}' if head_ref else base_ref,
                        '--', 'tests/')
            if patch:
                git(second, 'apply', '--binary', '-', input_bytes=patch)
            third = add('draw3', head_ref) if head_ref else head_tree
            yield [first, second, third]
    finally:
        registered = git(repo, 'worktree', 'list', '--porcelain').decode()
        for path in reversed(added):
            if f'worktree {path}\n' in registered:
                git(repo, 'worktree', 'remove', '--force', str(path))


def _number(value, label: str) -> Decimal:
    try:
        number = Decimal(value)
    except (TypeError, InvalidOperation, ValueError):
        raise ValueError(f'{label}: missing or invalid count {value!r}') from None
    if not number.is_finite() or number < 0:
        raise ValueError(f'{label}: invalid count {value!r}')
    return number


def phase_counts(draw: dict, family: str) -> dict:
    """Require the counting instrument, never its CPU-time fallback."""
    data = draw[family]
    if family == 'suite':
        if data.get('instrument') != 'instruction_count':
            raise ValueError('suite instruction counter unavailable')
        phases, unit = SUITE_PHASES, 'million_instructions'
    else:
        if not (data.get('counts') or {}).get('available'):
            raise ValueError('reparse bytecodes counter unavailable')
        phases, unit = REPARSE_PHASES, 'bytecode_hundreds_per_file'
    return {phase: _number(data['phases'][phase].get(unit),
                           f'{family} {phase} count') for phase in phases}


def verdict(draws: list[dict]) -> dict:
    """Apply the signed scan delta, fixed deltas and reparse phase caps."""
    first, second, third = [phase_counts(draw, 'suite') for draw in draws]
    base_lines = draws[0]['suite']['tests_tree_lines']
    head_lines = draws[2]['suite']['tests_tree_lines']
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0
           for value in (base_lines, head_lines)):
        raise ValueError('tests_tree_lines must be nonnegative integers')
    failures = []
    comparisons = []

    def compare(clause, phase, before, after, allowance, margin, unit):
        delta = after - before
        passed = delta <= allowance
        comparisons.append({
            'clause': clause, 'phase': phase, 'base': str(before),
            'head': str(after), 'delta': str(delta),
            'allowance': str(allowance), 'unit': unit, 'passed': passed,
        })
        if not passed:
            failures.append(f'{clause} {phase}: {after} - {before} = {delta} '
                            f'{unit} > {allowance}; margin {margin}')

    compare('SCAN', 'run', first['run'], second['run'],
            Decimal(SCAN_RATE_CAP * (head_lines - base_lines)) / MILLION
            + NOISE,
            f'{SCAN_RATE_CAP} instructions/line * '
            f'({head_lines} - {base_lines}) + {NOISE}M noise',
            'million_instructions')
    for phase in SUITE_PHASES:
        compare('FIXED', phase, second[phase], third[phase], FIXED_MARGIN,
                f'{FIXED_MARGIN}M', 'million_instructions')
    base = phase_counts(draws[0], 'reparse')
    head = phase_counts(draws[2], 'reparse')
    for phase in REPARSE_PHASES:
        compare('REPARSE', phase, base[phase], head[phase],
                base[phase] * REPARSE_MARGIN_PCT,
                f'{(REPARSE_MARGIN_PCT * 100).normalize():g}%',
                'bytecode_hundreds_per_file')
    return {'passed': not failures, 'failures': failures,
            'comparisons': comparisons}


def _instrument(kind: str, root: Path, scratch: Path, output: Path) -> None:
    """Use the head's instruments against this draw's backend and tests."""
    sys.path.insert(0, str(root))
    importlib.import_module('backend')
    sys.path.insert(0, str(ROOT / 'scripts/ci'))
    bench = importlib.import_module(f'{kind}_bench')
    # The instruments add their own tree to sys.path at import. Backend
    # is already bound to the draw; tests and other imports belong there too.
    sys.path.insert(0, str(root))
    if kind == 'suite':
        fixture = bench.read_fixture(bench.FIXTURE, root)
        os.environ['PYTEST_ADDOPTS'] = shlex.join(
            ['--basetemp', str(scratch / 'pytest-warm')])
        bench.warm_bytecode_caches(fixture, root)
        os.environ['PYTEST_ADDOPTS'] = shlex.join(
            ['--basetemp', str(scratch / 'pytest-count')])
        bench._load_bounded_document()  # pylint: disable=protected-access
        measurement = bench.measure(fixture, root)
        bench.suite_report.write_measurement(output, measurement)
        print(bench.suite_report.report(measurement))
    else:
        entries = bench.corpus()
        counts = bench.reparse_phases.measure_counts(entries, bench.run_pass)
        per_file = (bench.reparse_phases.bytecodes_per_file(counts)
                    if counts.available else None)
        measurement = bench.measure(
            entries, instruction_counts=bench.reparse_report.counts_payload(counts),
            instruction_per_file=per_file, instruction_note=counts.reason)
        bench.reparse_report.write_measurement(output, measurement)
        print(bench.reparse_report.report(measurement))


def measure_draw(root: Path, scratch: Path) -> dict:
    """Fresh deterministic children prevent cross-tree module/cache reuse."""
    scratch.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PYTHONHASHSEED='0', PYTHONDONTWRITEBYTECODE='1',
               PYTHONPATH=str(root))
    draw = {}
    for kind in ('suite', 'reparse'):
        output = scratch / f'{kind}.json'
        subprocess.run(
            [sys.executable, str(Path(__file__).resolve()),
             '--instrument', kind, '--head-tree', str(root),
             '--scratch', str(scratch), '--write', str(output)],
            cwd=root, env=env, check=True)
        draw[kind] = json.loads(output.read_text(encoding='utf-8'))
    return draw


def deltas(draws: list[dict]) -> dict:
    return {family: {phase: str(value - phase_counts(draws[0], family)[phase])
                     for phase, value in phase_counts(draws[-1], family).items()}
            for family in ('suite', 'reparse')}


def summary(payload: dict) -> str:
    lines = ['### Suite and reparse cost base-vs-head', '']
    for family in ('suite', 'reparse'):
        lines += [f'{family} phase counts', '',
                  '| phase | ' + ' | '.join(
                      f'draw{i + 1}' for i in range(len(payload['draws'])))
                  + ' | delta last-first |',
                  '| --- |' + ' --- |' * (len(payload['draws']) + 1)]
        for phase in (SUITE_PHASES if family == 'suite' else REPARSE_PHASES):
            values = [str(phase_counts(draw, family)[phase])
                      for draw in payload['draws']]
            delta = payload.get('deltas', {}).get(family, {}).get(phase, '-')
            lines.append(f'| {phase} | ' + ' | '.join(values) + f' | {delta} |')
        lines.append('')
    lines.append('Tests-tree lines: ' + ', '.join(
        str(draw['suite']['tests_tree_lines']) for draw in payload['draws']))
    lines.extend(payload['verdict']['failures'])
    lines.append('PASS' if payload['verdict']['passed'] else 'FAIL')
    return '\n'.join(lines) + '\n'


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-ref')
    head = parser.add_mutually_exclusive_group()
    head.add_argument('--head-ref')
    head.add_argument('--head-tree', type=Path, default=Path.cwd())
    parser.add_argument('--self-check', action='store_true')
    parser.add_argument('--write', type=Path, default=Path('cost-gate-verdict.json'))
    parser.add_argument('--summary', type=Path)
    parser.add_argument('--instrument', choices=('suite', 'reparse'),
                        help=argparse.SUPPRESS)
    parser.add_argument('--scratch', type=Path, help=argparse.SUPPRESS)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    if args.instrument:
        _instrument(args.instrument, args.head_tree.resolve(), args.scratch,
                    args.write)
        return 0
    payload = {'self_check': args.self_check, 'draws': [],
               'verdict': {'passed': False, 'failures': []}}
    try:
        repo = Path(git(Path.cwd(), 'rev-parse', '--show-toplevel').decode().strip())
        payload['base_ref'] = args.base_ref
        payload['head_ref'] = args.head_ref
        with tempfile.TemporaryDirectory(prefix='claudit-cost-gate-') as temp:
            scratch = Path(temp)
            with trees(repo, scratch / 'trees', args.base_ref, args.head_ref,
                       args.head_tree.resolve(), args.self_check) as roots:
                for index, root in enumerate(roots):
                    payload['draws'].append(measure_draw(
                        root, scratch / f'measure{index + 1}'))
        payload['deltas'] = deltas(payload['draws'])
        payload['verdict'] = ({'passed': True, 'failures': []}
                              if args.self_check else verdict(payload['draws']))
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        payload['verdict'] = {'passed': False, 'failures': [str(error)]}
    finally:
        args.write.parent.mkdir(parents=True, exist_ok=True)
        args.write.write_text(json.dumps(payload, indent=2) + '\n', encoding='utf-8')
    try:
        text = summary(payload)
    except (ValueError, KeyError):
        text = '\n'.join(payload['verdict']['failures']) + '\nFAIL\n'
    print(text)
    if args.summary:
        with args.summary.open('a', encoding='utf-8') as stream:
            stream.write(text)
    return 0 if payload['verdict']['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
