"""Every bot push to master, run as its real step against a bare remote.

A push GitHub refuses while master stands still is retried: the remote's
update hook refuses a set number of pushes, as GitHub's `fatal error in
commit_refs` does, or applies the first one and still reports it refused.
Every network git call runs under a bounded ssh command, and the ratchet
commit lands under its Conventional Commits subject."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_workflow_refresh_push import (  # pylint: disable=unused-import
    _env, _scratch_fixture, _stage_push_data, ubuntu_run_block)
from tests.test_workflow_release_dispatch import (
    BUMP, SSH_STUB, SUCCESSOR, _git, _push_env, _seed)
from tests.workflow_checks import WORKFLOWS, load_workflow, write_retry_helper

PUSH = "push origin HEAD:master"


def _refuse(remote: Path, refusals: int, applied: bool = False) -> None:
    """Refuse the first `refusals` pushes; `applied` lands the ref first."""
    land = 'git update-ref "$1" "$3" "$2"\n' if applied else ""
    hook = remote / "hooks" / "update"
    hook.write_text(
        "#!/bin/sh\nset -eu\ncount=0\n"
        "if [ -f pushes ]; then read -r count < pushes; fi\n"
        "count=$((count + 1))\nprintf '%s\\n' \"$count\" > pushes\n"
        f'if [ "$count" -le {refusals} ]; then\n{land}'
        "  echo 'fatal error in commit_refs' >&2\n  exit 1\nfi\n",
        encoding="utf-8", newline="\n")
    hook.chmod(0o755)


def _run(workflow: str, name: str, env: dict[str, str],
         cwd: Path) -> subprocess.CompletedProcess:
    """Run the named step as GitHub's bash does, after its job's retry.sh copy."""
    job, = [job for job in load_workflow(WORKFLOWS / workflow)["jobs"].values()
            if any(step.get("name") == name for step in job["steps"])]
    step, = [step for step in job["steps"] if step.get("name") == name]
    write_retry_helper(job, env["RUNNER_TEMP"])
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c",
         SSH_STUB + step["run"]],
        env={"PATH": os.environ["PATH"], "CLAUDIT_REPO": "example/claudit", **env},
        cwd=cwd, capture_output=True, text=True, encoding="utf-8",
        check=False, timeout=60)


def _refresh(request, tmp_path):
    scratch = request.getfixturevalue("scratch")
    _stage_push_data(scratch)
    data = scratch / "runner-temp" / "push-data" / "pricing.json"
    env = _env(scratch, {"CLAUDIT_MASTER_PUSH_DEPLOY_KEY": "test-key",
                         "GITHUB_OUTPUT": str(tmp_path / "output")})
    return (scratch / "remote.git", "src/pricing.json", data.read_text(encoding="utf-8"),
            lambda: _run("refresh-pricing.yml", "Push the tested tree to master",
                         env, scratch / "origin"))


def _ratchet(_request, tmp_path):
    remote, _ = _seed(tmp_path)
    seed = tmp_path / "seed"
    (seed / ".github").mkdir()
    (seed / ".github" / "ci-thresholds.json").write_text("{}\n", encoding="utf-8", newline="\n")
    _git(seed, "add", ".github/ci-thresholds.json")
    _git(seed, "commit", "-m", "Thresholds")
    _git(seed, "push", "origin", "HEAD:master")
    env = _push_env(tmp_path, remote) | {"GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md")}
    data = Path(env["RUNNER_TEMP"]) / "ratchet-data"
    data.mkdir()
    (data / "ci-thresholds.json").write_text('{"tightened": 1}\n', encoding="utf-8", newline="\n")
    (data / "base.txt").write_text(_git(remote, "rev-parse", "master") + "\n",
                                   encoding="utf-8", newline="\n")
    return (remote, ".github/ci-thresholds.json", '{"tightened": 1}\n',
            lambda: _run("ratchet-push.yml", "Push the ratchet commit to master", env, tmp_path))


def _release(step: str, version: str, pushed: str):
    def setup(_request, tmp_path):
        remote, _ = _seed(tmp_path, version)
        env = _push_env(tmp_path, remote)
        return (remote, "VERSION", pushed,
                lambda: _run("release-dispatch.yml", step, env, tmp_path))
    return setup


SITES = {"refresh-pricing": _refresh, "ratchet-push": _ratchet,
         "release-version": _release(BUMP, "9.9.8-dev", "9.9.9\n"),
         "release-successor": _release(SUCCESSOR, "9.9.9", "9.9.10-dev\n")}


def _ssh_reads(command: str) -> dict[str, str]:
    """The options OpenSSH itself reads from an ssh command line, no config file consulted."""
    proc = subprocess.run(["sh", "-c", command + " -F /dev/null -G example.invalid"],
                          capture_output=True, text=True, check=True, timeout=30)
    return dict(line.split(" ", 1) for line in proc.stdout.splitlines() if " " in line)


@ubuntu_run_block
@pytest.mark.skipif(shutil.which("ssh") is None, reason="asks the installed OpenSSH client how it reads the command")
@pytest.mark.parametrize("site", SITES)
def test_every_network_git_call_of_a_push_runs_under_a_bounded_ssh_command(
        request, tmp_path, monkeypatch, site):
    seen, shims = tmp_path / "ssh-seen", tmp_path / "ssh-shims"
    shims.mkdir()
    (shims / "git").write_text(
        '#!/bin/sh\nif [ "$1" = -C ]; then sub="$3"; else sub="$1"; fi\n'
        f'case "$sub" in fetch|ls-remote|push) printf "%s\\t%s\\n" "$sub" "${{GIT_SSH_COMMAND-}}" >> "{seen}" ;; esac\n'
        f'exec "{shutil.which("git")}" "$@"\n', encoding="utf-8", newline="\n")
    (shims / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{shims}{os.pathsep}{os.environ['PATH']}")
    _, _, _, run = SITES[site](request, tmp_path)
    seen.write_text("", encoding="utf-8")  # the fixture's own pushes are not the step's

    proc = run()

    assert proc.returncode == 0, proc.stderr
    calls = [line.split("\t") for line in seen.read_text(encoding="utf-8").splitlines()]
    assert {"fetch", "push"} <= {subcommand for subcommand, _ in calls}
    for command in {command for _, command in calls}:
        read = _ssh_reads(command)
        for bound in ("connecttimeout", "serveraliveinterval", "serveralivecountmax"):
            assert read[bound].isdigit() and int(read[bound]) > 0, (bound, command)
        assert read["stricthostkeychecking"] in ("yes", "true"), command


@ubuntu_run_block
@pytest.mark.parametrize("applied", [False, True], ids=["refused", "applied-but-refused"])
@pytest.mark.parametrize("site", SITES)
def test_a_push_refused_once_while_master_stood_still_lands(request, tmp_path, site, applied):
    remote, path, pushed, run = SITES[site](request, tmp_path)
    before = _git(remote, "rev-parse", "master")
    _refuse(remote, 1, applied)

    proc = run()

    assert proc.returncode == 0, proc.stderr
    assert _git(remote, "rev-parse", "master^") == before
    assert subprocess.check_output(
        ["git", "-C", str(remote), "show", f"master:{path}"], text=True) == pushed
    # An applied push leaves master at the commit, so the retry sends nothing.
    assert (remote / "pushes").read_text(encoding="utf-8") == ("1\n" if applied else "2\n")
    assert f"{PUSH} failed (attempt 1 of 3)" in proc.stderr
    assert f"{PUSH} failed (attempt 2 of 3)" not in proc.stderr
    assert "::notice::" not in proc.stdout


@ubuntu_run_block
@pytest.mark.parametrize("site", SITES)
def test_a_push_refused_on_every_attempt_while_master_stood_still_goes_red(
        request, tmp_path, site):
    remote, _, _, run = SITES[site](request, tmp_path)
    before = _git(remote, "rev-parse", "master")
    _refuse(remote, 99)

    proc = run()

    assert proc.returncode == 1, proc.stderr
    assert "rejected while master stood still" in proc.stderr
    assert f"{PUSH} still failing after 3 attempts" in proc.stderr
    assert (remote / "pushes").read_text(encoding="utf-8") == "3\n"
    assert _git(remote, "rev-parse", "master") == before
    # Only the release VERSION step writes GITHUB_OUTPUT, and only once its push lands.
    assert not (tmp_path / "output").exists()


@ubuntu_run_block
def test_the_ratchet_commit_lands_under_its_conventional_subject(request, tmp_path):
    remote, _, _, run = _ratchet(request, tmp_path)

    proc = run()

    assert proc.returncode == 0, proc.stderr
    assert _git(remote, "log", "-1", "--format=%s", "master") == (
        "chore(ratchet): Ratchet ci-thresholds: raise coverage floor / "
        "tighten baselines (automated)")
