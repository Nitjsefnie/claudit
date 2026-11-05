"""The jobs without a checkout read through their copy of the retry helper:
each executed read fails once, transiently, and the step still succeeds."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from tests.test_workflow_refresh_push import (  # pylint: disable=unused-import
    _run_push_step, _scratch_fixture, _sha, _stage_push_data, _state_lines,
    ubuntu_run_block)
from tests.test_workflow_release import (
    PROCEEDING, PUSH_RUN, WAIT_STEP, WAIT_STUB, _checks, _row, _run, _runs,
    _run_step as _run_release_step, _step_run)
from tests.test_workflow_release_dispatch import (  # pylint: disable=unused-import
    BUMP, POST_RELEASE, POST_RELEASE_STUB, SSH_STUB, SUCCESSOR, _decide_env,
    _git, _push_env, _run_step as _run_dispatch_step, _seed, ubuntu_step_body)

# Wraps a defined stub gh: each distinct call first fails once (a marker file
# in $FLAKY_DIR records it), then reaches the stub; sleep skips the backoff.
FAIL_EACH_GH_READ_ONCE = r'''
eval "stub_$(declare -f gh)"
gh() {
  local key
  key="$FLAKY_DIR/$(printf '%s' "$*" | cksum | cut -d ' ' -f 1)"
  if [ ! -e "$key" ]; then : > "$key"; echo "HTTP 503 (injected)" >&2; return 1; fi
  stub_gh "$@"
}
sleep() { :; }
export -f gh stub_gh sleep
'''
# Every odd-numbered git read fails before reaching git, so each read site
# must retry once to proceed; the count lands in $GIT_READS.
FLAKY_GIT = r'''
case " $* " in
  *" fetch "*|*" ls-remote "*)
    n=$(($(cat "$GIT_READS" 2>/dev/null || echo 0) + 1))
    printf '%s\n' "$n" > "$GIT_READS"
    if [ $((n % 2)) -eq 1 ]; then echo "fatal: unable to access (injected)" >&2; exit 128; fi ;;
esac
'''


@ubuntu_step_body
def test_release_reads_retry_a_transient_failure(tmp_path):
    raw = '"api -H Accept: application/vnd.github.raw repos/$CLAUDIT_REPO/contents/'
    stub = WAIT_STUB.replace(raw + 'scripts', (
        raw + 'VERSION?ref=$CLAUDIT_SHA") echo 9.9.9 ;; '
        '"api repos/$CLAUDIT_REPO/commits/$CLAUDIT_SHA --jq .parents[0].sha") echo parent ;; '
        + raw + 'VERSION?ref=parent") echo 9.9.8-dev ;; '
        '*/tags/$CLAUDIT_TAG) echo "HTTP 404" >&2; return 1 ;; ' + raw + 'scripts'))
    flaky, output = tmp_path / "flaky", tmp_path / "output"
    flaky.mkdir()
    body = "\n".join(_step_run(name) for name in (
        "Read and validate VERSION", "Verify this commit changed VERSION",
        "Refuse to re-release an existing tag", WAIT_STEP))
    proc = _run_release_step(
        body, stub + "\n" + FAIL_EACH_GH_READ_ONCE,
        {"STUB_CHECKS": _checks(_row("completed", "success", "aggregate")),
         "STUB_RUNS": _runs(_run(PUSH_RUN)), "FLAKY_DIR": str(flaky),
         "GITHUB_OUTPUT": str(output), "CLAUDIT_VERSION": "9.9.9"})
    assert proc.returncode == 0, proc.stderr
    assert "version=9.9.9" in output.read_text(encoding="utf-8")
    assert "unchanged" not in output.read_text(encoding="utf-8")
    assert "v9.9.9 is free" in proc.stdout
    assert PROCEEDING in proc.stdout
    assert len(list(flaky.iterdir())) == 8
    # The two probes capture their stderr; the six other reads print theirs.
    assert proc.stderr.count("HTTP 503 (injected)") == 6


@ubuntu_step_body
def test_decide_reads_retry_a_transient_failure(decide_env, tmp_path):
    flaky = tmp_path / "flaky"
    flaky.mkdir()
    proc = _run_dispatch_step(POST_RELEASE, POST_RELEASE_STUB + FAIL_EACH_GH_READ_ONCE,
                              decide_env | {"FLAKY_DIR": str(flaky)})
    assert proc.returncode == 0, proc.stderr
    assert Path(decide_env["GITHUB_OUTPUT"]).read_text(encoding="utf-8") == (
        "version=9.9.9\nsuccessor=9.9.10-dev\n")
    assert Path(decide_env["STUB_CALLFILE"]).read_text(encoding="utf-8") == "7"
    assert len(list(flaky.iterdir())) == 6


@ubuntu_step_body
@pytest.mark.parametrize("step", [BUMP, SUCCESSOR])
@pytest.mark.parametrize("refused", [False, True])
def test_version_push_reads_retry_a_transient_failure(tmp_path, step, refused):
    remote, before = _seed(tmp_path, "9.9.9" if step == SUCCESSOR else "9.9.8-dev")
    if refused:
        hook = remote / "hooks/update"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)
    env = _push_env(tmp_path, remote) | {"GIT_READS": str(tmp_path / "git-reads")}
    flaky_git = 'git() {' + FLAKY_GIT.replace("exit 128", "return 128") + 'command git "$@"; }'
    proc = _run_dispatch_step(step, SSH_STUB + flaky_git + "\nsleep() { :; }\nexport -f git sleep\n",
                              env, cwd=tmp_path)
    reads = (tmp_path / "git-reads").read_text(encoding="utf-8")
    if refused:
        assert proc.returncode == 1
        assert "master stood still" in proc.stderr
        assert _git(remote, "rev-parse", "master") == before
        assert reads == "6\n"
    else:
        assert proc.returncode == 0, proc.stderr
        assert _git(remote, "rev-parse", "master^") == before
        assert reads == "4\n"


@ubuntu_run_block
@pytest.mark.parametrize("raced", [False, True])
def test_pricing_push_reads_retry_a_transient_failure(scratch: Path, raced: bool):
    _stage_push_data(scratch)
    shims = scratch / "flaky-bin"
    shims.mkdir()
    (shims / "git").write_text(f'#!/bin/bash\n{FLAKY_GIT}exec "{scratch}/bin/git" "$@"\n',
                               encoding="utf-8")
    # Records the helper's backoff; the agent shim's own sleep stays real.
    (shims / "sleep").write_text(f'#!/bin/sh\ncase "$1" in 5|10) echo "$1" >> "$FAKE_STATE/sleeps" ;;\n'
                                 f'*) exec {shutil.which("sleep")} "$@" ;; esac\n', encoding="utf-8")
    for shim in shims.iterdir():
        shim.chmod(0o755)
    path = os.pathsep.join([str(shims), str(scratch / "bin"), os.environ["PATH"]])

    proc = _run_push_step(scratch, {"CLAUDIT_MASTER_PUSH_DEPLOY_KEY": "test-key", "PATH": path,
                                    "GIT_READS": str(scratch / "state" / "git-reads"),
                                    "FAKE_RACE_PUSH": "1" if raced else None})

    assert proc.returncode == 0, proc.stderr
    assert _sha(scratch / "remote.git", "master") == (
        _sha(scratch / "peer", "HEAD") if raced
        else _sha(scratch / "runner-temp" / "push-repo", "HEAD"))
    assert _state_lines(scratch, "git-reads") == ["6" if raced else "4"]
    # A raced push fails all three retry.sh attempts (5 s, 10 s) before the re-fetch.
    assert _state_lines(scratch, "sleeps") == (["5", "5", "5", "10", "5"] if raced else ["5", "5"])
