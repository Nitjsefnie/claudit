#!/usr/bin/env python3
"""Attribute the perturbed-data CI leg's cost, phase by phase (issue #456).

The perturbed-data leg ran for ~16-22 minutes per ci-gate run with
nothing recording where the time went. This bench is the leg's
instrument: it keeps the pristine pricing document and constants module
in memory, and per fixed seed it restores them byte for byte, runs the
perturbation in-process (phase ``perturb``), then runs the suite as the
leg runs it — a pytest subprocess over the caller's arguments, on the
just-perturbed tree (phase ``suite``). Each phase records WALL seconds
and CPU seconds (the perturb phase's own ``process_time``; the suite
phase's child CPU from ``getrusage(RUSAGE_CHILDREN)`` deltas) plus the
phase's exit code. The first nonzero suite exit stops the run and is
the bench's own exit — the loop's first failure fails the step, exactly
as the leg always has.

Per the v9 measurement doctrine this is TELEMETRY, never a gate: the
leg's verdict is the perturbed suite's pass/fail, never its duration,
and nothing here is compared against a committed budget. The wall
numbers are diagnostic — the size decision they feed (issue #456) is
about CI minutes, and a sub-second phase is the only honest witness to
the ~21 minutes beside it. Container startup and dependency install
live outside this bench's window: GitHub records those per step, and
the bench names that in its report instead of pretending to measure
what it does not run.

The measurement records the workload identity ``tests_tree_lines``
(the tests/*.py line total, the same #524 identity the suite-cost bench
binds its budgets to) beside the phases, so a later reading of the
summary names the workload the numbers describe.

The seeds are the caller's policy — the leg's three fixed seeds
(issue #226) are spelled in the workflow, not here. A run's
per-seed phases are also written as JSON with ``--measurement``.

    python3 scripts/ci/perturbed_leg_bench.py --seed 42 [--seed S ...]
        [--pricing PATH] [--constants PATH] [--pytest-args ARGS]
        [--summary FILE] [--measurement FILE]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import NamedTuple

try:
    import resource
except ImportError:  # windows: no rusage, the child-CPU columns read '-'
    resource = None

SCRIPTS = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS.parents[1]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
# pylint: disable=wrong-import-position
import perturb_test_data  # noqa: E402

PHASES = ("perturb", "suite")
INSTRUMENT = "wall_and_cpu"
QUANTUM = Decimal("0.1")
TELEMETRY_NOTE = (
    "telemetry: the leg's verdict is the perturbed suite's pass/fail, "
    "never its duration")
DEFAULT_PRICING = REPO_ROOT / "src" / "pricing.json"
DEFAULT_CONSTANTS = REPO_ROOT / "backend" / "constants.py"
# The leg's exact suite invocation, as the workflow ran it before this
# bench owned the loop.
DEFAULT_PYTEST_ARGS = ["tests/", "-q", "--tb=short", "-ra"]


class SeedRun(NamedTuple):
    """One seed's phases: per-phase wall/cpu seconds (one-decimal
    strings) and exit code."""
    seed: int
    phases: dict


def run_seed(pristine_pricing: bytes, pristine_constants: bytes,
             pricing_path: Path, constants_path: Path, seed: int,
             pytest_args: list[str]) -> SeedRun:
    """Restore the tree, perturb, run the suite; time both phases."""
    pricing_path.write_bytes(pristine_pricing)
    constants_path.write_bytes(pristine_constants)
    cpu_start = time.process_time()
    wall_start = time.perf_counter()
    rows, _seed, stamp_base = perturb_test_data.perturb_pricing(
        pricing_path, seed=seed)
    perturb_test_data.bump_constants(constants_path)
    perturb = {
        "wall_s": _fmt(time.perf_counter() - wall_start),
        "cpu_s": _fmt(time.process_time() - cpu_start),
        "exit_code": 0,
    }
    print(f"perturbed {rows} rate rows (5 entries per row), "
          f"seed={seed}, stamp base={stamp_base}")
    return SeedRun(seed, {"perturb": perturb,
                          "suite": _suite_phase(pytest_args)})


def _suite_phase(pytest_args: list[str]) -> dict:
    """Run the suite as the leg always has: a pytest subprocess, env
    inherited, cwd the repo root; wall and child-CPU seconds recorded,
    the CPU '-' where the platform cannot measure it."""
    child_start = _children_cpu()
    wall_start = time.perf_counter()
    # The tree under test is deliberately not the deployed data: tests
    # that pin the DEPLOYED document itself (a byte ceiling) would fire
    # on the inflation that is the leg's point, so the child is told the
    # data is perturbed and may scope its data pins to the deployed tree.
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *pytest_args],
        cwd=str(REPO_ROOT), check=False,
        env=dict(os.environ, CLAUDIT_TEST_PERTURBED_DATA="1"))
    child_cpu = _children_cpu()
    return {
        "wall_s": _fmt(time.perf_counter() - wall_start),
        "cpu_s": ("-" if child_start is None or child_cpu is None
                  else _fmt(child_cpu - child_start)),
        "exit_code": int(proc.returncode),
    }


def measure(seeds: list[int], pricing_path: Path, constants_path: Path,
            pytest_args: list[str], repo_root: Path = REPO_ROOT) -> dict:
    """Run the seeded loop; the measurement document or a raised error.

    Stops at the first nonzero suite exit — later seeds do not run,
    matching the leg's shell loop.
    """
    pristine_pricing = pricing_path.read_bytes()
    pristine_constants = constants_path.read_bytes()
    seed_runs = []
    for seed in seeds:
        run = run_seed(pristine_pricing, pristine_constants,
                       pricing_path, constants_path, seed, pytest_args)
        seed_runs.append({"seed": run.seed, "phases": dict(run.phases)})
        if run.phases["suite"]["exit_code"] != 0:
            break
    return {
        "instrument": INSTRUMENT,
        "note": TELEMETRY_NOTE,
        "pytest_args": pytest_args,
        "tests_tree_lines": _tests_tree_lines(repo_root),
        "seeds": seed_runs,
        "totals": _totals(seed_runs),
    }


def _totals(seed_runs: list[dict]) -> dict:
    """Per-phase sums over the seeds that ran, one-decimal strings.

    A '-' reading (a column the platform cannot measure) sums to '-':
    an unmeasured total never reads as a measured zero.
    """
    totals = {}
    for phase in PHASES:
        totals[phase] = {
            column: _sum_column(seed_runs, phase, column)
            for column in ("wall_s", "cpu_s")
        }
    return totals


def _sum_column(seed_runs: list[dict], phase: str, column: str) -> str:
    values = [run["phases"][phase][column] for run in seed_runs]
    if any(value == "-" for value in values):
        return "-"
    return _fmt(sum(Decimal(value) for value in values))


def _children_cpu() -> Decimal | None:
    """Cumulative CPU seconds of reaped children, or None when the
    platform has no rusage (windows: the column reads '-')."""
    if resource is None:
        return None
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return Decimal(repr(usage.ru_utime + usage.ru_stime))


def _fmt(seconds: Decimal | float) -> str:
    """Seconds as a one-decimal string, the measurement convention."""
    value = (seconds if isinstance(seconds, Decimal)
             else Decimal(repr(seconds)))
    return value.quantize(QUANTUM).to_eng_string()


def _tests_tree_lines(repo_root: Path) -> int:
    """The scanned tree's size: the workload identity the numbers
    describe (the #524 identity, the same reading suite_bench binds its
    budgets to). A tree with no tests/ records an honest 0."""
    root = repo_root / "tests"
    if not root.is_dir():
        return 0
    return sum(len(path.read_bytes().splitlines())
               for path in sorted(root.rglob("*.py")))


def report(measurement: dict) -> str:
    """The human report: per-seed phases, totals, the instrument named."""
    lines = [
        f"perturbed leg attribution over {len(measurement['seeds'])} "
        f"seed(s), pytest args: "
        f"{' '.join(measurement['pytest_args'])}:",
        f"  {'seed':<12} {'phase':<10} {'wall_s':>10} {'cpu_s':>10} "
        f"{'exit_code':>10}",
    ]
    for run in measurement["seeds"]:
        for phase in PHASES:
            record = run["phases"][phase]
            lines.append(
                f"  {run['seed']:<12} {phase:<10} "
                f"{record['wall_s']:>10} {record['cpu_s']:>10} "
                f"{record['exit_code']:>10}")
    lines.append(f"  total perturb: wall "
                 f"{measurement['totals']['perturb']['wall_s']}s, "
                 f"cpu {measurement['totals']['perturb']['cpu_s']}s")
    lines.append(f"  total suite: wall "
                 f"{measurement['totals']['suite']['wall_s']}s, "
                 f"cpu {measurement['totals']['suite']['cpu_s']}s")
    lines.append(f"  tests_tree_lines: {measurement['tests_tree_lines']}")
    lines.append(f"  instrument: {measurement['instrument']} — "
                 f"{TELEMETRY_NOTE}")
    lines.append("  container startup and dependency install are outside "
                 "this window: GitHub records them per step")
    return "\n".join(lines)


def summary_markdown(measurement: dict) -> str:
    """The step-summary markdown: the per-seed phase table and totals."""
    lines = [
        "### Perturbed leg attribution", "",
        "| seed | phase | wall_s | cpu_s | exit_code |",
        "| --- | --- | --- | --- | --- |",
    ]
    for run in measurement["seeds"]:
        for phase in PHASES:
            record = run["phases"][phase]
            lines.append(
                f"| {run['seed']} | {phase} | {record['wall_s']} | "
                f"{record['cpu_s']} | {record['exit_code']} |")
    lines.append(f"| total | perturb | "
                 f"{measurement['totals']['perturb']['wall_s']} | "
                 f"{measurement['totals']['perturb']['cpu_s']} | - |")
    lines.append(f"| total | suite | "
                 f"{measurement['totals']['suite']['wall_s']} | "
                 f"{measurement['totals']['suite']['cpu_s']} | - |")
    lines += ["", f"Instrument: `{measurement['instrument']}` — "
              f"{TELEMETRY_NOTE}.",
              "Container startup and dependency install are outside this "
              "window: GitHub records them per step.",
              "", f"tests_tree_lines: {measurement['tests_tree_lines']}"]
    return "\n".join(lines) + "\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Attribute the perturbed-data leg's cost, phase by "
                    "phase: per fixed seed, the perturbation build and "
                    "the suite run, wall + CPU seconds, telemetry only.")
    parser.add_argument("--seed", type=int, action="append", required=True,
                        help="a run seed; repeat for every seed the leg "
                             "runs (the fixed seeds are the caller's "
                             "policy, issue #226)")
    parser.add_argument("--pricing", type=Path, default=DEFAULT_PRICING,
                        help="path to the pricing document (default: "
                             "src/pricing.json)")
    parser.add_argument("--constants", type=Path, default=DEFAULT_CONSTANTS,
                        help="path to the constants module (default: "
                             "backend/constants.py)")
    parser.add_argument("--pytest-args", default=None,
                        help="the suite invocation after '-m pytest' "
                             "(default: the leg's own arguments)")
    parser.add_argument("--summary", type=Path, metavar="FILE",
                        help="write the step-summary markdown here")
    parser.add_argument("--measurement", type=Path, metavar="FILE",
                        help="write the measurement JSON here")
    return parser


def main(argv: list[str] | None = None) -> int:
    """The CI leg's entry point; the first failing suite exit is the run's."""
    args = _parser().parse_args(argv)
    pytest_args = (args.pytest_args.split()
                   if args.pytest_args is not None
                   else list(DEFAULT_PYTEST_ARGS))
    try:
        measurement = measure(args.seed, args.pricing, args.constants,
                              pytest_args)
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(report(measurement))
    if args.summary is not None:
        args.summary.write_text(summary_markdown(measurement),
                                encoding="utf-8")
    if args.measurement is not None:
        args.measurement.write_text(
            json.dumps(measurement, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
    for run in measurement["seeds"]:
        code = run["phases"]["suite"]["exit_code"]
        if code:
            return code
    return 0


if __name__ == "__main__":
    sys.exit(main())
