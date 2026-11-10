#!/usr/bin/env python3
"""Partition tests/ by line count or recorded portable test durations.

An --os the weight table lacks is split by line count, with a warning on stderr.

Regenerate weights from a portable CI run:
  gh run download 41846196618 -p 'pytest-junit-*-shard-*' -D /tmp/pytest-junit-41846196618
  python scripts/ci/pytest_shards.py --record /tmp/pytest-junit-41846196618 --run-id 41846196618 --output scripts/ci/pytest_shard_weights.json
"""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from xml.etree import ElementTree


def partition(files: list[tuple[str, int]], shards: int) -> list[list[tuple[str, int]]]:
    """Place largest files first, breaking equal loads by shard index."""
    groups: list[list[tuple[str, int]]] = [[] for _ in range(shards)]
    loads = [0] * shards
    for path, lines in sorted(files, key=lambda item: (-item[1], item[0])):
        index = min(range(shards), key=lambda index: loads[index])
        groups[index].append((path, lines))
        loads[index] += lines
    for group in groups:
        group.sort(key=lambda item: item[0])
    return groups


def discover(root: Path = Path(".")) -> list[tuple[str, int]]:
    """Find test files and line counts, with paths relative to the tree."""
    files = []
    for path in sorted((root / "tests").rglob("test_*.py")):
        if path.is_file():
            with path.open(encoding="utf-8") as stream:
                files.append((path.relative_to(root).as_posix(), sum(1 for _ in stream)))
    return files


def weighted(files: list[tuple[str, int]], weights: dict[str, int]) -> list[tuple[str, int]]:
    """Apply measured weights, scaling new files from measured current files."""
    known = [(lines, weights[path]) for path, lines in files if weights.get(path, 0) > 0]
    known_lines = sum(lines for lines, _ in known)
    if not known_lines and any(path not in weights for path, _ in files):
        raise ValueError("cannot scale new files without positive weights for current files")
    total = sum(weight for _, weight in known)
    return [(path, (weights[path] or 200) if path in weights
             else round(Fraction(lines * total, known_lines))) for path, lines in files]


def artifact_os(name: str) -> str:
    """Extract the OS from a portable pytest JUnit artifact directory."""
    prefix = "pytest-junit-"
    cell, sep, shard = name.removeprefix(prefix).rpartition("-shard-")
    os_name, divider, version = cell.rpartition("-")
    parts = version.split(".")
    valid_version = len(parts) >= 2 and all(part.isdecimal() for part in parts)
    valid_shard = shard.isdecimal() and int(shard) > 0
    if not all((name.startswith(prefix), sep, divider, os_name, valid_version, valid_shard)):
        raise ValueError(f"unparseable report directory: {name}")
    return os_name


def record(reports: Path, files: list[tuple[str, int]], run_id: str) -> dict:
    """Aggregate JUnit testcase times by OS and discovered test file."""
    modules = {path[:-3].replace("/", "."): path for path, _ in files}
    durations: dict[str, dict[str, Decimal]] = {}
    directories = sorted(path for path in reports.iterdir() if path.is_dir())
    if not directories:
        raise ValueError(f"no report directories in {reports}")
    for directory in directories:
        os_name = artifact_os(directory.name)
        totals = durations.setdefault(os_name, {path: Decimal(0) for path, _ in files})
        for case in ElementTree.parse(directory / "pytest-junit.xml").iter("testcase"):
            classname = case.get("classname", "")
            module = classname
            while module and module not in modules:
                module = module.rpartition(".")[0]
            if not module:
                raise ValueError(f"unmappable testcase classname: {classname}")
            totals[modules[module]] += Decimal(case.get("time", "0")) * 1000
    return {"run_id": run_id, "weights": {
        os_name: {path: round(value) for path, value in totals.items()}
        for os_name, totals in durations.items()}}


def write_record(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Record a downloaded run."""
    if not args.run_id or args.output is None:
        parser.error("--record requires --run-id and --output")
    try:
        result = record(args.record, discover(), args.run_id)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8", newline="\n")


def load_weighted(parser: argparse.ArgumentParser, files: list[tuple[str, int]],
                  table: Path, os_name: str | None) -> list[tuple[str, int]] | None:
    """Apply the selected OS weights; None when the table lacks that OS."""
    weights = json.loads(table.read_text(encoding="utf-8"))["weights"]
    if os_name not in weights:
        if not isinstance(weights, dict):
            parser.error(f"no weights for OS {os_name}; regenerate the table")
        print(f"warning: no weights for OS {os_name}; splitting by line count",
              file=sys.stderr)
        return None
    try:
        result = weighted(files, weights[os_name])
    except ValueError as exc:
        parser.error(str(exc))
    return result


def main() -> None:
    """Print shard assignments or record portable test durations."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard", type=int, help="one-based shard number")
    parser.add_argument("--of", type=int, default=4, help="total shards (default: 4)")
    parser.add_argument("--table", action="store_true", help="print all shard totals")
    parser.add_argument("--weights", type=Path, help="JSON file of recorded weights")
    parser.add_argument("--os", help="OS key in the weight table; an unrecorded OS "
                        "is split by line count")
    parser.add_argument("--record", type=Path, help="directory of downloaded JUnit artifacts")
    parser.add_argument("--run-id", help="source run id for a recording")
    parser.add_argument("--output", type=Path, help="output JSON for a recording")
    args = parser.parse_args()
    if (args.weights is None) != (args.os is None):
        parser.error("--weights and --os must be used together")
    if args.record is not None:
        write_record(parser, args)
        return
    if args.run_id or args.output:
        parser.error("--run-id and --output require --record")
    if args.of < 1:
        parser.error("--of must be at least 1")
    if args.shard is not None and not 1 <= args.shard <= args.of:
        parser.error("--shard must be between 1 and --of")
    if not args.table and args.shard is None:
        parser.error("--shard is required unless --table is used")
    files = discover()
    if not files:
        parser.error("tests/ contains no test_*.py files")
    measured = None
    if args.weights is not None:
        measured = load_weighted(parser, files, args.weights, args.os)
    groups = partition(measured or files, args.of)
    if args.table:
        for index, group in enumerate(groups, start=1):
            lines = sum(lines for _, lines in group)
            unit = "weight" if measured else "lines"
            print(f"shard {index}: {len(group)} files, {lines} {unit}")
    elif args.shard is not None:
        print(" ".join(path for path, _ in groups[args.shard - 1]))


if __name__ == "__main__":
    main()
