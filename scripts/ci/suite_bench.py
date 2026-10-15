#!/usr/bin/env python3
"""Measure one pytest run's cost in instructions, phase by phase.

The suite-cost gate uses instruction counts, partitioned into collection,
run and residual. ``sys.monitoring``'s INSTRUCTION event counts retired
bytecodes. A count is exact for one tree, interpreter and library set;
session-level work outside the wrapped phases remains environment-sensitive.

The bench establishes its measuring state: the pinned fixture
``suite_bench_files.txt``, a collect-only bytecode-cache warm-up before
the counted window, and ``PYTHONHASHSEED=0`` by re-exec. Comparable local
readings require a venv holding exactly ``backend/requirements.txt`` and
``requirements-test.txt``; extra pytest plugins change the workload.
The collection / run / residual partition is complete, and
``suite_phases.partition`` refuses a negative residual.

Tree-scanning tests spend per-line over ``tests/``. Each measurement
records ``tests_tree_lines`` (the recursive tests/*.py line total),
which supplies L for the growth allowance. Committed phase budgets in
``.github/ci-thresholds.json`` carry measured and floor = measured + 1.5
at a seed identity L_seed, plus optional per_line instructions per line.
The gate ceiling at L is
``floor + per_line * max(0, L - L_seed) / 1e6`` million instructions;
absent identities or absent/zero per_line apply no growth allowance.
Ordinary growth at the seeded slope is budgeted. The 1.5M gap covers
interpreter and library drift.

``suite_ratchet.py --seed m1 --growth m2`` requires TWO runner
measurements: m1 of the final tree the seed lands on, and m2 of the
same code plus a larger probe tree. Both must carry gateable counts,
``PYTHONHASHSEED=0`` and positive identities, with L2 > L1. The seed
records m1's counts and identity and derives each non-negative integer
per_line slope by rounding the instruction difference per added line upward
and clamping at zero; convert million counts to instructions first.
The tighten bot subtracts budgeted growth before comparing with the
recorded measured value, requires a saving greater than 1.5M, rounds
qualifying targets upward to one decimal, and preserves slope and identity.
Smaller-tree artifacts never tighten; negative normalized phases skip
with a seed-candidate diagnostic. Changed fixture lists, interpreter
pins, executed code or per-line slopes use the sanctioned re-seed path.

Wall time does not gate. ``time.process_time()`` supplies portable
telemetry when ``sys.monitoring`` is unavailable, marked ``process_time``
in the measurement. ``--check`` fails closed for counts-less measurements
and measurements not taken under ``PYTHONHASHSEED=0``.

  python3 scripts/ci/suite_bench.py --write m.json   # measure + write
  python3 scripts/ci/suite_bench.py --check m.json   # gate it
"""
from __future__ import annotations

import argparse
import contextlib
import importlib
import os
import platform
import shutil
import subprocess
import sys
import tempfile
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
# Set on the re-exec'd child: the parent has already drawn the
# compile/load split outside the counted window, so the child's own
# warm-up pass would only repeat it.
WARMED_ENV = 'CLAUDIT_SUITE_BENCH_WARMED'
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
    root = Path(repo_root).resolve()
    for number, line in enumerate(
            Path(path).read_text(encoding='utf-8').splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        target = (root / stripped).resolve()
        if not target.is_file():
            raise ValueError(
                f'{path}:{number}: {stripped} is not a file under '
                f'{repo_root}')
        try:
            target.relative_to(root)
        except ValueError:
            raise ValueError(
                f'{path}:{number}: {stripped} resolves outside '
                f'{repo_root}') from None
        entries.append(target)
    if not entries:
        raise ValueError(f'{path}: the fixture list is empty')
    return entries


def warm_bytecode_caches(fixture: list[Path], repo_root: Path) -> None:
    """Compile the tree's own modules once, before any count is taken.

    A count must not depend on what the checkout arrived with. The
    interpreter compiles a module the first time it imports it and
    loads the cached bytecode after that, and the compile costs about
    as much again as the load: the same tree measured 29.1M
    instructions at collection with no caches present and 14.2M with
    them. Before #499 the two call sites did not agree on that -- the
    leg's bench measured a fresh checkout while the canary's measured
    after the suite had run in the same working tree -- so the gate
    read one number and the tighten bot wrote the other into the
    committed budgets, leaving a ceiling no run could satisfy
    (issue #496).

    So the bench establishes the state rather than inheriting it: one
    collect-only pass compiles the fixture's modules, and pytest writes
    the assertion-rewritten bytecode for the test modules itself, all
    outside every counted window. Compiling is what this pass is FOR;
    it is not a warm-up in the statistical sense, and nothing it
    measures is counted. ``PYTHONDONTWRITEBYTECODE`` is dropped for
    this child alone -- the counting child keeps it, so the
    measurement itself still writes nothing into the checkout.
    """
    env = dict(os.environ)
    env.pop('PYTHONDONTWRITEBYTECODE', None)
    args = [
        sys.executable, '-m', 'pytest',
        *[str(path) for path in fixture],
        '--collect-only', '-q', '--no-header', '--tb=no',
        '-p', 'no:cacheprovider',
    ]
    # A non-zero exit is deliberately not raised here: this pass is
    # preparation, and the counted run that follows reports an unusable
    # fixture in its own words, naming the exit code it saw. Both passes
    # collect the same files with the same interpreter, so a fixture
    # this one cannot collect, the counted run cannot either.
    # pylint: disable-next=subprocess-run-check
    subprocess.run(
        args, cwd=str(repo_root), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        check=False)


def _tests_tree_lines(repo_root: Path) -> int:
    """The scanned tree's size L, used against the budget's seed identity.

    The fixture's tree-scanning tests (the db-marker derivation, the
    version-literal guard, the scratch-DB scan) walk ``tests/``
    recursively, and their cost is per-line over that set. Budgets allow
    per_line * max(0, L - L_seed) additional instructions for this growth;
    the gate converts that allowance to millions. A tree with no
    ``tests/`` has no scanned workload and records an honest 0.
    """
    root = repo_root / 'tests'
    if not root.is_dir():
        return 0
    return sum(len(path.read_bytes().splitlines())
               for path in sorted(root.rglob('*.py')))


def measure(fixture: list[Path], repo_root: Path) -> Measurement:
    """One pass over the fixture, partitioned, counted, reported.

    Runs pytest in-process (``pytest.main``) with the phase plugin; the
    instruction counter counts the whole region, the plugin's windows
    attribute collection and run, and the residual is the divergence.
    """
    counter = suite_phases.InstructionCounter()
    # Captured at entry: close() (the ExitStack's cleanup) marks the
    # counter closed, and the counts read afterwards must not mistake
    # our own cleanup for an instrument that was never there.
    counting = counter.available
    sink = suite_phases.ProcessTimeSink()
    plugin = suite_phases.SuitePhasePlugin(counter, sink)
    basetemp = tempfile.mkdtemp(prefix='claudit-suite-bench-basetemp-')
    try:
        args = [
            *[str(path) for path in fixture],
            '-q', '--no-header', '--tb=no', '-p', 'no:cacheprovider',
            '--basetemp', basetemp,
        ]
        cpu_start = time.process_time()
        with contextlib.ExitStack() as stack:
            if counting:
                # Registered BEFORE the window: ExitStack unwinds in reverse
                # entry order, so the window closes first (events off) and
                # close() then frees the tool id -- an in-process caller is
                # not left paying the INSTRUCTION tax for the rest of its
                # life, and close() never touches a freed tool.
                stack.callback(counter.close)
                stack.enter_context(counter.window('total'))
            code = pytest.main(args, plugins=[plugin])
        cpu_total = time.process_time() - cpu_start
    finally:
        shutil.rmtree(basetemp, ignore_errors=True)
    if code not in _USABLE:
        raise ValueError(
            f'the pinned fixture did not produce a usable run '
            f'(pytest exited {int(code)})')
    cpu_phases = suite_phases.partition(
        _cpu(cpu_total), _cpu(sink.phases['collection']),
        _cpu(sink.phases['run']),
        tolerance=Decimal('0.001'))
    counts = None
    if counting:
        raw = suite_phases.partition(
            Decimal(counter.total),
            Decimal(counter.phases['collection']),
            Decimal(counter.phases['run']))
        counts = {phase: (raw[phase] / suite_phases.SCALE).quantize(
            _QUANTUM) for phase in suite_phases.PHASES}
    return Measurement(
        instrument=('instruction_count' if counting
                    else 'process_time'),
        hash_seed=('0' if sys.flags.hash_randomization == 0
                   else 'randomized'),
        tests=plugin.tests,
        counts=counts,
        cpu_s={phase: f'{cpu_phases[phase]:.3f}'
               for phase in suite_phases.PHASES},
        fixture=str(FIXTURE),
        interpreter=platform.python_version(),
        tests_tree_lines=_tests_tree_lines(repo_root),
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
    instruction-count wobble. ``warm_bytecode_caches`` has already
    drawn that split OUTSIDE the counted window, so the caches the
    child loads are the same ones on every run whatever the checkout
    arrived with.
    """
    if os.environ.get('PYTHONHASHSEED') == '0':
        os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
        return
    env = dict(os.environ, PYTHONHASHSEED='0', PYTHONDONTWRITEBYTECODE='1',
               # The parent already warmed the tree before handing over;
               # saying so keeps the child from paying for a second
               # collect-only pass it does not need (issue #496).
               **{WARMED_ENV: '1'})
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


def _load_bounded_document() -> None:
    """Load the pricing tables from the bounded bench document.

    The counted run's conftest imports ``backend.pricing``, whose
    import-time parse of ``src/pricing.json`` scaled with the document
    (issue #872: a data-only refresh breached the residual ceiling). The
    bench loads the committed bounded ``suite_pricing_doc.json`` HERE —
    in the counted child, before any counted window opens — and rebinds
    every loaded table on both pricing modules, so the measured workload
    parses the bounded document and never the deployed one. Nothing the
    deployed app reads changes: ``backend/`` gains no override and reads
    no environment (a deploy could otherwise reprice through it); the
    injection lives on the bench side only, and the bench parses the
    deployed document once outside every counted window to import the
    modules it rebinds.
    """
    # pylint: disable-next=import-outside-toplevel
    import json
    # pylint: disable-next=import-outside-toplevel
    from backend import pricing, pricing_load
    doc_path = Path(__file__).resolve().parent / 'suite_pricing_doc.json'
    tables = pricing_load.load_tables(
        json.loads(doc_path.read_text(encoding='utf-8')))
    for name, value in tables.items():
        setattr(pricing_load, name, value)
        if hasattr(pricing, name):
            setattr(pricing, name, value)


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        if args.write is not None:
            # The counted run must not depend on the caches the checkout
            # arrived with, and the parent is the only process outside
            # every counted window: compile the tree here, then hand the
            # measurement to the deterministic child.
            if os.environ.get(WARMED_ENV) != '1':
                warm_bytecode_caches(
                    read_fixture(args.fixture, args.repo_root),
                    args.repo_root)
            _reexec_with_deterministic_hash_seed(sys.argv[1:])
            _load_bounded_document()
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
                    verdict=suite_report.gate_verdict(
                        args.thresholds, code)),
                encoding='utf-8')
        return code
    except (OSError, ValueError) as main_error:
        print(str(main_error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
