from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/release-dispatch.yml"
DECIDE = "Probe the tag and the release, then decide"
BUMP = "Push the release VERSION commit to master"
WAIT = "Wait for the release run on the VERSION commit"
SUCCESSOR = "Push the next -dev VERSION commit to master"
ubuntu_step_body = pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="these ubuntu-latest workflow bodies require POSIX bash",
)
PROBE_STUB = r'''
gh() {
  local n
  n="$(cat "$STUB_CALLFILE")"
  n=$((n + 1))
  printf '%s' "$n" > "$STUB_CALLFILE"
  case "$n:$*" in
    "1:api repos/$REPO/git/ref/tags/v$VERSION")
      printf '%s\n' "$TAG_REPLY"; return "$TAG_RC" ;;
    "2:api repos/$REPO/releases/tags/v$VERSION")
      printf '%s\n' "$RELEASE_REPLY"; return "$RELEASE_RC" ;;
    *) echo "unmodelled gh call: $*" >&2; return 99 ;;
  esac
}
'''
SSH_STUB = '''
ssh-agent() { :; }
ssh-add() { :; }
ssh() { :; }
kill() { test "$1" = "$SSH_AGENT_PID"; }
'''


def _load():
    assert WORKFLOW.is_file(), "release dispatch workflow is missing"
    return yaml.load(WORKFLOW.read_text(encoding="utf-8"),
                     Loader=yaml.BaseLoader)


def _step(name: str) -> dict:
    for job in _load()["jobs"].values():
        for step in job["steps"]:
            if step.get("name") == name:
                return step
    raise AssertionError(f"missing step {name!r}")


def _run_step(name: str, stub: str, env: dict[str, str],
              cwd: Path = ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c",
         stub + "\n" + _step(name)["run"]],
        env={"PATH": os.environ["PATH"], "REPO": "example/claudit",
             "VERSION": "9.9.9", **env},
        cwd=cwd, capture_output=True, text=True, encoding="utf-8",
        check=False, timeout=30,
    )


def _git(path: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True,
        text=True, encoding="utf-8", check=True, timeout=30,
    )
    return proc.stdout.strip()


def _seed(tmp_path: Path, version: str = "9.9.8-dev") -> tuple[Path, str]:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare", "--initial-branch=master")
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "--initial-branch=master")
    _git(seed, "config", "user.name", "Test")
    _git(seed, "config", "user.email", "test@example.invalid")
    (seed / "VERSION").write_bytes((version + "\n").encode())
    _git(seed, "add", "VERSION")
    _git(seed, "commit", "-m", "Seed")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "origin", "HEAD:master")
    return remote, _git(remote, "rev-parse", "master")


def _push_env(tmp_path: Path, remote: Path) -> dict[str, str]:
    runner = tmp_path / "runner"
    runner.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    return {"PUSH_REMOTE": str(remote), "MASTER_PUSH_DEPLOY_KEY": "dummy",
            "GITHUB_OUTPUT": str(tmp_path / "output"), "HOME": str(home),
            "RUNNER_TEMP": str(runner), "SSH_AGENT_PID": "999999999"}


def test_dispatch_shape():
    workflow = _load()
    assert workflow["name"] == "release dispatch"
    assert set(workflow["on"]) == {"workflow_dispatch"}
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"version"}
    assert inputs["version"]["required"] == "true"
    assert inputs["version"]["type"] == "string"
    assert workflow["permissions"] == {"contents": "read", "actions": "read"}
    assert workflow["concurrency"] == {"group": "release-dispatch",
                                       "cancel-in-progress": "false"}
    jobs = workflow["jobs"]
    assert set(jobs) == {"validate", "bump", "wait-release", "successor"}
    assert "needs" not in jobs["validate"]
    assert jobs["bump"]["needs"] == "validate"
    assert jobs["wait-release"]["needs"] == "bump"
    assert jobs["successor"]["needs"] == ["validate", "wait-release"]
    for name, minutes in [("validate", "10"), ("bump", "15"),
                          ("wait-release", "100"), ("successor", "15")]:
        assert jobs[name]["runs-on"] == "ubuntu-latest"
        assert jobs[name]["timeout-minutes"] == minutes
        assert jobs[name]["permissions"] == (
            {"contents": "read"} if name in {"bump", "successor"}
            else {"contents": "read", "actions": "read"})
    assert jobs["bump"]["outputs"] == {"sha": "${{ steps.push.outputs.sha }}"}
    assert jobs["bump"]["steps"][0]["id"] == "push"
    assert jobs["validate"]["steps"][0]["name"] == (
        "Refuse a dispatch that is not on master")
    assert jobs["validate"]["steps"][0]["if"] == (
        "${{ github.ref != 'refs/heads/master' }}")


def test_key_jobs_and_checkout_boundary():
    jobs = _load()["jobs"]
    for name in ("bump", "successor"):
        job = jobs[name]
        assert job["environment"] == "master-push"
        assert len(job["steps"]) == 1
        step = job["steps"][0]
        assert "uses" not in step
        assert "python3" not in step["run"]
        content = "VERSION" if name == "bump" else "successor"
        assert f"printf '%s\\n' \"${content}\" > VERSION" in step["run"]
        assert step["env"]["MASTER_PUSH_DEPLOY_KEY"] == (
            "${{ secrets.MASTER_PUSH_DEPLOY_KEY }}")
        assert step["env"]["REPO"] == "${{ github.repository }}"
        assert step["env"]["VERSION"] == "${{ inputs.version }}"
    checkouts = [step for job in jobs.values() for step in job["steps"]
                 if "uses" in step]
    assert len(checkouts) == 2
    assert "scripts/ci/release_dispatch.py decide" in _step(DECIDE)["run"]
    assert "scripts/ci/release_dispatch.py wait-release" in _step(WAIT)["run"]
    assert _step(WAIT)["env"] == {
        "GH_TOKEN": "${{ github.token }}", "REPO": "${{ github.repository }}",
        "SHA": "${{ needs.bump.outputs.sha }}",
    }
    assert _step(DECIDE)["env"] == {
        "GH_TOKEN": "${{ github.token }}", "REPO": "${{ github.repository }}",
        "VERSION": "${{ inputs.version }}",
    }


@ubuntu_step_body
@pytest.mark.parametrize("tag_rc,release_rc,reply,code,message", [
    (1, 1, "HTTP 404", 0, "is free"),
    (0, 1, "HTTP 404", 1, "refusing: tag"),
    (1, 0, "HTTP 404", 1, "refusing: release"),
    (1, 1, "HTTP 403", 1, "failing closed"),
])
def test_probes_decide_with_real_helper(
        tmp_path, tag_rc, release_rc, reply, code, message):
    calls = tmp_path / "calls"
    calls.write_text("0", encoding="utf-8")
    proc = _run_step(DECIDE, PROBE_STUB, {
        "STUB_CALLFILE": str(calls), "TAG_RC": str(tag_rc),
        "RELEASE_RC": str(release_rc), "TAG_REPLY": reply,
        "RELEASE_REPLY": "HTTP 404",
    })
    assert proc.returncode == code, proc.stdout + proc.stderr
    assert message in proc.stdout + proc.stderr


@ubuntu_step_body
def test_release_probe_fails_closed(tmp_path):
    calls = tmp_path / "calls"
    calls.write_text("0", encoding="utf-8")
    proc = _run_step(DECIDE, PROBE_STUB, {
        "STUB_CALLFILE": str(calls), "TAG_RC": "1", "RELEASE_RC": "1",
        "TAG_REPLY": "HTTP 404", "RELEASE_REPLY": "HTTP 503",
    })
    assert proc.returncode == 1
    assert "release probe" in proc.stderr
    assert "failing closed" in proc.stderr


@ubuntu_step_body
def test_bump_sets_requested_version_and_reports_sha(tmp_path):
    remote, before = _seed(tmp_path)
    env = _push_env(tmp_path, remote)
    proc = _run_step(BUMP, SSH_STUB, env, cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    tip = _git(remote, "rev-parse", "master")
    assert tip != before
    assert _git(remote, "rev-list", "--count", f"{before}..master") == "1"
    assert _git(remote, "rev-parse", "master^") == before
    assert subprocess.check_output(
        ["git", "-C", str(remote), "show", "master:VERSION"]) == b"9.9.9\n"
    assert _git(remote, "log", "-1", "--format=%s", "master") == "Release 9.9.9"
    assert Path(env["GITHUB_OUTPUT"]).read_text(encoding="utf-8") == f"sha={tip}\n"


@ubuntu_step_body
def test_rejected_push_with_master_still_fails(tmp_path):
    remote, before = _seed(tmp_path)
    hook = remote / "hooks/update"
    hook.write_text("#!/bin/sh\necho non-fast-forward >&2\nexit 1\n",
                    encoding="utf-8")
    hook.chmod(0o755)
    env = _push_env(tmp_path, remote)
    proc = _run_step(BUMP, SSH_STUB, env, cwd=tmp_path)
    assert proc.returncode == 1
    assert "master stood still" in proc.stderr
    assert _git(remote, "rev-parse", "master") == before
    assert not Path(env["GITHUB_OUTPUT"]).exists()


@ubuntu_step_body
def test_successor_bumps_patch_after_release(tmp_path):
    remote, before = _seed(tmp_path, "9.9.9")
    proc = _run_step(SUCCESSOR, SSH_STUB, _push_env(tmp_path, remote),
                     cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert _git(remote, "rev-parse", "master^") == before
    assert subprocess.check_output(
        ["git", "-C", str(remote), "show", "master:VERSION"]) == b"9.9.10-dev\n"
    assert _git(remote, "log", "-1", "--format=%s", "master") == (
        "Bump VERSION to 9.9.10-dev after the 9.9.9 release")


@ubuntu_step_body
@pytest.mark.parametrize("step", [BUMP, SUCCESSOR])
@pytest.mark.parametrize("races", [1, 2, 3],
                         ids=["one-race", "two-races", "exhausted"])
def test_push_retries_on_masters_new_tip(tmp_path, step, races):
    remote, _ = _seed(tmp_path)
    seed = tmp_path / "seed"
    tips = []
    for number in range(1, races + 1):
        (seed / "concurrent.txt").write_bytes(f"concurrent change {number}\n".encode())
        _git(seed, "add", "concurrent.txt")
        _git(seed, "commit", "-m", f"Concurrent master change {number}")
        tips.append(_git(seed, "rev-parse", "HEAD"))
    _git(remote, "fetch", str(seed), "master")
    hook = remote / "hooks/update"
    hook.write_text('''#!/bin/sh
set -eu
count=0
if [ -f push-count ]; then read -r count < push-count; fi
count=$((count + 1))
printf '%s\\n' "$count" > push-count
if [ "$count" -le "$RACE_COUNT" ]; then
  next="$(printf '%s\\n' "$RACE_TIPS" | sed -n "${count}p")"
  git update-ref refs/heads/master "$next" "$2"
  echo non-fast-forward >&2
  exit 1
fi
''', encoding="utf-8")
    hook.chmod(0o755)
    env = _push_env(tmp_path, remote) | {
        "RACE_COUNT": str(races), "RACE_TIPS": "\n".join(tips),
    }
    proc = _run_step(step, SSH_STUB, env, cwd=tmp_path)
    if races == 3:
        assert proc.returncode == 1, proc.stderr
        assert "master moved during all three push attempts" in proc.stderr
        assert _git(remote, "rev-parse", "master") == tips[-1]
        assert not Path(env["GITHUB_OUTPUT"]).exists()
    else:
        assert proc.returncode == 0, proc.stderr
        assert _git(remote, "rev-parse", "master^") == tips[-1]
    assert (remote / "push-count").read_text(encoding="utf-8") == (
        f"{min(races + 1, 3)}\n")
    assert _git(remote, "show", "master:concurrent.txt") == f"concurrent change {races}"
    assert "non-fast-forward" in proc.stderr
    if races == 3:
        return
    content = b"9.9.9\n" if step == BUMP else b"9.9.10-dev\n"
    assert subprocess.check_output(
        ["git", "-C", str(remote), "show", "master:VERSION"]) == content


@ubuntu_step_body
@pytest.mark.parametrize("step", [BUMP, SUCCESSOR])
def test_empty_key_refuses_before_git(tmp_path, step):
    remote, before = _seed(tmp_path)
    env = _push_env(tmp_path, remote) | {"MASTER_PUSH_DEPLOY_KEY": ""}
    proc = _run_step(step, SSH_STUB, env, cwd=tmp_path)
    assert proc.returncode == 1
    assert "MASTER_PUSH_DEPLOY_KEY is empty" in proc.stderr
    assert _git(remote, "rev-parse", "master") == before
