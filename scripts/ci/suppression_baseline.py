#!/usr/bin/env python3
"""Check and tighten the inline pylint suppression baseline.

A second only-shrinks baseline beside the module-size one (issue #389):
the per-file count of inline ``pylint: disable``/``disable-next``
comments whose check list names a complexity check — any check starting
``too-many-`` — over tracked ``*.py`` under ``backend/`` and
``scripts/``. ``tests/`` stays outside: a suppression there is not
production complexity debt. An entry is never added or raised by hand,
exactly like ``module_size_baseline``; the remedy is to reduce the
complexity (or rely on master's --tighten to follow a shrunk count down
and drop a clean or missing file). A counted site is a LINE containing
at least one qualifying disable, so a long check list counts once.

  python3 scripts/ci/suppression_baseline.py
  python3 scripts/ci/suppression_baseline.py --tighten
"""
from __future__ import annotations

import argparse
import importlib
import re
import subprocess
import sys
from pathlib import Path

if __package__:
    # pylint: disable-next=relative-beyond-top-level,no-name-in-module
    from . import thresholds, thresholds_removed
else:
    thresholds = importlib.import_module('thresholds')
    thresholds_removed = importlib.import_module('thresholds_removed')


ROOT = Path(__file__).resolve().parents[2]
# Production scope: tracked Python under these prefixes; tests/ stays
# outside by construction (and its suppressions stay ungated).
TRACKED_PREFIXES = ('backend/', 'scripts/')
# A counted site: a line whose disable comment's check list names a
# complexity check. The check list spelling pylint itself accepts:
# lowercase check names, commas, and whitespace padding.
_SUPPRESSION = re.compile(
    r'#\s*pylint:\s*disable(?:-next)?\s*=\s*([a-z0-9_,\s\-]+)')
_UNRECORDED_LIMIT = 1

GROWTH_REMEDY = (
    'A suppression entry is never raised or added by hand: reduce the '
    'complexity, or argue the exception in a gate-definer change.')
STALE_ENTRY_REMEDY = (
    'A stale entry goes away rather than being kept: master\'s --tighten '
    'drops one whose file went clean or no longer exists.')
REMEDY_FOR = {
    'grown': GROWTH_REMEDY,
    'over': GROWTH_REMEDY,
    'missing': STALE_ENTRY_REMEDY,
    'graduated': STALE_ENTRY_REMEDY,
}


def _is_suppression_line(line):
    """Whether a line's disable comment names a complexity check."""
    return any(
        check.strip().startswith('too-many-')
        for checks in _SUPPRESSION.findall(line)
        for check in checks.split(','))


def suppression_lines(text):
    """Count lines carrying a complexity-check disable comment."""
    return sum(
        1 for line in text.splitlines() if _is_suppression_line(line))


def tracked_suppression_counts(root=ROOT):
    """Return per-file suppression-line counts for production Python.

    Every tracked production file is listed, zero counts included, so
    the violation classes can tell a graduated file (tracked, back to
    zero) from a missing one (untracked, removed by master's --tighten).
    """
    listed = subprocess.run(
        ['git', '-C', str(root), 'ls-files', '-z', '*.py'],
        capture_output=True, check=True, timeout=30)
    counts = {}
    for raw in listed.stdout.split(b'\0'):
        if not raw:
            continue
        rel = raw.decode('utf-8', 'surrogateescape')
        if not rel.startswith(TRACKED_PREFIXES):
            continue
        path = root / rel
        if not path.is_file():
            continue
        counts[rel] = suppression_lines(
            path.read_text(encoding='utf-8', errors='surrogateescape'))
    return counts


def violations(counts, baseline,
               declared_removed: set[str] | frozenset[str] = frozenset()):
    return {
        'grown': {rel: (counts[rel], recorded)
                  for rel, recorded in baseline.items()
                  if rel in counts and counts[rel] > recorded},
        # An unrecorded file is over at any count: the absence of an
        # entry is its zero, so one more suppression site than zero is
        # growth past the policy.
        'over': {rel: (count, _UNRECORDED_LIMIT)
                 for rel, count in counts.items()
                 if rel not in baseline and rel not in declared_removed
                 and count >= _UNRECORDED_LIMIT},
        'missing': sorted(rel for rel in baseline if rel not in counts),
        'graduated': {rel: 0 for rel in baseline
                      if rel in counts and counts[rel] == 0},
    }


def _check(counts, baseline, base_rev, head_rev):
    exempt = thresholds_removed.exempt_paths(
        base_rev, head_rev, ROOT, 'pylint_suppression_baseline', baseline, counts)
    for rel in sorted(exempt):
        if counts[rel] >= _UNRECORDED_LIMIT:
            print(f'declared-removed: {rel} ({counts[rel]} suppressions '
                  'above the default until master seeds)')
    return violations(counts, baseline, exempt)


def tightened(baseline, counts):
    """Lower or seed suppression entries; return ``None`` if unchanged."""
    updated = dict(baseline)
    for rel, recorded in baseline.items():
        if rel not in counts:
            del updated[rel]
            continue
        current = counts[rel]
        if current == 0:
            del updated[rel]
        elif current < recorded:
            updated[rel] = current
    updated.update({rel: count for rel, count in counts.items()
                    if rel not in baseline and count >= _UNRECORDED_LIMIT})
    return updated if updated != baseline else None


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tighten', action='store_true',
                        help='follow shrunk counts down instead of '
                             'reporting')
    parser.add_argument(
        '--thresholds', type=Path, default=thresholds.THRESHOLDS)
    parser.add_argument('--base-rev', help='exclusive base of the judged commit range')
    parser.add_argument('--head-rev', default='HEAD',
                        help='inclusive head of the judged commit range (default HEAD)')
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        data = thresholds.load(args.thresholds)
        baseline = data.get('pylint_suppression_baseline', {})
        counts = tracked_suppression_counts()
        if args.tighten:
            updated = tightened(baseline, counts)
            if updated is None:
                print('no file\'s count dropped below its recorded '
                      'suppressions')
                return 0
            data['pylint_suppression_baseline'] = updated
            thresholds.write(args.thresholds, data)
            print('tightened the pylint suppression baseline; '
                  f'seeded {len(updated.keys() - baseline.keys())} entries')
            return 0

        found = _check(counts, baseline, args.base_rev, args.head_rev)
        if not any(found.values()):
            print(f'no complexity suppressions outside the baseline: '
                  f'{len(baseline)} recorded files')
            return 0
        failed = bool(found['grown'] or found['over'])
        diagnostics = [(kind, detail) for kind, detail in found.items()
                       if detail]
        for kind, detail in diagnostics:
            print(f'{kind}: {detail}', file=sys.stderr)
        remedies = dict.fromkeys(
            REMEDY_FOR[kind] for kind, _ in diagnostics)
        for remedy in remedies:
            print(remedy, file=sys.stderr)
        return int(failed)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
