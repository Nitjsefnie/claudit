#!/usr/bin/env python3
"""The aggregate job's verdict: fold every ci-gate leg into one check.

`ci gate / aggregate` is the one name the required-check ruleset
requires, so it must report even when legs fail, skip or never start —
the job carries `if: always()` and this module owns the fold over the
needs document the workflow passes in as JSON:

- a leg's `success` passes, and so does its `skipped` when and only
  when a classifier output narrowed it — `docs_only` skips every leg,
  `data_only` skips every leg outside CHEAP_LEGS — so narrowing runs
  still report without any check ever going missing;
- any other result (`failure`, `cancelled`, anything unrecognised)
  fails the aggregate and is named in the verdict;
- a leg the document lacks at all fails it too, so the fold and the
  workflow cannot silently disagree about what a complete run covers.

SUPERSESSION IS NOT THIS MODULE'S DECISION. ci-gate's concurrency group
is one per pull request, or per pushed commit SHA on a push (issue
#358), so a newer push starts its own run and the group itself never
cancels the older one — the ordinary two-push supersession cancel
cannot leave a stale aggregate behind. The two long legs (speed,
test-data) are per-SHA on a push too (issues #366, #409); a residual
lives one level down in the remaining per-ref legs, whose groups hold
at most one PENDING run, so a third concurrent master push cancels a
merely-queued short leg, and that middle run's aggregate folds red
here (the window is that leg's own duration; fails closed; the next
push re-verifies it — see the workflow's SUPERSESSION block). Beyond
that residual, a run-level cancel is
always DELIBERATE: it leaves that run's aggregate cancelled, which a
required-check ruleset treats as never-green, exactly as a failure
does. So this module treats a cancelled leg as a failure without
querying for a newer run.

The verdict and a per-leg table go to the step summary; the exit code
is the gate.
"""
from __future__ import annotations

import json
import os
import sys

# Every leg the workflow must report through, classifier included.
# tests/test_workflow_ci_gate.py pins this tuple against the workflow's
# needs list, so the two cannot drift apart.
EXPECTED_LEGS = ('classify', 'tests', 'test-data', 'lint', 'types', 'eslint',
                 'smoke', 'audit', 'actionlint', 'speed', 'codeql')

PASSED = 'passed'
FAILED = 'failed'

# The legs the data-only class still runs: lint and the boot-and-serve
# check. Pinned in lockstep with the workflow by tests
# (tests/test_workflow_ci_gate.py, issue #455).
CHEAP_LEGS = frozenset({'lint', 'smoke'})


def _classify_output(needs, name):
    """A classifier output, read fail-closed."""
    classify = needs.get('classify')
    if not isinstance(classify, dict):
        return False
    outputs = classify.get('outputs')
    if not isinstance(outputs, dict):
        return False
    return outputs.get(name) == 'true'


def docs_only(needs):
    """The classifier's docs_only output, read fail-closed."""
    return _classify_output(needs, 'docs_only')


def data_only(needs):
    """The classifier's data_only output, read fail-closed."""
    return _classify_output(needs, 'data_only')


def _narrowed(name, needs):
    """Whether classification legitimately skips leg `name`.

    A docs-only run skips every leg; a data-only run skips every leg
    OUTSIDE the cheap set — lint and smoke are why the class exists.
    """
    if docs_only(needs):
        return True
    return data_only(needs) and name not in CHEAP_LEGS


def fold(needs):
    """Return (ok, failures, missing, entries) over the needs document.

    `failures` names every leg whose result gates red, `missing` every
    expected leg the document lacks, `entries` is (name, result) for
    everything the document carried.
    """
    failures = []
    missing = [name for name in EXPECTED_LEGS if name not in needs]
    entries = []
    for name, details in needs.items():
        result = (details or {}).get('result')
        entries.append((name, result))
        if result == 'success':
            continue
        if result == 'skipped' and _narrowed(name, needs):
            continue
        failures.append(f'{name}={result}')
    return (not failures and not missing), failures, missing, entries


def decide(needs):
    """Return (verdict, message). The message names the legs that decided."""
    ok, failures, missing, entries = fold(needs)
    if ok:
        if docs_only(needs):
            skipped = sorted(name for name, result in entries
                             if result == 'skipped')
            joined = ', '.join(skipped)
            return PASSED, (
                f'docs-only change: {len(skipped)} gate legs skipped by '
                f'classification ({joined}); the aggregate still reports')
        if data_only(needs):
            skipped = sorted(name for name, result in entries
                             if result == 'skipped')
            joined = ', '.join(skipped)
            cheap = ', '.join(sorted(CHEAP_LEGS))
            return PASSED, (
                f'data-only change: {len(skipped)} gate legs skipped by '
                f'classification ({joined}); cheap legs ran ({cheap})')
        joined = ', '.join(f'{name}={result}' for name, result in entries)
        return PASSED, f'all {len(entries)} legs succeeded: {joined}'
    parts = []
    if failures:
        parts.append('failing legs: ' + ', '.join(sorted(failures)))
    if missing:
        parts.append('missing legs: ' + ', '.join(missing))
    return FAILED, '; '.join(parts)


def render(needs, verdict, message):
    """The markdown step summary: the verdict plus one row per leg."""
    lines = [
        '### ci gate',
        '',
        f'verdict: **{verdict}**',
        '',
        f'docs-only narrowing: '
        f'{"true" if _classify_output(needs, "docs_only") else "false"}',
        f'data-only narrowing: '
        f'{"true" if _classify_output(needs, "data_only") else "false"}',
    ]
    classify = needs.get('classify')
    reason = ((classify or {}).get('outputs') or {}).get('reason')
    if reason:
        lines.append(f'classification: {reason}')
    lines += [
        '',
        message,
        '',
        '| leg | result |',
        '| --- | --- |',
    ]
    for name in sorted(needs):
        result = (needs[name] or {}).get('result') or '(none)'
        note = ''
        if result == 'skipped':
            if docs_only(needs):
                note = ' (docs-only narrowing)'
            elif data_only(needs) and name not in CHEAP_LEGS:
                note = ' (data-only narrowing)'
        lines.append(f'| {name} | {result}{note} |')
    return '\n'.join(lines) + '\n'


def write_summary(path, text):
    """Append the rendered summary where the step summary env names."""
    if not path:
        return
    with open(path, 'a', encoding='utf-8') as handle:
        handle.write(text)


def main_with(needs, summary_path=None):
    """The decision over an injected document, for tests and callers."""
    verdict, message = decide(needs)
    write_summary(summary_path or os.environ.get('GITHUB_STEP_SUMMARY'),
                  render(needs, verdict, message))
    return 0 if verdict == PASSED else 1


def main():
    try:
        needs = json.loads(os.environ.get('NEEDS_JSON') or '')
    except ValueError:
        print('NEEDS_JSON is missing or not JSON', file=sys.stderr)
        return 1
    if not isinstance(needs, dict):
        print('NEEDS_JSON is not a needs document', file=sys.stderr)
        return 1
    print(f'legs in the document: {", ".join(sorted(needs))}')
    return main_with(needs)


if __name__ == '__main__':
    raise SystemExit(main())
