#!/usr/bin/env python3
"""List the open pull-request heads a new required check would strand.

Once `ci gate / aggregate` becomes a required check, a head that has
never run it (or whose latest run predates the workflow) cannot go
green until it rebases or reruns. The manager rebinding rulesets needs
that set NAMED, not guessed; this script lists it.

SELECTION. A head is stale when its latest `ci gate / aggregate` run —
the Actions runs of ci-gate.yml whose head SHA is the PR's — predates
the first commit carrying ci-gate.yml, or when it has none. Everything
is read fresh with no-cache headers and pagination; a head whose runs
cannot be read is reported stale (fail-closed), never silently dropped.

THE REFERENCE POINT. The reference is the commit that ADDED ci-gate.yml
to master, read by `git log --diff-filter=A` over the checkout. A head
that carries the file itself (a branch gated by a branch-carried
ci-gate before the workflow landed on master) is compared against the
same instant.

THE BOUND. One git lookup, one open-PR listing, one run list per open
head. No check runs are created, nothing is written back: this is a
report, and its deliverable is the step summary naming the stale heads
that must rebase or rerun once the aggregate becomes required.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from urllib.parse import quote

WORKFLOW_FILE = '.github/workflows/ci-gate.yml'
# The workflow filename the runs API filters by (the URL segment, not
# the stored path).
WORKFLOW_ID = 'ci-gate.yml'
CHECK_NAME = 'ci gate / aggregate'
BASE_BRANCH = 'master'
_OLDEST = datetime.min.replace(tzinfo=timezone.utc)
_HEX40 = frozenset('0123456789abcdef')


class QueryError(RuntimeError):
    """A `gh api` call or a git lookup that could not be read."""


def _hex40(value):
    return (isinstance(value, str) and len(value) == 40
            and all(char in _HEX40 for char in value))


def _stamp(text):
    """Parse an API timestamp; an unparseable one reads as the oldest."""
    stamp = _OLDEST
    if text:
        try:
            stamp = datetime.fromisoformat(str(text).replace('Z', '+00:00'))
        except ValueError:
            stamp = _OLDEST
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def _run_key(entry):
    """Order runs by (started_at, id) so list order never decides."""
    run_id = entry.get('id')
    try:
        number = int(run_id)
    except (TypeError, ValueError):
        number = 0
    return _stamp(entry.get('run_started_at') or entry.get('created_at')), \
        number


def select_stale(pulls, runs_by_sha, gate_started_at):
    """The open heads on BASE_BRANCH that must rebase or rerun.

    `pulls` carries (number, base, head sha) records; `runs_by_sha` maps
    a head SHA to its ci-gate run list, where an entry is a mapping with
    `id` and `run_started_at` (or `created_at`). A head is stale when it
    has no run list, an empty one, or a latest run strictly before
    `gate_started_at`.
    """
    stale = []
    for pr in pulls:
        number, sha = pr.get('number'), pr.get('sha')
        if pr.get('base') != BASE_BRANCH or not _hex40(sha):
            continue
        runs = runs_by_sha.get(sha)
        if not runs:
            stale.append({
                'number': number,
                'sha': sha,
                'reason': f'no {CHECK_NAME} run recorded on this head',
            })
            continue
        latest = max(runs, key=_run_key)
        started = _stamp(
            latest.get('run_started_at') or latest.get('created_at'))
        if started < gate_started_at:
            stale.append({
                'number': number,
                'sha': sha,
                'reason': (
                    f'latest {CHECK_NAME} run (id {latest.get("id")}, '
                    f'started {started.isoformat()}) predates the first '
                    'ci-gate commit '
                    f'({gate_started_at.isoformat()})'),
            })
    return stale


def first_gate_commit(run_git):
    """(sha, committed_at) of the first commit carrying ci-gate.yml."""
    try:
        out = run_git(['git', 'log', '--diff-filter=A', '--format=%H %cI',
                       '--', WORKFLOW_FILE])
    except Exception as exc:
        raise QueryError(
            f'the git lookup for {WORKFLOW_FILE} failed: {exc}') from exc
    lines = [line for line in out.strip().splitlines() if line.strip()]
    if not lines:
        raise QueryError(
            f'no commit adds {WORKFLOW_FILE} in this checkout; fetch the '
            'full history and run from the repository root')
    # Oldest adding commit wins: a delete-and-re-add later must not move
    # the reference point.
    sha, stamp_text = lines[-1].split(' ', 1)
    if not _hex40(sha):
        raise QueryError(f'unreadable sha from git: {sha!r}')
    stamp = _stamp(stamp_text)
    if stamp == _OLDEST:
        raise QueryError(f'unreadable commit time from git: {stamp_text!r}')
    return sha, stamp


def _read(argv):
    """One `gh api` call: stdout, or a QueryError naming the failure."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=60, check=False)
    except (subprocess.SubprocessError, OSError) as exc:
        raise QueryError(f'gh failed: {exc}') from exc
    if proc.returncode != 0:
        raise QueryError(
            proc.stderr.strip()[:400] or f'gh exit {proc.returncode}')
    return proc.stdout


def _decode(stdout, what):
    try:
        return json.loads(stdout)
    except ValueError as exc:
        raise QueryError(f'unparseable {what}: {exc}') from exc


def _api(url, *extra):
    """The one call shape, so the no-cache convention has one site."""
    return ['gh', 'api', '-H', 'Cache-Control: no-cache', url, *extra]


def _open_pulls(repository):
    """Every open pull request against BASE_BRANCH, complete."""
    stdout = _read(_api(
        f'repos/{repository}/pulls?state=open&base={quote(BASE_BRANCH)}'
        f'&per_page=100', '--paginate'))
    payload = _decode(stdout, 'the open pull-request list')
    if not isinstance(payload, list):
        raise QueryError('the open pull-request list did not decode as a '
                         'list')
    return payload


def _runs_for(repository, sha):
    """ci-gate's runs for one head SHA, complete."""
    stdout = _read(_api(
        f'repos/{repository}/actions/workflows/{quote(WORKFLOW_ID)}'
        f'/runs?head_sha={sha}&per_page=100', '--paginate'))
    payload = _decode(stdout, 'the workflow-run list')
    if not isinstance(payload, dict):
        raise QueryError('the workflow-run list did not decode as an '
                         'object')
    runs = payload.get('workflow_runs')
    return runs if isinstance(runs, list) else []


def write_summary(path, stale, scanned):
    """The step summary naming the stale heads and why each is stale."""
    if not path:
        return
    lines = [
        '### Gate freshness',
        '',
        f'{len(stale)} of {scanned} open pull-request head(s) on '
        f'{BASE_BRANCH} must rebase or rerun once `{CHECK_NAME}` becomes '
        'a required check:',
        '',
    ]
    if stale:
        for entry in stale:
            lines.append(f'- PR #{entry["number"]} '
                         f'(`{str(entry["sha"])[:12]}`): {entry["reason"]}')
    else:
        lines.append('None — every open head has an aggregate run at or '
                     'after the ci-gate workflow landed.')
    with open(path, 'a', encoding='utf-8') as handle:
        handle.write('\n'.join(lines) + '\n')


def main():
    repository = os.environ.get('GITHUB_REPOSITORY', '')

    def run_git(argv):
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=60, check=False)
        if proc.returncode != 0:
            raise QueryError(
                proc.stderr.strip()[:400] or f'git exit {proc.returncode}')
        return proc.stdout

    try:
        _, committed = first_gate_commit(run_git)
    except QueryError as error:
        print(f'gate freshness: {error}; reporting nothing',
              file=sys.stderr)
        return 1
    try:
        payload = _open_pulls(repository)
    except QueryError as error:
        print(f'gate freshness: could not read the open pull-request '
              f'list; reporting nothing: {error}', file=sys.stderr)
        return 1
    heads = []
    for pr in payload:
        if not isinstance(pr, dict):
            continue
        head = pr.get('head')
        base = pr.get('base')
        heads.append({
            'number': pr.get('number'),
            'sha': head.get('sha') if isinstance(head, dict) else None,
            'base': base.get('ref') if isinstance(base, dict) else None,
        })
    runs_by_sha: dict[str, list | None] = {}
    for head in heads:
        sha = head['sha']
        if not _hex40(sha):
            continue
        try:
            runs_by_sha[sha] = _runs_for(repository, sha)
        except QueryError as error:
            # Fail closed: an unreadable run list must not read as
            # fresh. Report the head stale and name the cause on stderr.
            runs_by_sha[sha] = []
            print(f'gate freshness: could not read the runs for PR '
                  f'#{head["number"]}: {error}; reporting the head stale',
                  file=sys.stderr)
    stale = select_stale(heads, runs_by_sha, committed)
    write_summary(os.environ.get('GITHUB_STEP_SUMMARY'), stale,
                  scanned=len(heads))
    for entry in stale:
        print(f'PR #{entry["number"]}: {entry["reason"]}')
    print(f'gate freshness: {len(stale)} stale of {len(heads)} open '
          f'pull-request head(s) on {BASE_BRANCH}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
