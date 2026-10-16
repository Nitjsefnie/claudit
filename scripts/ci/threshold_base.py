#!/usr/bin/env python3
"""Select a push threshold base in the repository containing cwd.

Usage: python3 scripts/ci/threshold_base.py <before> <path>
Prefer a usable BEFORE; otherwise use HEAD^. Print only the base SHA on
success, or explain both failures on stderr and exit 3 when neither works.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ['git', '-C', str(repo), *args],
        capture_output=True, text=True, check=False, timeout=30)


def _candidate(repo: Path, ref: str, path: str) -> tuple[str | None, str]:
    try:
        if _git(repo, 'cat-file', '-e', f'{ref}^{{commit}}').returncode:
            return None, 'does not resolve to a commit in this clone'
        if _git(repo, 'cat-file', '-e', f'{ref}:{path}').returncode:
            return None, f'resolves to a commit but does not contain {path!r}'
        resolved = _git(repo, 'rev-parse', '--verify', ref)
        if resolved.returncode:
            return None, 'contains the path but its full SHA could not be resolved'
        return resolved.stdout.strip(), ''
    except (OSError, subprocess.SubprocessError) as error:
        return None, f'could not inspect the candidate with git: {error}'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('before')
    parser.add_argument('path')
    args = parser.parse_args(argv)
    repo = Path.cwd()

    if args.before and any(character != '0' for character in args.before):
        base, before_reason = _candidate(repo, args.before, args.path)
        if base is not None:
            print(base)
            return 0
    else:
        before_reason = 'empty or all zeros; no before commit was supplied'

    base, parent_reason = _candidate(repo, 'HEAD^', args.path)
    if base is not None:
        print(base)
        return 0

    print(f'No threshold base available. BEFORE {args.before!r}: {before_reason}; '
          f'HEAD^: {parent_reason}.', file=sys.stderr)
    return 3


if __name__ == '__main__':
    raise SystemExit(main())
