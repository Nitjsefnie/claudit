#!/usr/bin/env python3
"""Classify a ci-gate run: docs-only, bot-data-only, comment-only, or full.

The gate workflows used to narrow documentation-only runs at the
trigger, with `paths-ignore` on their push and pull_request events. A
trigger filter cannot report: a docs-only change started no run at all,
so those check names never appeared, and a required-check
ruleset could never go green on such a pull request. ci-gate.yml starts
on every push and pull request and narrows INSIDE the workflow instead:
this script reads the run's changed paths and emits two outputs,
`docs_only` and `data_only`, which condition the legs while the
aggregate job still reports.

Documentation is the exact set the old deny-lists carried, re-homed as
classifier patterns, minus the three docs whose setup blocks a test
guard pins (SETUP_DOC_PATTERNS): the guard lives in the tests leg, so a
docs-only classification of a change to those files would skip the leg
that fails it (issue #477). Every fallback over-runs: an event this
script cannot identify, a file list it cannot read, or a list that may
be truncated all classify as a full change and run every leg. An API
failure that under-runs would skip gates over code; one that over-runs
only wastes minutes.

A second class narrows the refresh bot's data: its hourly push changes
EXACTLY `src/pricing.json` and `backend/constants.py` — the appended
rate entries, and the PRICING_VERSION bump that SV-RATE-REFRESH ships
in the same commit — so that exact changed set gets the cheap class.
The refresh job runs the full suite against the new rates BEFORE
pushing (refresh-pricing.yml's refresh job), so re-running the whole
matrix on the bot's push re-verifies nothing. The class matches the
exact SET, never per-file verdicts: a third path, or either half
alone, is not the bot. `src/pricing.json` alone is a lone hand-edited
rate file — the bot never produces it, so it has no pre-test behind it
and runs the full matrix.

A THIRD class narrows a change that cannot move behaviour at all. When
EVERY changed path is a source file (.py/.js/.jsx), the set fits
MAX_COMMENT_ONLY_FILES, and each path's base and head blob both read,
DIFFER as text, and strip to an IDENTICAL token stream once comments are
removed, the change is `comment_only`: the legs a comment cannot affect
(test-data, panel-layout, codeql) and the portable pytest matrix inside
tests.yml are skipped, while the behaviour legs still run. A docstring
edit is a STRING token and reads as a change; a byte-equal (no-op or
mode-only) pair is not comment-only. Every failure over-runs: an
unreadable ref or blob, a tokenization error, a set too large or
outside the source suffixes.

On a PUSH the changed set is read over the verified base rather than
the push's own `before`: the newest ci-gate run on master whose
conclusion is `success` and that executed at least one leg marks the
last commit whose code the gate actually ran over. Classifying only
the push's own before...sha let a docs-only push hide an unverified
code push behind a green aggregate, when the code push's own run had
been cancelled by the docs-only push seconds after it started — and a
run that concluded `failure` or `timed_out` verified nothing either,
so a docs-only push on top of failed code would otherwise narrow over
the very code that is failing. The base walk reads the workflow-runs
and run-jobs endpoints, newest first; any failure over-runs to the
full gate set.
"""
from __future__ import annotations

import fnmatch
import io
import json
import os
import subprocess
import tempfile
import tokenize

# The paths-ignore deny-lists the gate workflows carried on master, now
# re-homed as classifier patterns. `.github/ci-thresholds.json` is
# deliberately NOT here: the ratchet bot's commit is kept off ci-gate by
# the push trigger's own paths-ignore, and a change to the thresholds
# data must run the gates rather than read as documentation.
DOC_PATTERNS = ('**/*.md', 'PRESENTATION.txt', 'examples/**', '.claude/**',
                'LICENSE', 'NOTICE', '.gitignore')

# The docs carrying the pinned setup blocks: tests/test_docs_setup.py
# asserts README's "Quick start", CONTRIBUTING's "Getting it running" and
# AGENTS's "Build and test commands" keep the same step set (issue #464).
# A change to any of them is documentation by shape and tested by
# behavior, so the carve-out below is by FILE, not by section: the gate
# classifies changed PATHS and never reads content, and a per-section
# rule would need a content diff this script has no read for. The cost is
# an over-run -- an edit to a badge above the setup block runs the matrix
# instead of skipping it -- which is this script's declared direction for
# every uncertainty ("Every fallback over-runs"). The alternative failure
# is a green push that broke the pin and finds out hours later.
SETUP_DOC_PATTERNS = ('README.md', 'CONTRIBUTING.md', 'AGENTS.md')

# The refresh bot's push signature: the hourly refresh appends rate
# entries to `src/pricing.json` and bumps PRICING_VERSION in
# `backend/constants.py` in the SAME commit (SV-RATE-REFRESH), so a bot
# push changes exactly this pair. Deliberately NOT a filter set beside
# DOC_PATTERNS: the cheap class exists for generated data the refresh
# job already tested, so only the exact set gets it — the boot-and-serve
# check (smoke) and lint still run, and a lone hand-edited rate file is
# untested data that keeps the full matrix.
BOT_SIGNATURE = frozenset({'src/pricing.json', 'backend/constants.py'})

# The comment-only class's shape: the suffixes the classifier can
# tokenize, the cap bounding the read cost, and the env naming where
# espree resolves from (CI installs it into an isolated prefix).
SOURCE_SUFFIXES = ('.py', '.js', '.jsx')
MAX_COMMENT_ONLY_FILES = 30
ESPREE_DIR_ENV = 'CLAUDIT_ESPREE_DIR'

# The workflow whose runs decide the verified base on a push, and the
# two jobs whose conclusions say nothing about whether the legs ran:
# the classifier itself, and the fold that always reports.
GATE_WORKFLOW_ID = 'ci-gate.yml'
NON_LEG_JOBS = frozenset({'classify', 'aggregate'})

# The verified-base walk reads the runs list newest first, one page of
# 100 — several days of master history at this repo's pace. A base not
# found within the page over-runs, never walks without bound.
RUNS_PAGE_SIZE = 100

# The jq shapes the walk reads through `_read`: one line per run, and
# one line per job with its conclusion ('-' where the API has none).
_RUNS_JQ = (r'.workflow_runs[] | "\(.head_sha) \(.status) '
            r'\(.conclusion // "-") \(.id)"')
_JOBS_JQ = r'.jobs[] | "\(.name) \(.conclusion // "-")"'

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


def touches_setup_docs(paths):
    """Whether `paths` names any pinned setup doc (see SETUP_DOC_PATTERNS)."""
    return any(path in SETUP_DOC_PATTERNS for path in paths)


def documentation_only(paths):
    """Whether `paths` is a nonempty all-documentation set the gate may
    narrow over.

    A pinned setup doc is excluded however documentation-shaped it is: the
    pin that reads those files lives in the tests leg, so classifying such
    a change docs-only skips the very leg that would fail it (issue #477).
    """
    return (bool(paths) and not touches_setup_docs(paths)
            and all(is_documentation(path) for path in paths))


def data_only(paths):
    """Whether `paths` is exactly the refresh bot's push signature."""
    return bool(paths) and set(paths) == BOT_SIGNATURE


def _espree_dir():
    """The espree module dir the JS tokenizer requires from: the repo's
    own node_modules by default, ESPREE_DIR_ENV (CI's isolated prefix)
    when set."""
    return os.environ.get(ESPREE_DIR_ENV) or os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), 'node_modules')


def _comment_stripped_py(text):
    """A Python source's `(type, string)` tokens minus COMMENT and NL, or
    None. Dropping those makes a comment-only edit compare equal while
    any real edit — a docstring's STRING token included — compares
    unequal; a tokenizer or parse failure answers None, which over-runs."""
    try:
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
        return tuple((token.type, token.string) for token in tokens
                     if token.type not in (tokenize.COMMENT, tokenize.NL))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None


def _js_token_stream(text, path, run):
    """A JS/JSX source's comment-stripped espree tokens, or None. Writes
    the text to a temp file and shells the node helper out through `run`,
    which prints the `[type, value]` list as JSON; any failure — node
    missing, espree absent, a parse error — answers None and over-runs."""
    helper = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          'js_comment_tokens.mjs')
    try:
        with tempfile.NamedTemporaryFile(
                mode='w', encoding='utf-8', suffix='.js') as handle:
            handle.write(text)
            handle.flush()
            stdout = run(['node', helper, _espree_dir(), handle.name,
                          'jsx' if path.endswith('.jsx') else 'plain'])
        return tuple(tuple(token) for token in json.loads(stdout))
    except Exception:  # any failure over-runs, never under-runs
        return None


def _source_tokens(text, path, run):
    """The comment-stripped token stream of a source file, or None."""
    if path.endswith('.py'):
        return _comment_stripped_py(text)
    if path.endswith(('.js', '.jsx')):
        return _js_token_stream(text, path, run)
    return None


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


def _legs_ran(repository, run_id, run):
    """Whether run `run_id` executed a gate leg, or None when unreadable.

    A leg is any job other than the classifier and the aggregate, and
    it executed when its conclusion names an outcome: `skipped`, a
    missing conclusion and a line that cannot be parsed are no evidence
    of execution, and a failed read answers None, which over-runs.
    """
    # `--paginate` makes the read complete whatever the run's job count
    # (the default first page is 30), and `per_page=100` batches it at the
    # API's per-page maximum so completeness costs fewer requests.
    lines = _read(run, [
        'gh', 'api', '--paginate', '-H', 'Cache-Control: no-cache',
        f'repos/{repository}/actions/runs/{run_id}/jobs?per_page=100',
        '--jq', _JOBS_JQ])
    if lines is None:
        return None
    ran = False
    for line in lines:
        name, _, conclusion = line.rpartition(' ')
        if not name or name in NON_LEG_JOBS:
            continue
        if conclusion not in ('-', 'skipped'):
            ran = True
    return ran


def _verified_base(repository, run):
    """The newest master commit whose ci-gate run passed its legs.

    Walks the ci-gate runs on master newest-first (one page of
    RUNS_PAGE_SIZE) for the first run that concluded `success` and
    executed at least one leg; returns its head SHA, or None — run the
    full gate set — when the runs cannot be read, no entry qualifies,
    or any read fails. A run that concluded `failure` or `timed_out`
    is skipped the way a cancelled one is: its legs executed, so it is
    evidence the code was exercised, not that it passed, and narrowing
    over it would let a docs-only push go green above failing code.
    The run in progress for THIS push is never completed, so it is
    skipped naturally.
    """
    lines = _read(run, [
        'gh', 'api', '-H', 'Cache-Control: no-cache',
        f'repos/{repository}/actions/workflows/{GATE_WORKFLOW_ID}/runs'
        f'?branch=master&per_page={RUNS_PAGE_SIZE}', '--jq', _RUNS_JQ])
    if lines is None:
        return None
    for line in lines:
        fields = line.split()
        if len(fields) != 4 or not _hex40(fields[0]):
            return None
        head_sha, status, conclusion, run_id = fields
        if not run_id.isascii() or not run_id.isdigit():
            return None
        if status != 'completed' or conclusion != 'success':
            continue
        legs = _legs_ran(repository, run_id, run)
        if legs is None:
            return None
        if legs:
            return head_sha
    return None


def _push_paths(repository, event, run):
    """The push event's changed paths, or None when they cannot be read.

    The range starts at the verified base (see `_verified_base`), not
    at `before`: the newest master commit whose gate legs executed. A
    push whose predecessor run was cancelled by this very push then
    classifies over the cancelled run's code instead of reading it as
    verified.
    """
    before, sha = event.get('before'), event.get('sha')
    if not (_hex40(before) and _hex40(sha)) or before == '0' * 40:
        return None
    base = _verified_base(repository, run)
    if base is None:
        return None
    # A rename reports ONE row — filename is the new path, and the old
    # path arrives as previous_filename; projecting only .filename read
    # backend/auth.py -> backend/auth.md as a docs-only change. Both
    # paths count, so a rename into or out of documentation runs the
    # legs.
    return _read(run, [
        'gh', 'api', '-H', 'Cache-Control: no-cache',
        f'repos/{repository}/compare/{base}...{sha}', '--jq',
        '.files[] | .filename, (.previous_filename // empty)'],
        cap=COMPARE_FILES_CAP)


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
        # As on the push path: a rename's old path counts beside its new
        # one (issue #245).
        return _read(run, [
            'gh', 'api', '--paginate', '-H', 'Cache-Control: no-cache',
            f'repos/{repository}/pulls/{number}/files', '--jq',
            '.[] | .filename, (.previous_filename // empty)'],
            cap=PULL_REQUEST_FILES_CAP)
    if name == 'push':
        return _push_paths(repository, event, run)
    return None


def _blob(repository, path, ref, run):
    """A path's raw file text at `ref`, or None when it cannot be read."""
    try:
        return run(['gh', 'api', '-H', 'Accept: application/vnd.github.raw',
                    f'repos/{repository}/contents/{path}?ref={ref}'])
    except Exception:  # any read failure over-runs, never under-runs
        return None


def _refs(event, repository, run):
    """The (base, head) commit pair to compare blobs at, or None. A push
    compares its verified base (the same one the changed-path read uses)
    against its own SHA; a pull request reads its base and head SHAs.
    Both are 40-hex, and anything unreadable answers None."""
    name = event.get('name')
    if name == 'push':
        base, sha = _verified_base(repository, run), event.get('sha')
        return (base, sha) if base is not None and _hex40(sha) else None
    if name != 'pull_request':
        return None
    number = event.get('pull_request')
    if not (isinstance(number, str) and number.isascii()
            and number.isdigit()):
        return None
    try:
        line = run([
            'gh', 'api', '-H', 'Cache-Control: no-cache',
            f'repos/{repository}/pulls/{number}',
            '--jq', '.base.sha + " " + .head.sha']).strip()
    except Exception:
        return None
    parts = line.split()
    if len(parts) != 2 or not all(_hex40(part) for part in parts):
        return None
    return (parts[0], parts[1])


def comment_only(paths, event, run):
    """Whether `paths` is a comment-only change over the source files.
    Every path must be a source file, the set must fit
    MAX_COMMENT_ONLY_FILES, and each path's base and head blob must both
    read, DIFFER as text (a no-op or mode-only change is not comment-only)
    and strip to an identical token stream. Any failure answers False,
    the over-run."""
    if not paths or len(paths) > MAX_COMMENT_ONLY_FILES:
        return False
    if not all(path.endswith(SOURCE_SUFFIXES) for path in paths):
        return False
    repository = event.get('repository')
    refs = _refs(event, repository, run) if repository else None
    if refs is None:
        return False
    base, head = refs
    for path in paths:
        base_text = _blob(repository, path, base, run)
        head_text = _blob(repository, path, head, run)
        if base_text is None or head_text is None or base_text == head_text:
            return False
        base_tokens = _source_tokens(base_text, path, run)
        head_tokens = _source_tokens(head_text, path, run)
        if (base_tokens is None or head_tokens is None
                or base_tokens != head_tokens):
            return False
    return True


def classify(event, run):
    """Return (docs_only, data_only, comment_only, reason)."""
    paths = changed_paths(event, run)
    if paths is None:
        return (False, False, False,
                'could not read the changed paths; running the '
                'full gate set')
    if documentation_only(paths):
        return (True, False, False,
                f'documentation-only change: {len(paths)} paths')
    if touches_setup_docs(paths) and all(
            is_documentation(path) for path in paths):
        setup = sorted({path for path in paths
                        if path in SETUP_DOC_PATTERNS})
        return (False, False, False,
                f'documentation-only change touching the pinned setup '
                f'docs ({"; ".join(setup)}): running the matrix so the '
                f'setup-docs pin fails on this push, not the next '
                f'code-touching one')
    if data_only(paths):
        return (False, True, False,
                f'bot-data-only change: {len(paths)} paths')
    if comment_only(paths, event, run):
        return (False, False, True,
                f'comment-only change: {len(paths)} source files')
    outside = sum(1 for path in paths if not is_documentation(path))
    return (False, False, False, f'{len(paths)} paths changed, {outside} '
            'outside documentation')


def event_from_environment(environ):
    """Build the `event` mapping from a process environment."""
    return {
        'name': environ.get('GITHUB_EVENT_NAME', ''),
        'repository': environ.get('GITHUB_REPOSITORY'),
        'sha': environ.get('GITHUB_SHA'),
        'pull_request': environ.get('PR_NUMBER'),
        'before': environ.get('BEFORE_SHA'),
    }


def write_outputs(path, documentation, data, comment, reason):
    """Append the step outputs to the file `path` names."""
    if not path:
        return
    with open(path, 'a', encoding='utf-8') as handle:
        handle.write(f"docs_only={'true' if documentation else 'false'}\n")
        handle.write(f"data_only={'true' if data else 'false'}\n")
        handle.write(f"comment_only={'true' if comment else 'false'}\n")
        handle.write(f'reason={reason}\n')


def main():
    """Classify this run and record the outputs. Returns an exit status."""
    event = event_from_environment(os.environ)

    def run(command):
        return subprocess.run(
            command, capture_output=True, text=True, check=True,
            timeout=60).stdout

    documentation, data, comment, reason = classify(event, run)
    write_outputs(os.environ.get('GITHUB_OUTPUT'), documentation, data,
                  comment, reason)
    print(reason)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
