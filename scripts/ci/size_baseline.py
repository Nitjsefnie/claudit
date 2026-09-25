#!/usr/bin/env python3
"""Check and tighten the module-size baseline in CI threshold data.

The mutable policy state is the ``module_size_baseline`` member of
``.github/ci-thresholds.json``. A recorded number is never raised by hand
and no entry is ever added by hand: relocate the code into a new module,
or shrink the file. A stale entry goes away rather than being kept:
--tighten drops one whose file is back under its ceiling, and an entry
naming a file that is gone is deleted by hand.

Scope is every tracked ``*.py`` under ``backend/``, ``scripts/`` and
``tests/``. A ``tests/`` prefix is a test file (ceiling 700 lines);
everything else is production (ceiling 500 lines).

  python3 scripts/ci/size_baseline.py
  python3 scripts/ci/size_baseline.py --tighten
"""
from __future__ import annotations

import argparse
import importlib
import subprocess
import sys
from pathlib import Path

if __package__:
    # pylint: disable-next=relative-beyond-top-level,no-name-in-module
    from . import thresholds
else:
    thresholds = importlib.import_module('thresholds')


ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_CEILING = 500
TEST_CEILING = 700
# Scope: tracked Python modules under these directory prefixes only.
TRACKED_PREFIXES = ('backend/', 'scripts/', 'tests/')

GROWTH_REMEDY = (
    'A recorded number is never raised by hand and no entry is ever added '
    'by hand: relocate the code into a new module, or shrink the file.')
STALE_ENTRY_REMEDY = (
    'A stale entry goes away rather than being kept: --tighten drops one '
    'whose file is back under its ceiling, and an entry naming a file '
    'that is gone is deleted by hand.')
REMEDY_FOR = {
    'grown': GROWTH_REMEDY,
    'over': GROWTH_REMEDY,
    'missing': STALE_ENTRY_REMEDY,
    'graduated': STALE_ENTRY_REMEDY,
}


def ceiling_for(rel):
    return TEST_CEILING if rel.startswith('tests/') else PRODUCTION_CEILING


def tracked_sizes(root=ROOT):
    """Return tracked Python module line counts within TRACKED_PREFIXES."""
    listed = subprocess.run(
        ['git', '-C', str(root), 'ls-files', '-z', '*.py'],
        capture_output=True, check=True, timeout=30)
    sizes = {}
    for raw in listed.stdout.split(b'\0'):
        if not raw:
            continue
        rel = raw.decode('utf-8', 'surrogateescape')
        if not rel.startswith(TRACKED_PREFIXES):
            continue
        path = root / rel
        if not path.is_file():
            continue
        with open(path, 'rb') as handle:
            sizes[rel] = sum(1 for _ in handle)
    return sizes


def violations(sizes, baseline):
    return {
        'grown': {rel: (sizes[rel], recorded)
                  for rel, recorded in baseline.items()
                  if rel in sizes and sizes[rel] > recorded},
        'over': {rel: (count, ceiling_for(rel))
                 for rel, count in sizes.items()
                 if rel not in baseline and count > ceiling_for(rel)},
        'missing': sorted(rel for rel in baseline if rel not in sizes),
        'graduated': {rel: (sizes[rel], ceiling_for(rel))
                      for rel in baseline
                      if rel in sizes and sizes[rel] <= ceiling_for(rel)},
    }


def tightened(baseline, sizes):
    """Return a lowered baseline mapping, or ``None`` when unchanged."""
    updated = dict(baseline)
    for rel, recorded in baseline.items():
        if rel not in sizes:
            continue
        current = sizes[rel]
        if current <= ceiling_for(rel):
            del updated[rel]
        elif current < recorded:
            updated[rel] = current
    return updated if updated != baseline else None


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tighten', action='store_true',
                        help='follow shrunk files down instead of reporting')
    parser.add_argument(
        '--thresholds', type=Path, default=thresholds.THRESHOLDS)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        data = thresholds.load(args.thresholds)
        baseline = thresholds.module_size_baseline(data)
        sizes = tracked_sizes()
        if args.tighten:
            updated = tightened(baseline, sizes)
            if updated is None:
                print('no module shrank below its recorded size')
                return 0
            data['module_size_baseline'] = updated
            thresholds.write(args.thresholds, data)
            print('tightened the module size baseline')
            return 0

        found = violations(sizes, baseline)
        if not any(found.values()):
            print(f'{len(sizes)} tracked modules within the size policy')
            return 0
        remedies = []
        for kind in ('grown', 'over', 'missing', 'graduated'):
            detail = found[kind]
            if detail:
                print(f'{kind}: {detail}', file=sys.stderr)
                if REMEDY_FOR[kind] not in remedies:
                    remedies.append(REMEDY_FOR[kind])
        for remedy in remedies:
            print(remedy, file=sys.stderr)
        return 1
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
