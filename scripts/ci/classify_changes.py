#!/usr/bin/env python3
"""Classify a ci-gate run: documentation-only, or a full change.

The gate workflows used to narrow documentation-only runs at the
trigger, with `paths-ignore` on their push and pull_request events. A
trigger filter cannot report: a docs-only change started no run at all,
so those check names never appeared, and a future required-check
ruleset could never go green on such a pull request. ci-gate.yml starts
on every push and pull request and narrows INSIDE the workflow instead:
this script reads the run's changed paths and emits one output,
`docs_only`, which conditions the expensive legs while the aggregate
job still reports.

Documentation is the exact set the old deny-lists carried, re-homed as
classifier patterns. Every fallback over-runs: an event this script
cannot identify, a file list it cannot read, or a list that may be
truncated all classify as a full change and run every leg. An API
failure that under-runs would skip gates over code; one that over-runs
only wastes minutes.
"""
from __future__ import annotations

import fnmatch
import os
import subprocess

# The paths-ignore deny-lists the gate workflows carried on master, now
# re-homed as classifier patterns. `.github/ci-thresholds.json` is
# deliberately NOT here: the ratchet bot's commit is kept off ci-gate by
# the push trigger's own paths-ignore, and a change to the thresholds
# data must run the gates rather than read as documentation.
DOC_PATTERNS = ('**/*.md', 'PRESENTATION.txt', 'examples/**', '.claude/**',
                'LICENSE', 'NOTICE', '.gitignore')

# The compare endpoint caps `files` at 300 entries; a list that long may
# be truncated, and a truncated list can read as documentation-only.
COMPARE_FILES_CAP = 300

# The pulls files endpoint paginates, but the API hard-caps the
# collection at 3000 files; past that a code file can sort beyond the
# cutoff unseen.
PULL_REQUEST_FILES_CAP = 3000

_WILDCARDS = '*?['
_HEX40 = frozenset('0123456789abcdef')


def matches(pattern, path):
    """Whether a GitHub filter `pattern` selects `path`."""
    if pattern.startswith('**/'):
        # GitHub's `*` does not cross a `/`, so only the final segment of
        # the path is compared against the rest of the pattern.
        return fnmatch.fnmatchcase(path.rsplit('/', 1)[-1], pattern[3:])
    if not any(char in pattern for char in _WILDCARDS):
        # Filter patterns are rooted: LICENSE selects LICENSE, never
        # sub/LICENSE or LICENSE.txt.
        return pattern == path
    if (pattern.endswith('/**') and pattern[:-3]
            and not any(char in pattern[:-3] for char in '*?[]')):
        return path.startswith(pattern[:-2])
    raise ValueError(f'unsupported pattern shape: {pattern!r}')


def is_documentation(path):
    """Whether any DOC_PATTERNS entry selects `path`."""
    return any(matches(pattern, path) for pattern in DOC_PATTERNS)


def documentation_only(paths):
    """Whether `paths` is nonempty and every entry is documentation."""
    return bool(paths) and all(is_documentation(path) for path in paths)


def _hex40(value):
    return (isinstance(value, str) and len(value) == 40
            and all(char in _HEX40 for char in value))


def _read(run, argv, cap=None):
    try:
        stdout = run(argv)
    except Exception:  # any read failure means over-run, never under-run
        return None
    paths = [line for line in stdout.splitlines() if line]
    if cap is not None and len(paths) >= cap:
        return None
    return paths or None


def changed_paths(event, run):
    """The paths this run changed, or None when they cannot be read."""
    repository = event.get('repository')
    if not repository:
        return None
    name = event.get('name')
    if name == 'pull_request':
        number = event.get('pull_request')
        if not (isinstance(number, str) and number.isascii()
                and number.isdigit()):
            return None
        return _read(run, [
            'gh', 'api', '--paginate', '-H', 'Cache-Control: no-cache',
            f'repos/{repository}/pulls/{number}/files', '--jq',
            '.[].filename'], cap=PULL_REQUEST_FILES_CAP)
    if name == 'push':
        before, sha = event.get('before'), event.get('sha')
        if not (_hex40(before) and _hex40(sha)) or before == '0' * 40:
            return None
        return _read(run, [
            'gh', 'api', '-H', 'Cache-Control: no-cache',
            f'repos/{repository}/compare/{before}...{sha}', '--jq',
            '.files[].filename'], cap=COMPARE_FILES_CAP)
    return None


def classify(event, run):
    """Return (docs_only, reason) for this run's changed paths."""
    paths = changed_paths(event, run)
    if paths is None:
        return (False, 'could not read the changed paths; running the '
                       'full gate set')
    if documentation_only(paths):
        return (True, f'documentation-only change: {len(paths)} paths')
    outside = sum(1 for path in paths if not is_documentation(path))
    return (False, f'{len(paths)} paths changed, {outside} outside '
                   'documentation')


def event_from_environment(environ):
    """Build the `event` mapping from a process environment."""
    return {
        'name': environ.get('GITHUB_EVENT_NAME', ''),
        'repository': environ.get('GITHUB_REPOSITORY'),
        'sha': environ.get('GITHUB_SHA'),
        'pull_request': environ.get('PR_NUMBER'),
        'before': environ.get('BEFORE_SHA'),
    }


def write_outputs(path, documentation, reason):
    """Append the step outputs to the file `path` names."""
    if not path:
        return
    with open(path, 'a', encoding='utf-8') as handle:
        handle.write(f"docs_only={'true' if documentation else 'false'}\n")
        handle.write(f'reason={reason}\n')


def main():
    """Classify this run and record the outputs. Returns an exit status."""
    event = event_from_environment(os.environ)

    def run(command):
        return subprocess.run(
            command, capture_output=True, text=True, check=True,
            timeout=60).stdout

    documentation, reason = classify(event, run)
    write_outputs(os.environ.get('GITHUB_OUTPUT'), documentation, reason)
    print(reason)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
