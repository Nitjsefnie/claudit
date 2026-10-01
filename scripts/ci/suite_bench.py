#!/usr/bin/env python3
"""Measure one pytest run's cost in instructions, phase by phase.

The speed gate's instrument (issue #452). The old gate compared
WALL-CLOCK durations between two checkouts, A/B/A/B, against a pinned
baseline release; a runner's speed varies by roughly a factor of two
between jobs, so the ratio measured the runners as much as the code.
This bench replaces that with a committed instruction-count ratchet:

- A COUNT IS EXACT. ``sys.monitoring``'s INSTRUCTION event fires once
  per bytecode the interpreter retires; the same tree retires the same
  count on a loaded machine and an idle one. One pass over the pinned
  fixture (``suite_bench_files.txt``), no statistics, no warm-up,
  ``PYTHONHASHSEED=0`` by re-exec -- the run is deterministic by
  construction, and the partition into collection / run / residual is
  complete (suite_phases.partition refuses a negative residual).
- THE COMMITTED DOCUMENT IS THE BASELINE. No baseline-release checkout,
  no merge-base computation, no A/B pairing, no ratio: the budgets live
  in ``.github/ci-thresholds.json`` next to the coverage ratchets and
  are tightened downward, never raised, by ``suite_ratchet.py``.
- WALL TIME IS TELEMETRY AT MOST, and the only time reading these
  modules take is ``time.process_time()`` -- the portable fallback when
  no ``sys.monitoring`` exists, marked ``process_time`` in the
  measurement and NEVER gateable: ``--check`` on a counts-less
  measurement fails closed.

  python3 scripts/ci/suite_bench.py --write m.json   # measure + write
  python3 scripts/ci/suite_bench.py --check m.json   # gate it
"""
from __future__ import annotations

import argparse
import contextlib
import importlib
import os
import platform
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if __package__:
    # pylint: disable-next=relative-beyond-top-level,no-name-in-module
    from . import suite_phases, suite_report, thresholds
else:
    thresholds = importlib.import_module('thresholds')
    suite_phases = importlib.import_module('suite_phases')
    suite_report = importlib.import_module('suite_report')

# The committed fixture list, and the repo whose test files it names.
FIXTURE = Path(__file__).resolve().parent / 'suite_bench_files.txt'
_ONE_PLACE = Decimal('0.1')
# pytest exit codes a usable run may return: 0 all passed, 1 tests
# failed -- a failing test does not fail the bench (the tests gate owns
# that verdict; a non-passing test still retires its instructions).
_USABLE = (0, 1)
_QUANTUM = _ONE_PLACE
# The reading's shape lives in the report module; this alias is what
# the annotations name, exactly as reparse_bench does.
Measurement = suite_report.Measurement


def read_fixture(path, repo_root) -> list:
    """The pinned fixture: every non-comment line, resolved and real.

    One test-file path per line, relative to the repo root, ``#``
    comments and blank lines allowed. An empty list, a missing file or a
    path outside the repo root is a setup error, not a measurement.
    """
    entries = []
    for number, line in enumerate(
            Path(path).read_text(encoding='utf-8').splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        target = (repo_root / stripped).resolve()
        if not target.is_file():
            raise ValueError(
                f'{path}:{number}: {stripped} is not a file under '
                f'{repo_root}')
        entries.append(target)
    if not entries:
        raise ValueError(f'{path}: the fixture list is empty')
    return entries


def measure(fixture: list[Path], repo_root: Path) -> Measurement:
    """One pass over the fixture, partitioned, counted, reported.

    Runs pytest in-process (``pytest.main``) with the phase plugin; the
    instruction counter counts the whole region, the plugin's windows
    attribute collection and run, and the residual is the divergence.
    """
    counter = suite_phases.InstructionCounter()
    sink = suite_phases.ProcessTimeSink()
    plugin = suite_phases.SuitePhasePlugin(counter, sink)
    args = [
        *[str(path) for path in fixture],
        '-q', '--no-header', '--tb=no', '-p', 'no:cacheprovider',
    ]
    cpu_start = time.process_time()
    with contextlib.ExitStack() as stack:
        if counter.available:
            stack.enter_context(counter.window('total'))
        code = pytest.main(args, plugins=[plugin])
    cpu_total = time.process_time() - cpu_start
    if code not in _USABLE:
        raise ValueError(
            f'the pinned fixture did not produce a usable run '
            f'(pytest exited {int(code)})')
    cpu_phases = suite_phases.partition(
        _cpu(cpu_total), _cpu(sink.phases['collection']),
        _cpu(sink.phases['run']),
        tolerance=Decimal('0.001'))
    counts = None
    if counter.available:
        raw = suite_phases.partition(
            Decimal(counter.total),
            Decimal(counter.phases['collection']),
            Decimal(counter.phases['run']))
        counts = {phase: (raw[phase] / suite_phases.SCALE).quantize(
            _QUANTUM) for phase in suite_phases.PHASES}
    return Measurement(
        instrument=('instruction_count' if counter.available
                    else 'process_time'),
        hash_seed=('0' if sys.flags.hash_randomization == 0
                   else 'randomized'),
        tests=plugin.tests,
        counts=counts,
        cpu_s={phase: f'{cpu_phases[phase]:.3f}'
               for phase in suite_phases.PHASES},
        fixture=str(FIXTURE),
        interpreter=platform.python_version(),
    )


def _cpu(seconds: float) -> Decimal:
    return Decimal(repr(seconds))


def _reexec_with_deterministic_hash_seed(argv) -> None:
    """Re-exec once with ``PYTHONHASHSEED=0`` when not already set.

    Set iteration order is the one input left to hash order in a pytest
    run, and pytest walks sets in several places. Re-exec makes the run
    deterministic by construction; the measurement records the
    interpreter's own confirmation (``sys.flags.hash_randomization``),
    and the determinism probe over the real fixture (five runs, two
    seed values, identical counts) proves it empirically.

    The child also runs with PYTHONDONTWRITEBYTECODE: a gate that
    wrote ``__pycache__`` into the checkout would dirty the tree, and
    a compile-on-first-run-then-load split is the one remaining
    instruction-count wobble. The seed and the gate assume the CI
    shape -- a fresh checkout, no repository bytecode caches, so every
    run compiles the tree's own modules identically.
    """
    if os.environ.get('PYTHONHASHSEED') == '0':
        os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
        return
    env = dict(os.environ, PYTHONHASHSEED='0', PYTHONDONTWRITEBYTECODE='1')
    # The child's streams are inherited on purpose (its report is
    # the run's output), so only its exit code is read back.
    # pylint: disable-next=subprocess-run-check
    raise SystemExit(subprocess.run(
        [sys.executable, os.path.abspath(__file__), *argv],
        env=env).returncode)


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--write', type=Path, metavar='MEASUREMENT',
                       help='measure and write the measurement file')
    modes.add_argument('--check', type=Path, metavar='MEASUREMENT',
                       help='gate a written measurement against the '
                            'committed budgets; nonzero when a phase '
                            'is over')
    parser.add_argument('--fixture', type=Path, default=FIXTURE,
                        help='the pinned fixture list (suite_bench_files'
                             '.txt beside this script)')
    parser.add_argument('--repo-root', type=Path, default=ROOT,
                        help='the repo whose test files the fixture '
                             'names (tests drive a synthetic mini suite '
                             'here)')
    parser.add_argument('--thresholds', type=Path,
                        default=thresholds.THRESHOLDS,
                        help=argparse.SUPPRESS)
    parser.add_argument('--summary', type=Path, metavar='FILE',
                        help='write the step-summary markdown here')
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        if args.write is not None:
            _reexec_with_deterministic_hash_seed(sys.argv[1:])
            fixture = read_fixture(args.fixture, args.repo_root)
            measurement = measure(fixture, args.repo_root)
            suite_report.write_measurement(args.write, measurement)
            print(suite_report.report(measurement))
            if args.summary is not None:
                args.summary.write_text(
                    suite_report.summary_markdown(measurement),
                    encoding='utf-8')
            return 0
        code = suite_report.check(args.check, args.thresholds)
        if args.summary is not None:
            measurement = suite_report.measurement_from_file(args.check)
            args.summary.write_text(
                suite_report.summary_markdown(
                    measurement,
                    verdict=('**within budget**' if code == 0
                             else '**OVER BUDGET**')),
                encoding='utf-8')
        return code
    except (OSError, ValueError) as main_error:
        print(str(main_error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
