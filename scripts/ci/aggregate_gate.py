#!/usr/bin/env python3
"""The aggregate job's verdict: fold every ci-gate leg into one check.

`ci gate / aggregate` is the one name the required-check ruleset
requires, so it must report even when legs fail, skip or never start —
the job carries `if: always()` and this module owns the fold over the
needs document the workflow passes in as JSON:

- a leg's `success` passes, and so does its `skipped` when and only
  when a classifier output narrowed it — `docs_only` skips every leg,
  `data_only` skips every leg outside CHEAP_LEGS, `comment_only` skips
  exactly COMMENT_ONLY_SKIPPED (the legs a comment cannot affect) — so
  narrowing runs still report without any check ever going missing;
  `setup_only` requires setup-docs alone, which otherwise skips because
  the ordinary tests leg covers its guard;
- any other result (`failure`, `cancelled`, anything unrecognised)
  fails the aggregate and is named in the verdict;
- a leg the document lacks at all fails it too, so the fold and the
  workflow cannot silently disagree about what a complete run covers.

SUPERSESSION IS NOT THIS MODULE'S DECISION. ci-gate groups requests by
number and pushes by SHA; test-data is per-SHA too. Per-ref legs can cancel
a queued middle run when a third arrives. All cancellations fail here,
deliberate or otherwise, without querying for a newer run.

The verdict and a per-leg table go to the step summary; the exit code
is the gate.
"""
from __future__ import annotations

import json
import os
import sys

# Every workflow leg, classifier included; kept in sync with its needs
# list by tests/test_workflow_ci_gate.py.
EXPECTED_LEGS = ('classify', 'tests', 'test-data', 'lint', 'types', 'eslint',
                 'panel-layout', 'smoke', 'docker-smoke', 'audit',
                 'actionlint', 'codeql', 'setup-docs')

PASSED = 'passed'
FAILED = 'failed'

# The legs the data-only class still runs: lint and the boot-and-serve
# check. Pinned in lockstep with the workflow by tests
# (tests/test_workflow_ci_gate.py).
CHEAP_LEGS = frozenset({'lint', 'smoke'})

# The legs the comment-only class skips: the perturbed-data leg and the
# panel-layout guard, neither of which a comment can affect. CodeQL is
# NOT here: `// codeql[...]` and `// lgtm[...]` are comment-keyed alert
# suppressions, so a comment-only diff CAN change its verdict and the
# class's "a comment cannot affect it" invariant is false for it.
# tests/test_workflow_ci_gate.py pins the legs; the matrix half (the
# `comment_only` passthrough into tests.yml) is pinned by
# tests/test_ci_classify_comment_only.py.
COMMENT_ONLY_SKIPPED = frozenset({'test-data', 'panel-layout'})


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


def comment_only(needs):
    """The classifier's comment_only output, read fail-closed."""
    return _classify_output(needs, 'comment_only')


def thresholds_only(needs):
    """The classifier's thresholds_only output, read fail-closed."""
    return _classify_output(needs, 'thresholds_only')


def _narrowed(name, needs):
    """Whether classification legitimately skips leg `name`.

    A docs-only run skips every leg; a data-only run skips every leg
    OUTSIDE the cheap set — lint and smoke are why the class exists; a
    comment-only run skips exactly the legs a comment cannot affect.
    A thresholds-only run requires tests and skips the other gate legs.
    """
    if name == 'setup-docs':
        return not _classify_output(needs, 'setup_only')
    if docs_only(needs):
        return True
    if _classify_output(needs, 'setup_only') and name in EXPECTED_LEGS[1:]:
        return True
    if data_only(needs) and name not in CHEAP_LEGS:
        return True
    if thresholds_only(needs) and name in EXPECTED_LEGS[1:] and name != 'tests':
        return True
    return comment_only(needs) and name in COMMENT_ONLY_SKIPPED


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
        if thresholds_only(needs):
            skipped = sorted(name for name, result in entries
                             if result == 'skipped')
            joined = ', '.join(skipped)
            return PASSED, (
                f'thresholds-only change: {len(skipped)} gate legs skipped by '
                f'classification ({joined}); tests ran narrowed')
        if comment_only(needs):
            skipped = sorted(name for name, result in entries
                             if result == 'skipped')
            joined = ', '.join(skipped)
            legs = ', '.join(sorted(COMMENT_ONLY_SKIPPED))
            return PASSED, (
                f'comment-only change: {len(skipped)} gate legs skipped by '
                f'classification ({joined}); the legs a comment cannot '
                f'affect were skipped ({legs})')
        # One sentence, naming no legs: the per-leg results are the
        # table's job -- avoid repeating the enumeration.
        return PASSED, f'all {len(entries)} legs passed'
    parts = []
    if failures:
        parts.append('failing legs: ' + ', '.join(sorted(failures)))
    if missing:
        parts.append('missing legs: ' + ', '.join(missing))
    return FAILED, '; '.join(parts)


def render(needs, verdict, message):
    lines = [
        '### ci gate',
        '',
        f'verdict: **{verdict}**',
        '',
        f'docs-only narrowing: '
        f'{"true" if _classify_output(needs, "docs_only") else "false"}',
        f'data-only narrowing: '
        f'{"true" if _classify_output(needs, "data_only") else "false"}',
        f'comment-only narrowing: '
        f'{"true" if _classify_output(needs, "comment_only") else "false"}',
        f'thresholds-only narrowing: '
        f'{"true" if thresholds_only(needs) else "false"}',
        f'setup-only narrowing: '
        f'{"true" if _classify_output(needs, "setup_only") else "false"}',
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
            elif thresholds_only(needs) and _narrowed(name, needs):
                note = ' (thresholds-only narrowing)'
            elif comment_only(needs) and name in COMMENT_ONLY_SKIPPED:
                note = ' (comment-only narrowing)'
        lines.append(f'| {name} | {result}{note} |')
    return '\n'.join(lines) + '\n'


def write_summary(path, text):
    if not path:
        return
    with open(path, 'a', encoding='utf-8') as handle:
        handle.write(text)


def main_with(needs, summary_path=None):
    verdict, message = decide(needs)
    write_summary(summary_path or os.environ.get('GITHUB_STEP_SUMMARY'),
                  render(needs, verdict, message))
    return 0 if verdict == PASSED else 1


def main():
    try:
        needs = json.loads(os.environ.get('CLAUDIT_NEEDS_JSON', os.environ.get('NEEDS_JSON')) or '')
    except ValueError:
        print('CLAUDIT_NEEDS_JSON is missing or not JSON', file=sys.stderr)
        return 1
    if not isinstance(needs, dict):
        print('CLAUDIT_NEEDS_JSON is not a needs document', file=sys.stderr)
        return 1
    print(f'legs in the document: {", ".join(sorted(needs))}')
    return main_with(needs)


if __name__ == '__main__':
    raise SystemExit(main())
