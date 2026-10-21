#!/usr/bin/env python3
"""Partition tests/ by line count for deterministic, balanced pytest shards."""
from __future__ import annotations

import argparse
from pathlib import Path


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


def main() -> None:
    """Print one shard's paths or every shard's file and line totals."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard", type=int, help="one-based shard number")
    parser.add_argument("--of", type=int, default=4, help="total shards (default: 4)")
    parser.add_argument("--table", action="store_true", help="print all shard totals")
    args = parser.parse_args()
    if args.of < 1:
        parser.error("--of must be at least 1")
    if args.shard is not None and not 1 <= args.shard <= args.of:
        parser.error("--shard must be between 1 and --of")
    if not args.table and args.shard is None:
        parser.error("--shard is required unless --table is used")
    files = discover()
    if not files:
        parser.error("tests/ contains no test_*.py files")
    groups = partition(files, args.of)
    if args.table:
        for index, group in enumerate(groups, start=1):
            lines = sum(lines for _, lines in group)
            print(f"shard {index}: {len(group)} files, {lines} lines")
    elif args.shard is not None:
        print(" ".join(path for path, _ in groups[args.shard - 1]))


if __name__ == "__main__":
    main()
