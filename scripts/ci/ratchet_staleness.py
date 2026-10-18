#!/usr/bin/env python3
"""Decide whether every commit after the measured base changes only data."""
from __future__ import annotations

import argparse
import importlib
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
changes = importlib.import_module('scripts.ci.classify_changes')
bot = importlib.import_module('scripts.ci.classify_bot')
sources = importlib.import_module('scripts.ci.classify_sources')
thresholds = importlib.import_module('scripts.ci.classify_thresholds')


def _git(*args: str) -> bytes:
    return subprocess.run(['git', *args], check=True, capture_output=True).stdout


def _non_data_reason(sha: str) -> str | None:
    paths = _git('diff-tree', '--no-commit-id', '--name-only', '--no-renames',
                 '-r', sha).decode('utf-8').splitlines()
    email = _git('show', '-s', '--format=%ae', sha).decode('utf-8').strip()
    if thresholds.thresholds_only(paths):
        return None
    if not changes.data_only(paths):
        return 'paths are neither thresholds-only nor a pricing-bot shape'
    if email != bot.BOT_EMAIL:
        return 'pricing data was not authored by the bot'
    before = _git('show', f'{sha}~:backend/constants.py')
    after = _git('show', f'{sha}:backend/constants.py')
    if not sources.numeric_assignment_only(before, after):
        return 'constants change is not a same-width numeric pricing-version edit'
    return None


def classify(base: str, tip: str) -> tuple[bool, str]:
    """Fail closed on ancestry, history, path or blob reads we cannot verify."""
    try:
        _git('merge-base', '--is-ancestor', base, tip)
        commits = _git('rev-list', '--reverse', f'{base}..{tip}').decode('ascii').splitlines()
    except (OSError, subprocess.CalledProcessError, UnicodeError):
        return False, f'{tip}: cannot verify ancestry or history from {base}'
    for sha in commits:
        try:
            reason = _non_data_reason(sha)
        except (OSError, subprocess.CalledProcessError, UnicodeError):
            return False, f'{sha}: cannot read commit paths, author or constants blobs'
        if reason:
            return False, f'{sha}: {reason}'
    return True, f'{base}..{tip}: every intervening commit is data-only'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', required=True)
    parser.add_argument('--tip', required=True)
    args = parser.parse_args(argv)
    safe, reason = classify(args.base, args.tip)
    print(reason)
    return 0 if safe else 1


if __name__ == '__main__':
    raise SystemExit(main())
