#!/usr/bin/env python3
"""Ratchet exact [comment bytes, non-whitespace bytes] shares at 15%."""
from __future__ import annotations

import argparse
import importlib
import subprocess
import sys
from pathlib import Path

comment_share = importlib.import_module('scripts.ci.comment_share' if __package__ else 'comment_share')
thresholds = importlib.import_module('scripts.ci.thresholds' if __package__ else 'thresholds')
TARGET = (15, 100)
Pair = tuple[int, int] | list[int]


def above(current: Pair, limit: Pair) -> bool:
    return current[0] * limit[1] > limit[0] * current[1]


def entries(measured: dict[str, tuple[int, int]]) -> dict[str, list[int]]:
    return {path: list(counts) for path, counts in measured.items()
            if above(counts, TARGET)}


def violations(measured: dict[str, tuple[int, int]],
               baseline: dict[str, list[int]]) -> dict[str, str]:
    failures = {}
    for path, counts in measured.items():
        if above(counts, baseline.get(path, TARGET)):
            failures[path] = f'{counts} exceeds {baseline.get(path, TARGET)}'
    for path in baseline:
        if path not in measured:
            failures[path] = 'entry names an unmeasured file'
        elif not above(measured[path], TARGET):
            failures[path] = 'at or below 15%; drop entry with --tighten'
    return failures


def tightened(baseline: dict[str, list[int]],
              measured: dict[str, tuple[int, int]]) -> dict[str, list[int]]:
    updated = {}
    for path, recorded in baseline.items():
        current = measured.get(path)
        if current is not None and above(current, TARGET):
            updated[path] = recorded if above(current, recorded) else list(current)
    return updated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--tighten', action='store_true')
    modes.add_argument('--seed', action='store_true', help='introduce an absent section once')
    modes.add_argument('--matches-tree', action='store_true', help='also refuse stale entries')
    parser.add_argument('--root', type=Path, default=comment_share.ROOT)
    parser.add_argument('--thresholds', type=Path, default=thresholds.THRESHOLDS)
    args = parser.parse_args(argv)
    try:
        if args.seed:
            data = thresholds.validate.decode(args.thresholds.read_bytes())
            if 'comment_share_baseline' in data:
                raise ValueError('comment_share_baseline already exists; use --tighten')
            data['comment_share_baseline'] = {}
            data = thresholds.normalise(data, thresholds.verdict())
        else:
            data = thresholds.load(args.thresholds)
        baseline = thresholds.comment_share_baseline(data)
        measured = comment_share.measure_files(args.root)
        if args.seed or args.tighten:
            updated = entries(measured) if args.seed else tightened(baseline, measured)
            if args.seed or updated != baseline:
                data['comment_share_baseline'] = updated
                thresholds.write(args.thresholds, data)
            print(f'{len(updated)} comment-share entries recorded')
            return 0
        found = violations(measured, baseline)
        if args.matches_tree and baseline != entries(measured):
            found['matches-tree'] = 'baseline differs from measured byte pairs; run --tighten'
        for path, detail in found.items():
            print(f'{path}: {detail}', file=sys.stderr)
        if found:
            return 1
        print(f'{len(measured)} tracked files within the comment-share policy')
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f'comment-share check failed: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
