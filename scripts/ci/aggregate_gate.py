#!/usr/bin/env python3
"""The aggregate job's verdict: fold every ci-gate leg into one check.

`ci gate / aggregate` is the one name a future required-check ruleset
requires, so it must report even when legs fail, skip or never start —
the job carries `if: always()` and this module owns the fold over the
needs document the workflow passes in as JSON:

- a leg's `success` passes, and so does its `skipped` when and only
  when the classifier's `docs_only` output narrowed it —
  documentation-only runs narrow the expensive legs without any check
  ever going missing;
- any other result (`failure`, `cancelled`, anything unrecognised)
  fails the aggregate and is named in the verdict;
- a leg the document lacks at all fails it too, so the fold and the
  workflow cannot silently disagree about what a complete run covers.

SUPERSESSION IS NOT THIS MODULE'S DECISION. The workflow's concurrency
group (one per pull request or branch, `cancel-in-progress`) means a
run superseded by a newer push is cancelled WHOLE — its aggregate never
reports, and the newer run's aggregate is the verdict that exists.
That is the rule "a run cancelled because a newer run superseded it
gates nothing": no stale red aggregate is ever left behind to read. A
DELIBERATE cancel of a still-current run leaves that run's aggregate
cancelled as well, which a required-check ruleset treats as
never-green, exactly as a failure does. So this module treats a
cancelled leg as a failure without querying for a newer run; the
concurrency group already answered it.

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


def docs_only(needs):
    """The classifier's docs_only output, read fail-closed."""
    classify = needs.get('classify')
    if not isinstance(classify, dict):
        return False
    outputs = classify.get('outputs')
    if not isinstance(outputs, dict):
        return False
    return outputs.get('docs_only') == 'true'


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
        if result == 'skipped' and docs_only(needs):
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
    narrowed = 'true' if docs_only(needs) else 'false'
    lines = [
        '### ci gate',
        '',
        f'verdict: **{verdict}**',
        '',
        f'docs-only narrowing: {narrowed}',
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
        note = ' (docs-only narrowing)' if (
            result == 'skipped' and docs_only(needs)) else ''
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
