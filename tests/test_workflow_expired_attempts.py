"""A gh attempt its per-attempt bound ends is retried like any failed attempt,
and a read whose every attempt is ended fails closed.

The step bodies run with `timeout` replaced by a stand-in that ends chosen
attempts the way timeout(1) ends an expired one: status 124, or 137 once
`-k` had to kill, and the command's answer never arrives."""
from __future__ import annotations

import os
import subprocess
from collections import Counter
from pathlib import Path

import pytest

from tests.test_workflow_release import (
    PROCEEDING, PUSH_RUN, REFUSAL_STEP, REFUSAL_STUB, RELEASE_404, TAG_404, _checks,
    _refusal_env, _row, _run, _runs, _run_step as _run_release_step, _step_run)
from tests.test_workflow_release_dispatch import (  # pylint: disable=unused-import
    DECIDE, POST_RELEASE, POST_RELEASE_STUB, PROBE_STUB, _decide_env,
    _run_step as _run_dispatch_step, ubuntu_step_body)
from tests.test_workflow_retried_reads import release_reads
from tests.workflow_checks import (
    ROOT, TIMEOUT_STUB, bounded_calls, find_step, job_of, workflow_by_name)

# version-guard's reads: no published tag, then the pushed commit's facts.
GUARD_STUB = r'''
gh() {
  case "$*" in
    "api repos/$CLAUDIT_REPO/git/ref/tags/v"*) echo "HTTP 404" >&2; return 1 ;;
    *".commit.author.email") echo dev@example.com ;;
    *".files[].filename"*) echo README.md ;;
    *) echo "unmodelled gh call: $*" >&2; return 99 ;;
  esac
}
python3() { echo "decider reached: $*"; }
sleep() { :; }
export -f gh python3 sleep
'''


def _ending(tmp_path: Path, attempts: int, only: str = "", status: str = "124") -> dict[str, str]:
    """Stand-in settings: end the first `attempts` attempts of each command holding `only`."""
    state = tmp_path / "timeout-state"
    state.mkdir()
    return {"TIMEOUT_STATE": str(state), "TIMEOUT_EXPIRE": str(attempts),
            "TIMEOUT_ONLY": only, "TIMEOUT_STATUS": status}


def _bounded(env: dict[str, str]) -> list[str]:
    calls = bounded_calls(Path(env["TIMEOUT_STATE"]))
    assert all(seconds > 0 and command.startswith("gh api ") for seconds, command in calls), calls
    return [command for _, command in calls]


def _guard(env: dict[str, str]) -> subprocess.CompletedProcess:
    step = find_step(job_of(workflow_by_name("version-guard.yml"), "version-guard"),
                     "version_guard.py decide")
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c",
         TIMEOUT_STUB + GUARD_STUB + step["run"]],
        env={"PATH": os.environ["PATH"], "CLAUDIT_REPO": "example/claudit",
             "CLAUDIT_SHA": "0" * 40, "CLAUDIT_EVENT": "push", **env},
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=False, timeout=30)


@ubuntu_step_body
def test_release_reads_retry_an_attempt_the_bound_ended(tmp_path):
    stub, body = release_reads()
    output, env = tmp_path / "output", _ending(tmp_path, 1)
    proc = _run_release_step(
        body, stub + "\nsleep() { :; }; export -f sleep",
        {"STUB_CHECKS": _checks(_row("completed", "success", "aggregate")),
         "STUB_RUNS": _runs(_run(PUSH_RUN)), "GITHUB_OUTPUT": str(output),
         "CLAUDIT_VERSION": "9.9.9", **env})
    assert proc.returncode == 0, proc.stderr
    assert "version=9.9.9" in output.read_text(encoding="utf-8")
    assert "v9.9.9 is free" in proc.stdout
    assert PROCEEDING in proc.stdout
    # Each of the eight reads ran under the bound: ended once, then answered. The
    # two probes' answer is a 404, which the helper asks for on both later attempts.
    assert sorted(Counter(_bounded(env)).values()) == [2] * 6 + [3] * 2


@ubuntu_step_body
def test_decide_reads_retry_an_attempt_the_bound_ended(decide_env, tmp_path):
    env = decide_env | _ending(tmp_path, 1)
    proc = _run_dispatch_step(POST_RELEASE, POST_RELEASE_STUB, env)
    assert proc.returncode == 0, proc.stderr
    assert Path(env["GITHUB_OUTPUT"]).read_text(encoding="utf-8") == (
        "version=9.9.9\nsuccessor=9.9.10-dev\n")
    assert Path(env["STUB_CALLFILE"]).read_text(encoding="utf-8") == "7"
    # Seven reads, two of them one command: six first attempts ended, seven answered.
    commands = _bounded(env)
    assert len(commands) == 13 and len(set(commands)) == 6


def _release_refusal(_tmp_path, env):
    proc = _run_release_step(_step_run(REFUSAL_STEP), REFUSAL_STUB,
                             _refusal_env(1, TAG_404, 1, RELEASE_404) | env)
    return proc, "is free" in proc.stdout


def _validate(tmp_path, env):
    calls, output = tmp_path / "calls", tmp_path / "output"
    calls.write_text("0", encoding="utf-8")
    proc = _run_dispatch_step(DECIDE, PROBE_STUB, {
        "STUB_CALLFILE": str(calls), "TAG_RC": "1", "RELEASE_RC": "1", "TAG_REPLY": "HTTP 404",
        "RELEASE_REPLY": "HTTP 404", "GITHUB_OUTPUT": str(output)} | env)
    return proc, output.exists()


def _decide(tmp_path, env, decide_env):
    proc = _run_dispatch_step(POST_RELEASE, POST_RELEASE_STUB, decide_env | env)
    return proc, (tmp_path / "output").exists()


def _version_guard(_tmp_path, env):
    proc = _guard(env)
    return proc, "decider reached" in proc.stdout


@ubuntu_step_body
@pytest.mark.parametrize("status", ["124", "137"])
@pytest.mark.parametrize("site,only,refusal", [
    (_release_refusal, "git/ref/tags", "the tag probe for v9.9.9"),
    (_release_refusal, "releases/tags", "the release probe for v9.9.9"),
    (_validate, "git/ref/tags", "the tag probe for v9.9.9"),
    (_validate, "releases/tags", "the release probe for v9.9.9"),
    (_decide, "git/ref/tags", "the tag probe for v9.9.9"),
    (_decide, "releases/tags", "the release probe for v9.9.9"),
    (_version_guard, "git/ref/tags", "the tag probe for v"),
    (_version_guard, ".commit.author.email", "the commit author probe for "),
    (_version_guard, ".files[].filename", "the changed-files probe for "),
])
def test_a_probe_whose_every_attempt_the_bound_ended_fails_closed(
        request, tmp_path, site, only, refusal, status):
    env = _ending(tmp_path, 3, only, status)
    extra = (request.getfixturevalue("decide_env"),) if site is _decide else ()
    proc, decided = site(tmp_path, env, *extra)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert f"could not verify {refusal}" in proc.stderr
    assert "failing closed" in proc.stderr
    assert not decided
    # All three attempts of the probe ran under the bound and were ended.
    assert sum(only in command for command in _bounded(env)) == 3


@ubuntu_step_body
def test_version_guard_reaches_its_decider_when_every_read_answers(tmp_path):
    env = _ending(tmp_path, 0)
    proc = _guard(env)
    assert proc.returncode == 0, proc.stderr
    assert "decider reached: scripts/ci/version_guard.py decide" in proc.stdout
    # The 404 probe is asked three times; the two commit reads once each.
    assert sorted(Counter(_bounded(env)).values()) == [1, 1, 3]
