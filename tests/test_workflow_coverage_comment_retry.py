"""The comment job's GitHub API calls retry, bounded and spaced (issue #763).

Run 37521615906 (comment job for PR #761) lost its patch-coverage
comment to ONE malformed `gh api` response ("unexpected end of JSON
input"): the POST exited 1 on its first attempt and no later run
reposted for that pull request. Every `gh api` call in the comment job
therefore runs behind `api_retry`, a shared bounded, spaced retry.

THE SHARED HELPER
- the job's FIRST step writes ONE `api_retry` definition to
  `$RUNNER_TEMP/api_retry.sh`; every step that calls `gh api` sources
  that file, and the definition text appears exactly once in the
  workflow, so the retry is never copied per call;
- every `gh api` call in the job is wrapped, with exactly one
  exception: the POST. A POST is the one non-idempotent call, so it is
  never repeated blindly inside the wrapper — it is retried only by the
  post step's bounded loop, whose every attempt RE-LISTS first, so a
  POST that landed despite reporting an error is found by the next
  listing and PATCHed, never duplicated.

BOUNDED AND SPACED (proven by execution against a fake `gh`, no
network; `sleep` is shadowed and its schedule read from the log)
- a call is attempted at most three times, five then ten seconds apart;
- a retried attempt's stdout is buffered and emitted only on success,
  so a `--paginate` call that dies after emitting one page cannot splice
  a partial page into the output a later attempt returns;
- the wrapper fails when every attempt fails, whatever the attempts'
  individual exit statuses.

THE POST STEP (proven by executing the step's actual script against a
fake `gh` that keeps state)
- a first listing that shows no marker comment POSTs once and reports
  the post;
- a listing that already shows the marker PATCHes it and never POSTs;
- a POST that exits nonzero but landed is PATCHed on the next attempt —
  exactly one POST, exit 0;
- a listing that fails on every attempt fails the step after a bounded
  number of calls.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="executes the workflow's bash against a fake gh; POSIX-bound "
           "— windows runners resolve bare bash to the WSL stub that "
           "exits 1")

ROOT = Path(__file__).resolve().parents[1]
COVERAGE_WF = ROOT / ".github" / "workflows" / "coverage-comment.yml"
JOB = "comment"
HELPER_STEP = "Write the shared API retry helper"
POST_STEP = "Post or update the pull request comment"
SOURCE_LINE = '. "$RUNNER_TEMP/api_retry.sh"'

# Command prefixes under which a `gh api` call counts as wrapped. The
# one sanctioned unwrapped call is the POST, asserted separately.
WRAPPED_PREFIXES = (
    "api_retry",
    "if ! api_retry",
    'if ! current_sha="$(api_retry',
)


def _load():
    # BaseLoader, as in test_workflow_patch_coverage.py: YAML 1.1 reads
    # the bare key `on` as boolean True.
    return yaml.load(COVERAGE_WF.read_text(encoding="utf-8"),
                     Loader=yaml.BaseLoader) or {}


def _steps():
    return _load()["jobs"][JOB]["steps"]


def _step(name):
    for step in _steps():
        if step.get("name") == name:
            return step
    raise AssertionError(f"no step named {name!r}")


def _run(name):
    return _step(name).get("run") or ""


def _logical_commands(run):
    """One string per command: backslash continuations joined."""
    commands = []
    buf = ""
    for raw in run.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        buf = f"{buf} {line}" if buf else line
        if line.endswith("\\"):
            buf = buf[:-1].rstrip()
            continue
        commands.append(buf)
        buf = ""
    if buf:
        commands.append(buf)
    return commands


def _write_helper(tmp_path):
    """Run the helper step's script verbatim; return the written file."""
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir(exist_ok=True)
    subprocess.run(
        ["bash", "-c", _run(HELPER_STEP)],
        cwd=tmp_path, env={**os.environ, "RUNNER_TEMP": str(runner_temp)},
        capture_output=True, text=True, check=True)
    helper = runner_temp / "api_retry.sh"
    assert helper.is_file(), "the helper step wrote no api_retry.sh"
    return helper


def _fake_gh(tmp_path, body):
    """Install a fake `gh` with the given body; return (bin_dir, log)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "gh"
    fake.write_text(body, encoding="utf-8")
    fake.chmod(0o755)
    log = tmp_path / "gh-calls.log"
    return bin_dir, log


def _bash(tmp_path, bin_dir, script, log):
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        "FAKE_LOG": str(log),
    }
    return subprocess.run(
        ["bash", "-c", script], cwd=tmp_path, env=env,
        capture_output=True, text=True, check=False)


def _call_log(log):
    lines = log.read_text(encoding="utf-8").splitlines() if log.exists() \
        else []
    return [" ".join(line.split()) for line in lines]


class TestSharedHelper:
    """One definition, written by the job's first step, sourced by all."""

    def test_the_helper_step_is_the_jobs_first_step(self):
        assert _steps()[0].get("name") == HELPER_STEP

    def test_the_definition_exists_exactly_once(self):
        whole = COVERAGE_WF.read_text(encoding="utf-8")
        assert whole.count("api_retry()") == 1

    def test_every_api_step_sources_the_helper(self):
        for step in _steps():
            code = "\n".join(
                line for line in (step.get("run") or "").splitlines()
                if not line.lstrip().startswith("#"))
            if "gh api" not in code:
                continue
            assert SOURCE_LINE in step["run"], step.get("name")

    def test_every_api_call_is_wrapped_except_the_single_post(self):
        unwrapped = []
        for step in _steps():
            for cmd in _logical_commands(step.get("run") or ""):
                if "gh api" not in cmd:
                    continue
                if cmd.lstrip().startswith(WRAPPED_PREFIXES):
                    continue
                unwrapped.append((step.get("name"), cmd))
        assert len(unwrapped) == 1, unwrapped
        name, cmd = unwrapped[0]
        assert name == POST_STEP
        assert cmd.startswith("if ! gh api -X POST"), cmd

    def test_the_post_loop_re_lists_before_every_attempt(self):
        run = _run(POST_STEP)
        assert "attempts=3" in run
        # The re-list happens inside the bounded loop: the listing CALL
        # (not the pin's own failure echo, which names the same string)
        # is textually after the loop's opening and before the POST.
        loop_at = run.index("attempt=1")
        listing_at = run.index('api_retry "listing the comments on')
        post_at = run.index("gh api -X POST")
        assert loop_at < listing_at < post_at


class TestApiRetry:
    """The wrapper's own contract, against a fake gh failing N times."""

    def _probe(self, tmp_path, fail_times):
        helper = _write_helper(tmp_path)
        script = f'''#!/bin/bash
printf '%s\\n' "$*" >> "$FAKE_LOG"
n="$(cat "{tmp_path}/state" 2>/dev/null || echo 0)"
n="$((n + 1))"
echo "$n" > "{tmp_path}/state"
if [ "$n" -le {fail_times} ]; then
  echo "partial output of attempt $n"
  exit 1
fi
echo "final output"
'''
        bin_dir, log = _fake_gh(tmp_path, script)
        sleep_log = tmp_path / "sleeps.log"
        driver = (
            'sleep() { printf \'%s\\n\' "$1" >> "$SLEEP_LOG"; }\n'
            f'. "{helper}"\n'
            'api_retry "probe call" -- gh api repos/o/r/nothing\n'
        )
        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
            "FAKE_LOG": str(log),
            "SLEEP_LOG": str(sleep_log),
        }
        proc = subprocess.run(
            ["bash", "-c", driver], cwd=tmp_path, env=env,
            capture_output=True, text=True, check=False)
        sleeps = sleep_log.read_text(encoding="utf-8").split() \
            if sleep_log.exists() else []
        calls = _call_log(log)
        return proc, calls, sleeps

    def test_first_try_success_no_retries(self, tmp_path):
        proc, calls, sleeps = self._probe(tmp_path, fail_times=0)
        assert proc.returncode == 0
        assert proc.stdout == "final output\n"
        assert len(calls) == 1
        assert sleeps == []

    def test_one_failure_retried_once_spaced(self, tmp_path):
        proc, calls, sleeps = self._probe(tmp_path, fail_times=1)
        assert proc.returncode == 0
        assert proc.stdout == "final output\n"
        assert len(calls) == 2
        assert sleeps == ["5"]

    def test_two_failures_two_spaced_retries(self, tmp_path):
        proc, calls, sleeps = self._probe(tmp_path, fail_times=2)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == "final output\n"
        assert len(calls) == 3
        assert sleeps == ["5", "10"]

    def test_exhaustion_is_bounded_and_discards_partial_output(
            self, tmp_path):
        proc, calls, sleeps = self._probe(tmp_path, fail_times=99)
        assert proc.returncode == 1
        # Nothing a failed attempt printed reaches the output.
        assert proc.stdout == ""
        assert len(calls) == 3
        assert sleeps == ["5", "10"]
        assert "still failing after 3 attempts" in proc.stderr


class TestPostStep:
    """The post step's script, executed against a stateful fake gh."""

    def _scenario(self, tmp_path, fake_body):
        helper_path = _write_helper(tmp_path)
        bin_dir, log = _fake_gh(tmp_path, fake_body)
        state_dir = tmp_path / "state"
        state_dir.mkdir(exist_ok=True)
        body_md = "### Coverage\n\n- backend/api.py: 92%\n"
        (tmp_path / "body.md").write_text(body_md, encoding="utf-8")
        (tmp_path / "pr-number.txt").write_text("763\n", encoding="utf-8")
        # The driver shadows sleep so the bounded waits cost nothing.
        script = (
            'sleep() { :; }\n'
            + _run(POST_STEP)
        )
        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
            "FAKE_LOG": str(log),
            "FAKE_STATE": str(state_dir),
            "REPO": "Nitjsefnie/claudit",
            "PR_NUMBER": "763",
            "HEAD_COMMIT": "0f1e2d3c4b5a69788796a5b4c3d2e1f00f9e8d7c",
            "GH_TOKEN": "unused-by-the-fake",
            "RUNNER_TEMP": str(helper_path.parent),
        }
        proc = subprocess.run(
            ["bash", "-c", script], cwd=tmp_path, env=env,
            capture_output=True, text=True, check=False)
        return proc, _call_log(log)

    # The fake gh's listing answer: with $FAKE_STATE/landed present, the
    # marker comment exists (id 123); without it, the list is empty.
    LISTING = r'''
case "$*" in
  *"--paginate"*"/comments"*)
    if [ -f "$FAKE_STATE/landed" ]; then
      printf '%s\n' '{"user":{"login":"github-actions[bot]"},"body":"<!-- claudit-diff-coverage --> x","id":123}'
    fi
    exit 0
    ;;
esac
'''

    def test_posts_once_when_the_list_is_empty(self, tmp_path):
        fake = f'''#!/bin/bash
printf '%s\\n' "$*" >> "$FAKE_LOG"
{self.LISTING}
case "$*" in
  *" -X POST "*) exit 0 ;;
esac
exit 1
'''
        proc, calls = self._scenario(tmp_path, fake)
        assert proc.returncode == 0, proc.stderr
        assert "posted a new patch-coverage comment" in proc.stdout
        posts = [c for c in calls if " -X POST " in c]
        patches = [c for c in calls if " -X PATCH " in c]
        assert len(posts) == 1
        assert patches == []

    def test_updates_instead_of_posting_when_the_marker_exists(
            self, tmp_path):
        fake = f'''#!/bin/bash
printf '%s\\n' "$*" >> "$FAKE_LOG"
mkdir -p "$FAKE_STATE"
touch "$FAKE_STATE/landed"
{self.LISTING}
case "$*" in
  *" -X PATCH "*) exit 0 ;;
esac
exit 1
'''
        proc, calls = self._scenario(tmp_path, fake)
        assert proc.returncode == 0, proc.stderr
        assert "updated patch-coverage comment 123" in proc.stdout
        assert len([c for c in calls if " -X POST " in c]) == 0
        assert len([c for c in calls if " -X PATCH " in c]) == 1

    def test_a_post_that_landed_despite_the_error_is_patched_not_duplicated(
            self, tmp_path):
        fake = f'''#!/bin/bash
printf '%s\\n' "$*" >> "$FAKE_LOG"
case "$*" in
  *" -X POST "*)
    # Landed server-side, but reports an error — the incident's shape.
    mkdir -p "$FAKE_STATE"
    touch "$FAKE_STATE/landed"
    exit 1
    ;;
  *" -X PATCH "*) exit 0 ;;
esac
{self.LISTING}
exit 1
'''
        proc, calls = self._scenario(tmp_path, fake)
        assert proc.returncode == 0, proc.stderr
        assert "updated patch-coverage comment 123" in proc.stdout
        posts = [c for c in calls if " -X POST " in c]
        patches = [c for c in calls if " -X PATCH " in c]
        assert len(posts) == 1, calls
        assert len(patches) == 1, calls

    def test_a_permanently_failing_listing_fails_bounded(
            self, tmp_path):
        fake = '''#!/bin/bash
printf '%s\\n' "$*" >> "$FAKE_LOG"
exit 1
'''
        proc, calls = self._scenario(tmp_path, fake)
        assert proc.returncode == 1
        # The wrapper's own three attempts, then the step fails: the
        # loop retries only the POST, not a dead listing.
        assert len(calls) == 3, calls
        assert not [c for c in calls if " -X POST " in c]

    def test_a_permanently_failing_post_fails_bounded(self, tmp_path):
        fake = f'''#!/bin/bash
printf '%s\\n' "$*" >> "$FAKE_LOG"
case "$*" in
  *" -X POST "*) exit 1 ;;
esac
{self.LISTING}
exit 1
'''
        proc, calls = self._scenario(tmp_path, fake)
        assert proc.returncode == 1
        # 3 loop attempts x (1 listing + 1 POST): bounded, and every
        # POST was preceded by its own re-list.
        assert len(calls) == 6, calls
        assert len([c for c in calls if " -X POST " in c]) == 3

    def test_a_transient_patch_failure_is_retried(self, tmp_path):
        fake = f'''#!/bin/bash
printf '%s\\n' "$*" >> "$FAKE_LOG"
mkdir -p "$FAKE_STATE"
touch "$FAKE_STATE/landed"
case "$*" in
  *" -X PATCH "*)
    n="$(cat "$FAKE_STATE/patches" 2>/dev/null || echo 0)"
    n="$((n + 1))"
    echo "$n" > "$FAKE_STATE/patches"
    if [ "$n" -ge 2 ]; then
      exit 0
    fi
    exit 1
    ;;
esac
{self.LISTING}
exit 1
'''
        proc, calls = self._scenario(tmp_path, fake)
        assert proc.returncode == 0, proc.stderr
        assert "updated patch-coverage comment 123" in proc.stdout
        assert len([c for c in calls if " -X PATCH " in c]) == 2
