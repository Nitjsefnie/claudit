#!/usr/bin/env python3
"""Compare same-run suite and reparse draws; no stored measured budgets."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

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
# Keep both ends of each child stream, including the final traceback cause.
DIAGNOSTIC_LIMIT = 16_000


def git(repo: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    """Run git without shell interpolation, retaining its failure reason."""
    done = subprocess.run(['git', '-C', str(repo), *args],
                          input=input_bytes, capture_output=True, check=False)
    if done.returncode:
        raise ValueError(f'git {" ".join(args)}: '
                         f'{done.stderr.decode(errors="replace").strip()}')
    return done.stdout


def _cleanup_trees(repo: Path, paths: list[Path]) -> None:
    """Attempt every removal and report errors without replacing the result."""
    failures = []
    for path in reversed(paths):
        try:
            git(repo, 'worktree', 'remove', '--force', str(path))
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            failures.append(f'worktree cleanup {path}: {error}')
    for failure in failures:
        print(failure, file=sys.stderr)


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
        _cleanup_trees(repo, added)


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
        if data.get('hash_seed') != '0':
            raise ValueError('suite hash_seed must be 0 for deterministic draws')
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


def _env(root: Path) -> dict:
    env = dict(os.environ, PYTHONHASHSEED='0', PYTHONDONTWRITEBYTECODE='1',
               PYTHONPATH=str(root))
    # A parent's warm marker cannot vouch for this draw's bytecode caches.
    env.pop('CLAUDIT_SUITE_BENCH_WARMED', None)
    return env


def _suite_command(root: Path, output: Path) -> list[str]:
    # Each tree uses its own instrument and loader. Instrument cost changes
    # belong in the delta; this PR rebuilds the bench, so its proofs cite
    # their own runs rather than pretending the instruments were identical.
    return [sys.executable, str(root / 'scripts/ci/suite_bench.py'),
            '--repo-root', str(root), '--write', str(output)]


def _reparse_command(root: Path, output: Path) -> list[str]:
    return [sys.executable, str(root / 'scripts/ci/reparse_bench.py'),
            '--write', str(output)]


def _bounded(text: str) -> str:
    if len(text) <= DIAGNOSTIC_LIMIT:
        return text
    half = DIAGNOSTIC_LIMIT // 2
    return text[:half] + '\n... output truncated ...\n' + text[-half:]


def _run_measurement(command: list[str], root: Path, output: Path) -> dict:
    done = subprocess.run(command, cwd=root, env=_env(root),
                          capture_output=True, text=True, check=False)
    stdout, stderr = _bounded(done.stdout), _bounded(done.stderr)
    try:
        if done.returncode:
            raise ValueError(f'exited {done.returncode}')
        measurement = json.loads(output.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise ValueError(f'{command[1]} draw at {root}: {error}\n'
                         f'stdout:\n{stdout}\nstderr:\n{stderr}') from error
    print(stdout, end='')
    print(stderr, end='', file=sys.stderr)
    return measurement


def _instrument(kind: str, root: Path, scratch: Path, output: Path) -> None:
    """Run the selected tree's own instrument CLI in a fresh child."""
    scratch.mkdir(parents=True, exist_ok=True)
    command = (_suite_command(root, output) if kind == 'suite'
               else _reparse_command(root, output))
    _run_measurement(command, root, output)


def measure_draw(root: Path, scratch: Path) -> dict:
    """Fresh deterministic children prevent cross-tree module/cache reuse."""
    scratch.mkdir(parents=True, exist_ok=True)
    draw = {}
    for kind in ('suite', 'reparse'):
        output = scratch / f'{kind}.json'
        command = (_suite_command(root, output) if kind == 'suite'
                   else _reparse_command(root, output))
        draw[kind] = _run_measurement(command, root, output)
    return draw


def deltas(draws: list[dict]) -> dict:
    return {family: {phase: str(value - phase_counts(draws[0], family)[phase])
                     for phase, value in phase_counts(draws[-1], family).items()}
            for family in ('suite', 'reparse')}


def summary(payload: dict) -> str:
    lines = ['### Suite and reparse cost base-vs-head', '']
    if not payload['draws']:
        return '\n'.join(lines + ['Draws: not measured.',
                                  *payload['verdict']['failures'], 'FAIL', ''])
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
    parser = _parser()
    args = parser.parse_args(argv)
    if args.instrument:
        if args.scratch is None:
            parser.error('--scratch is required with --instrument')
        try:
            _instrument(args.instrument, args.head_tree.resolve(), args.scratch,
                        args.write)
            return 0
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            print(error, file=sys.stderr)
            return 1
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
