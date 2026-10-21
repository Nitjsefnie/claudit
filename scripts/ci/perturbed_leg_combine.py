#!/usr/bin/env python3
"""Combine perturbed-suite shard telemetry into per-seed costs and verdicts."""
from __future__ import annotations

import argparse
from decimal import Decimal
import json
from pathlib import Path
import sys

PHASES = ("perturb", "suite")


def _sum(records: list[dict], column: str) -> str:
    values = [record[column] for record in records]
    if not records or "-" in values:
        return "-"
    return f"{sum((Decimal(value) for value in values), Decimal(0)):.1f}"


def combine(documents: list[dict], seeds: list[int], count: int) -> dict:
    """Sum measured shard work; missing shards make their seed incomplete."""
    by_seed: dict[int, dict[int, dict]] = {seed: {} for seed in seeds}
    workloads = set()
    for doc in documents:
        shard = doc["shard"]
        if (doc["instrument"] != "wall_and_cpu" or doc["of"] != count
                or not 1 <= shard <= count or len(doc["seeds"]) != 1):
            raise ValueError("inconsistent shard measurement")
        run, = doc["seeds"]
        seed = run["seed"]
        if seed not in by_seed or shard in by_seed[seed]:
            raise ValueError(f"unexpected or duplicate seed/shard: {seed}/{shard}")
        by_seed[seed][shard] = run["phases"]
        workloads.add(doc["tests_tree_lines"])
    if len(workloads) > 1:
        raise ValueError("shards measured different tests_tree_lines")
    runs = []
    for seed, shards in by_seed.items():
        missing = sorted(set(range(1, count + 1)) - shards.keys())
        phases: dict[str, dict] = {}
        for phase in PHASES:
            records = [phases[phase] for _, phases in sorted(shards.items())]
            phases[phase] = {
                column: _sum(records, column) for column in ("wall_s", "cpu_s")}
            phases[phase]["exit_code"] = next(
                (record["exit_code"] for record in records if record["exit_code"]),
                1 if missing else 0)
        runs.append({"seed": seed, "phases": phases, "shards": shards,
                     "missing": missing,
                     "exit_code": next((phases[p]["exit_code"] for p in PHASES
                                        if phases[p]["exit_code"]), 0)})
    return {"seeds": runs, "of": count,
            "tests_tree_lines": next(iter(workloads), "-")}


def summary_markdown(measurement: dict) -> str:
    """Show seed verdicts and phase sums alongside every shard's readings."""
    lines = ["### Perturbed leg attribution — per seed", "",
             "Wall totals are the sum of shard wall times, not elapsed run time.",
             "Missing shards make totals partial and the seed verdict FAIL.", "",
             "| seed | verdict |", "| --- | --- |"]
    for run in measurement["seeds"]:
        lines.append(f"| {run['seed']} | {'FAIL' if run['exit_code'] else 'PASS'} |")
    for run in measurement["seeds"]:
        if run["missing"]:
            lines.append(f"\nSeed {run['seed']}: missing shards {run['missing']}.\n")
    lines += ["", "| seed | phase | wall_s | cpu_s | exit_code |",
              "| --- | --- | --- | --- | --- |"]
    for run in measurement["seeds"]:
        for phase, record in run["phases"].items():
            lines.append(f"| {run['seed']} | {phase} | {record['wall_s']} | "
                         f"{record['cpu_s']} | {record['exit_code']} |")
    lines += ["", "#### Shard breakdown", "",
              "| seed | shard | phase | wall_s | cpu_s | exit_code |",
              "| --- | --- | --- | --- | --- | --- |"]
    for run in measurement["seeds"]:
        for shard, phases in sorted(run["shards"].items()):
            for phase, record in phases.items():
                lines.append(f"| {run['seed']} | {shard} of {measurement['of']} | {phase} | "
                             f"{record['wall_s']} | {record['cpu_s']} | {record['exit_code']} |")
    lines += ["", f"tests_tree_lines: {measurement['tests_tree_lines']}", "",
              "Instrument: `wall_and_cpu` — telemetry: the leg's verdict is the "
              "perturbed suite's pass/fail, never its duration.",
              "Container startup and dependency install are outside this window."]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Read downloaded measurement artifacts, write a summary, and retain failures."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--measurements", type=Path, required=True)
    parser.add_argument("--seed", type=int, action="append", required=True)
    parser.add_argument("--of", type=int, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.of < 1 or len(set(args.seed)) != len(args.seed):
        parser.error("expected a positive shard count and distinct seeds")
    try:
        documents = [json.loads(path.read_text(encoding="utf-8"))
                     for path in sorted(args.measurements.rglob("*.json"))]
        measurement = combine(documents, args.seed, args.of)
        text = summary_markdown(measurement)
        args.summary.write_text(text, encoding="utf-8")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    print(text, end="")
    return int(any(run["exit_code"] for run in measurement["seeds"]))


if __name__ == "__main__":
    raise SystemExit(main())
