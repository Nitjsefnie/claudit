#!/usr/bin/env python3
"""Ratchet comment shares at 15% using exact byte pairs.

Only measured rises above an entry or the unrecorded 15% limit fail.
Lower/equal shares and deleted/graduated entries pass on PRs.
Master --tighten lowers pairs and drops deleted or at/below-15% entries.
"""
from __future__ import annotations

import argparse
import importlib
import subprocess
import sys
from pathlib import Path

comment_share = importlib.import_module('scripts.ci.comment_share' if __package__ else 'comment_share')
thresholds = importlib.import_module('scripts.ci.thresholds' if __package__ else 'thresholds')
thresholds_removed = importlib.import_module(
    'scripts.ci.thresholds_removed' if __package__ else 'thresholds_removed')
TARGET = (15, 100)
Pair = tuple[int, int] | list[int]


def above(current: Pair, limit: Pair) -> bool:
    return current[0] * limit[1] > limit[0] * current[1]


def entries(measured: dict[str, tuple[int, int]]) -> dict[str, list[int]]:
    return {path: list(counts) for path, counts in measured.items()
            if above(counts, TARGET)}


def violations(measured: dict[str, tuple[int, int]],
               baseline: dict[str, list[int]],
               declared_removed: set[str] | frozenset[str] = frozenset()) -> dict[str, str]:
    failures = {}
    for path, counts in measured.items():
        if path not in baseline and path in declared_removed:
            continue
        if above(counts, baseline.get(path, TARGET)):
            failures[path] = f'{counts} exceeds {baseline.get(path, TARGET)}'
    return failures


def tightened(baseline: dict[str, list[int]],
              measured: dict[str, tuple[int, int]]) -> dict[str, list[int]]:
    updated = {}
    for path, recorded in baseline.items():
        current = measured.get(path)
        if current is not None and above(current, TARGET):
            updated[path] = recorded if above(current, recorded) else list(current)
    updated.update({path: pair for path, pair in entries(measured).items()
                    if path not in baseline})
    return updated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--tighten', action='store_true')
    modes.add_argument('--seed', action='store_true', help='introduce an absent section once')
    parser.add_argument('--root', type=Path, default=comment_share.ROOT)
    parser.add_argument('--thresholds', type=Path, default=thresholds.THRESHOLDS)
    parser.add_argument('--base-rev', help='exclusive base of the judged commit range')
    parser.add_argument('--head-rev', default='HEAD',
                        help='inclusive head of the judged commit range (default HEAD)')
    args = parser.parse_args(argv)
    try:
        if args.seed:
            data = thresholds.validate.decode(args.thresholds.read_bytes())
            if 'comment_share_baseline' in data:
                raise ValueError('comment_share_baseline already exists; use --tighten')
            data['comment_share_baseline'] = {}
            data = thresholds.normalise(data)
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
        removed = thresholds_removed.declared(args.base_rev, args.head_rev, args.root)
        exempt = removed.paths('comment_share_baseline', measured) - baseline.keys()
        for path in sorted(exempt):
            if above(measured[path], TARGET):
                print(f'declared-removed: {path} ({measured[path]} above '
                      'the default until master seeds)')
        found = violations(measured, baseline, exempt)
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
