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
SUCCESSOR = "Push the next -dev VERSION commit to master"
POST_RELEASE = "Verify the release and decide its successor"
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
    "1:api repos/$CLAUDIT_REPO/git/ref/tags/v${TAG_VERSION:-$CLAUDIT_VERSION}")
      printf '%s\n' "$TAG_REPLY"; return "$TAG_RC" ;;
    "2:api repos/$CLAUDIT_REPO/releases/tags/v$CLAUDIT_VERSION")
      printf '%s\n' "$RELEASE_REPLY"; return "$RELEASE_RC" ;;
    *) echo "unmodelled gh call: $*" >&2; return 99 ;;
  esac
}
'''
POST_RELEASE_STUB = r'''
gh() {
  local n
  n="$(cat "$STUB_CALLFILE")"
  n=$((n + 1))
  printf '%s' "$n" > "$STUB_CALLFILE"
  case "$n:$*" in
    "1:api -H Accept: application/vnd.github.raw repos/$CLAUDIT_REPO/contents/VERSION?ref=$CLAUDIT_SHA")
      printf '%s\n' "$VERSION_REPLY"; return "${VERSION_RC:-0}" ;;
    "2:api -H Accept: application/vnd.github.raw repos/$CLAUDIT_REPO/contents/VERSION?ref=refs/heads/master")
      printf '%s\n' "$TIP_REPLY"; return "${TIP_RC:-0}" ;;
    "3:api repos/$CLAUDIT_REPO/commits?path=VERSION&per_page=30 --jq .[].sha")
      printf '%s\n' "$CANDIDATES"; return "${LIST_RC:-0}" ;;
    *":api -H Accept: application/vnd.github.raw repos/$CLAUDIT_REPO/contents/VERSION?ref=release-head")
      printf '%s\n' "$INTRODUCER_REPLY"; return "${CANDIDATE_RC:-0}" ;;
    *":api -H Accept: application/vnd.github.raw repos/$CLAUDIT_REPO/contents/VERSION?ref=newer-head")
      printf '%s\n' '9.9.8-dev'; return "${CANDIDATE_RC:-0}" ;;
    *":api repos/$CLAUDIT_REPO/git/ref/tags/v$PROBE_VERSION")
      printf '%s\n' "$TAG_REPLY"; return "$TAG_RC" ;;
    *":api repos/$CLAUDIT_REPO/releases/tags/v$PROBE_VERSION")
      printf '%s\n' "$RELEASE_REPLY"; return "$RELEASE_RC" ;;
    *":api -H Accept: application/vnd.github.raw repos/$CLAUDIT_REPO/contents/scripts/ci/release_dispatch.py?ref=$CLAUDIT_SELECTOR_SHA")
      cat "$HELPER_PATH"; return "${HELPER_RC:-0}" ;;
    *) echo "unmodelled gh call: $*" >&2; return 99 ;;
  esac
}
'''
SSH_STUB = '''
ssh-agent() { :; }
ssh-add() { :; }
ssh() { :; }
kill() { test "$1" = "$SSH_AGENT_PID"; }
gh() { echo "unexpected gh call in keyed step: $*" >&2; return 99; }
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
        env={"PATH": os.environ["PATH"], "CLAUDIT_REPO": "example/claudit",
             "GITHUB_OUTPUT": os.devnull,
             "CLAUDIT_VERSION": "9.9.9", **env},
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
    return {"CLAUDIT_PUSH_REMOTE": str(remote), "CLAUDIT_MASTER_PUSH_DEPLOY_KEY": "dummy",
            "CLAUDIT_VERSION": "9.9.9", "CLAUDIT_SUCCESSOR": "9.9.10-dev",
            "GITHUB_OUTPUT": str(tmp_path / "output"), "HOME": str(home),
            "RUNNER_TEMP": str(runner), "SSH_AGENT_PID": "999999999"}


@pytest.fixture(name="decide_env")
def _decide_env(tmp_path):
    calls = tmp_path / "calls"
    calls.write_text("0", encoding="utf-8")
    return {
        "STUB_CALLFILE": str(calls), "VERSION_REPLY": "9.9.9",
        "TIP_REPLY": "9.9.9", "CANDIDATES": "release-head\nnewer-head",
        "INTRODUCER_REPLY": "9.9.9",
        "PROBE_VERSION": "9.9.9", "TAG_RC": "0", "TAG_REPLY": "{}",
        "RELEASE_RC": "0", "RELEASE_REPLY": "{}",
        "CLAUDIT_SHA": "release-head", "CLAUDIT_SELECTOR_SHA": "workflow-head",
        "CLAUDIT_RELEASE_CONCLUSION": "success",
        "HELPER_PATH": str(ROOT / "scripts/ci/release_dispatch.py"),
        "GITHUB_OUTPUT": str(tmp_path / "output"), "RUNNER_TEMP": str(tmp_path),
    }


def test_dispatch_shape():
    workflow = _load()
    assert workflow["name"] == "release dispatch"
    assert set(workflow["on"]) == {"workflow_dispatch", "workflow_run"}
    assert workflow["on"]["workflow_run"] == {
        "workflows": ["release"], "types": ["completed"],
    }
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"version"}
    assert inputs["version"]["required"] == "true"
    assert inputs["version"]["type"] == "string"
    assert workflow["permissions"] == {"contents": "read", "actions": "read"}
    assert workflow["concurrency"] == {
        "group": "${{ github.event_name == 'workflow_dispatch' &&\n"
                 "    'release-dispatch' || 'release-dispatch-successor' }}",
        "cancel-in-progress": "false",
    }
    jobs = workflow["jobs"]
    assert set(jobs) == {"validate", "bump", "decide", "successor"}
    assert "needs" not in jobs["validate"]
    assert jobs["bump"]["needs"] == "validate"
    assert jobs["successor"]["needs"] == ["validate", "bump", "decide"]
    for name in ("validate", "bump"):
        assert jobs[name]["if"] == "github.event_name == 'workflow_dispatch'"
    assert jobs["successor"]["if"] == (
        "always() && github.event_name == 'workflow_run' && needs.decide.result == 'success'")
    assert jobs["decide"]["if"] == "always() && github.event_name == 'workflow_run'"
    assert "needs" not in jobs["decide"]
    assert "environment" not in jobs["decide"]
    assert len(jobs["decide"]["steps"]) == 1
    assert jobs["decide"]["steps"][0] == _step(POST_RELEASE)
    assert jobs["decide"]["outputs"] == {
        "version": "${{ steps.decide.outputs.version }}",
        "successor": "${{ steps.decide.outputs.successor }}",
    }
    assert _step(POST_RELEASE)["id"] == "decide"
    assert _step(POST_RELEASE)["env"] == {
        "GH_TOKEN": "${{ github.token }}", "CLAUDIT_REPO": "${{ github.repository }}",
        "CLAUDIT_SHA": "${{ github.event.workflow_run.head_sha }}",
        "CLAUDIT_SELECTOR_SHA": "${{ github.workflow_sha }}",
        "CLAUDIT_RELEASE_CONCLUSION": "${{ github.event.workflow_run.conclusion }}",
    }
    for name, minutes in [("validate", "10"), ("bump", "15"),
                          ("decide", "10"), ("successor", "15")]:
        assert jobs[name]["runs-on"] == "ubuntu-latest"
        assert jobs[name]["timeout-minutes"] == minutes
        assert jobs[name]["permissions"] == (
            {"contents": "read"} if name in {"bump", "decide", "successor"}
            else {"contents": "read", "actions": "read"})
    assert jobs["bump"]["outputs"] == {"sha": "${{ steps.push.outputs.sha }}"}
    assert jobs["validate"]["outputs"] == {
        "successor": "${{ steps.decide.outputs.successor }}",
    }
    assert _step(DECIDE)["id"] == "decide"
    assert _step(SUCCESSOR)["env"] == {
        "CLAUDIT_MASTER_PUSH_DEPLOY_KEY": "${{ secrets.MASTER_PUSH_DEPLOY_KEY }}",
        "CLAUDIT_REPO": "${{ github.repository }}",
        "CLAUDIT_VERSION": "${{ needs.decide.outputs.version }}",
        "CLAUDIT_SUCCESSOR": "${{ needs.decide.outputs.successor }}",
    }
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
        assert "gh " not in step["run"]
        content = "CLAUDIT_VERSION" if name == "bump" else "CLAUDIT_SUCCESSOR"
        assert f"printf '%s\\n' \"${content}\" > VERSION" in step["run"]
        assert step["env"]["CLAUDIT_MASTER_PUSH_DEPLOY_KEY"] == (
            "${{ secrets.MASTER_PUSH_DEPLOY_KEY }}")
        assert step["env"]["CLAUDIT_REPO"] == "${{ github.repository }}"
        if name == "bump":
            assert step["env"]["CLAUDIT_VERSION"] == "${{ inputs.version }}"
    checkouts = [step for job in jobs.values() for step in job["steps"]
                 if "uses" in step]
    assert len(checkouts) == 1
    assert "scripts/ci/release_dispatch.py decide" in _step(DECIDE)["run"]
    assert _step(DECIDE)["env"] == {
        "GH_TOKEN": "${{ github.token }}", "CLAUDIT_REPO": "${{ github.repository }}",
        "CLAUDIT_VERSION": "${{ inputs.version }}",
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
@pytest.mark.parametrize("version,expected", [
    ("9.9.9", "9.9.10-dev"),
    ("9.9.9007199254740992", "9.9.9007199254740993-dev"),
    ("９.９.９", None),
])
def test_validate_emits_exact_successor_or_refuses(tmp_path, version, expected):
    calls = tmp_path / "calls"
    calls.write_text("0", encoding="utf-8")
    output = tmp_path / "output"
    proc = _run_step(DECIDE, PROBE_STUB, {
        "STUB_CALLFILE": str(calls), "TAG_RC": "1", "RELEASE_RC": "1",
        "TAG_REPLY": "HTTP 404", "RELEASE_REPLY": "HTTP 404",
        "CLAUDIT_VERSION": version, "GITHUB_OUTPUT": str(output),
    })
    if expected is None:
        assert proc.returncode == 1
        assert "X.Y.Z" in proc.stdout + proc.stderr
        assert not output.exists()
    else:
        assert proc.returncode == 0, proc.stderr
        assert output.read_text(encoding="utf-8") == f"successor={expected}\n"
        remote, _ = _seed(tmp_path, version)
        env = _push_env(tmp_path, remote) | {
            "CLAUDIT_VERSION": version, "CLAUDIT_SUCCESSOR": expected,
        }
        pushed = _run_step(SUCCESSOR, SSH_STUB, env, cwd=tmp_path)
        assert pushed.returncode == 0, pushed.stderr
        assert subprocess.check_output(
            ["git", "-C", str(remote), "show", "master:VERSION"]) == (
                expected + "\n").encode()


@ubuntu_step_body
def test_validate_stops_when_successor_helper_fails(tmp_path):
    calls = tmp_path / "calls"
    calls.write_text("0", encoding="utf-8")
    output = tmp_path / "output"
    stub = PROBE_STUB + '''
python3() {
  case "$1:$2" in
    scripts/ci/release_dispatch.py:decide) command python3 "$@" ;;
    scripts/ci/release_dispatch.py:successor)
      printf '%s\\n' incomplete
      echo 'successor helper failed' >&2
      return 1 ;;
    *) echo "unmodelled python3 call: $*" >&2; return 99 ;;
  esac
}
'''
    proc = _run_step(DECIDE, stub, {
        "STUB_CALLFILE": str(calls), "TAG_RC": "1", "RELEASE_RC": "1",
        "TAG_REPLY": "HTTP 404", "RELEASE_REPLY": "HTTP 404",
        "GITHUB_OUTPUT": str(output),
    })
    assert proc.returncode == 1
    assert "is free" in proc.stdout
    assert "successor helper failed" in proc.stderr
    assert not output.exists()


@ubuntu_step_body
def test_decide_skips_dev_version_without_outputs(decide_env):
    env = decide_env | {"VERSION_REPLY": "9.9.10-dev",
                        "CLAUDIT_RELEASE_CONCLUSION": "failure"}
    proc = _run_step(POST_RELEASE, POST_RELEASE_STUB, env)
    assert proc.returncode == 0, proc.stderr
    assert "nothing to do" in proc.stdout
    assert not Path(env["GITHUB_OUTPUT"]).exists()
    assert Path(env["STUB_CALLFILE"]).read_text(encoding="utf-8") == "1"


@ubuntu_step_body
@pytest.mark.parametrize("version", ["", "v9.9.9", "9.9", "9.9.9!", "９.９.９"])
def test_decide_refuses_non_semver(decide_env, version):
    env = decide_env | {"VERSION_REPLY": version}
    proc = _run_step(POST_RELEASE, POST_RELEASE_STUB, env)
    assert proc.returncode == 1
    assert "VERSION is not semver" in proc.stderr
    assert not Path(env["GITHUB_OUTPUT"]).exists()
    assert Path(env["STUB_CALLFILE"]).read_text(encoding="utf-8") == "1"


@ubuntu_step_body
@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "skipped", ""])
def test_decide_refuses_unsuccessful_release(decide_env, conclusion):
    env = decide_env | {"CLAUDIT_RELEASE_CONCLUSION": conclusion}
    proc = _run_step(POST_RELEASE, POST_RELEASE_STUB, env)
    assert proc.returncode == 1
    assert f"the release run concluded {conclusion} while VERSION is 9.9.9" in proc.stderr
    assert not Path(env["GITHUB_OUTPUT"]).exists()
    assert Path(env["STUB_CALLFILE"]).read_text(encoding="utf-8") == "4"


@ubuntu_step_body
def test_decide_refuses_missing_tag(decide_env):
    env = decide_env | {"TAG_RC": "1", "TAG_REPLY": "HTTP 404"}
    proc = _run_step(POST_RELEASE, POST_RELEASE_STUB, env)
    assert proc.returncode == 1
    assert "the release for v9.9.9 did not happen" in proc.stderr
    assert not Path(env["GITHUB_OUTPUT"]).exists()


@ubuntu_step_body
@pytest.mark.parametrize("reply", ["HTTP 403", "HTTP 503"])
def test_decide_tag_probe_fails_closed(decide_env, reply):
    env = decide_env | {"TAG_RC": "1", "TAG_REPLY": reply}
    proc = _run_step(POST_RELEASE, POST_RELEASE_STUB, env)
    assert proc.returncode == 1
    assert "tag probe for v9.9.9 — failing closed" in proc.stderr
    assert not Path(env["GITHUB_OUTPUT"]).exists()


@ubuntu_step_body
def test_decide_refuses_tag_without_release(decide_env):
    env = decide_env | {"RELEASE_RC": "1", "RELEASE_REPLY": "HTTP 404"}
    proc = _run_step(POST_RELEASE, POST_RELEASE_STUB, env)
    assert proc.returncode == 1
    assert "the release for v9.9.9 did not happen" in proc.stderr
    assert not Path(env["GITHUB_OUTPUT"]).exists()


@ubuntu_step_body
@pytest.mark.parametrize("reply", ["HTTP 403", "HTTP 503"])
def test_decide_release_probe_fails_closed(decide_env, reply):
    env = decide_env | {"RELEASE_RC": "1", "RELEASE_REPLY": reply}
    proc = _run_step(POST_RELEASE, POST_RELEASE_STUB, env)
    assert proc.returncode == 1
    assert "release probe for v9.9.9 — failing closed" in proc.stderr
    assert not Path(env["GITHUB_OUTPUT"]).exists()


@ubuntu_step_body
def test_decide_emits_exact_outputs_from_real_helper(decide_env):
    env = decide_env | {"VERSION_REPLY": " \t9.9.9007199254740992\n ",
                        "TIP_REPLY": " \t9.9.9007199254740992\n ",
                        "INTRODUCER_REPLY": " \t9.9.9007199254740992\n ",
                        "PROBE_VERSION": "9.9.9007199254740992"}
    proc = _run_step(POST_RELEASE, POST_RELEASE_STUB, env)
    assert proc.returncode == 0, proc.stderr
    assert Path(env["GITHUB_OUTPUT"]).read_text(encoding="utf-8") == (
        "version=9.9.9007199254740992\nsuccessor=9.9.9007199254740993-dev\n")
    assert Path(env["STUB_CALLFILE"]).read_text(encoding="utf-8") == "7"


@ubuntu_step_body
def test_decide_skips_when_master_is_dev(decide_env):
    env = decide_env | {"TIP_REPLY": " \t9.9.10-dev\n ",
                        "CLAUDIT_RELEASE_CONCLUSION": "failure"}
    proc = _run_step(POST_RELEASE, POST_RELEASE_STUB, env)
    assert proc.returncode == 0, proc.stderr
    assert "no release in flight" in proc.stdout
    assert not Path(env["GITHUB_OUTPUT"]).exists()
    assert Path(env["STUB_CALLFILE"]).read_text(encoding="utf-8") == "2"


@ubuntu_step_body
@pytest.mark.parametrize("failure", ["TIP_RC", "LIST_RC", "CANDIDATE_RC"])
def test_decide_attribution_read_fails_closed(decide_env, failure):
    env = decide_env | {failure: "1"}
    proc = _run_step(POST_RELEASE, POST_RELEASE_STUB, env)
    assert proc.returncode == 1
    assert "could not verify" in proc.stderr
    assert "failing closed" in proc.stderr
    assert not Path(env["GITHUB_OUTPUT"]).exists()


@ubuntu_step_body
def test_decide_skips_another_commits_release_before_conclusion(decide_env):
    env = decide_env | {"CLAUDIT_SHA": "another-head",
                        "CLAUDIT_RELEASE_CONCLUSION": "failure"}
    proc = _run_step(POST_RELEASE, POST_RELEASE_STUB, env)
    assert proc.returncode == 0, proc.stderr
    assert "Another commit" in proc.stdout
    assert not Path(env["GITHUB_OUTPUT"]).exists()
    assert Path(env["STUB_CALLFILE"]).read_text(encoding="utf-8") == "4"


@ubuntu_step_body
@pytest.mark.parametrize("candidates", ["", "release-head\nnewer-head"])
def test_decide_refuses_without_a_matching_introducer(decide_env, candidates):
    env = decide_env | {"CANDIDATES": candidates, "TIP_REPLY": "10.0.0"}
    proc = _run_step(POST_RELEASE, POST_RELEASE_STUB, env)
    assert proc.returncode == 1
    assert "introducer" in proc.stderr
    assert "failing closed" in proc.stderr
    assert not Path(env["GITHUB_OUTPUT"]).exists()


@ubuntu_step_body
def test_decide_walks_candidates_newest_first(decide_env):
    env = decide_env | {"CANDIDATES": "newer-head\nrelease-head"}
    proc = _run_step(POST_RELEASE, POST_RELEASE_STUB, env)
    assert proc.returncode == 0, proc.stderr
    assert Path(env["GITHUB_OUTPUT"]).read_text(encoding="utf-8") == (
        "version=9.9.9\nsuccessor=9.9.10-dev\n")
    assert Path(env["STUB_CALLFILE"]).read_text(encoding="utf-8") == "8"


@ubuntu_step_body
@pytest.mark.parametrize("failure", ["VERSION_RC", "HELPER_RC"])
def test_decide_stops_on_failed_contents_fetch(decide_env, failure):
    env = decide_env | {failure: "1"}
    proc = _run_step(POST_RELEASE, POST_RELEASE_STUB, env)
    assert proc.returncode == 1
    assert not Path(env["GITHUB_OUTPUT"]).exists()


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
@pytest.mark.parametrize("version", ["9.9.9", "10.0.0-dev", "9.9.10-dev", "9.9.9-rc.1"])
def test_successor_skips_without_decided_successor(tmp_path, version):
    remote, before = _seed(tmp_path, version)
    env = _push_env(tmp_path, remote) | {"CLAUDIT_SUCCESSOR": ""}
    proc = _run_step(SUCCESSOR, SSH_STUB, env,
                     cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert _git(remote, "rev-parse", "master") == before
    assert "nothing to do" in proc.stdout
    assert not (Path(env["RUNNER_TEMP"]) / "push-repo").exists()


@ubuntu_step_body
@pytest.mark.parametrize("content", [b"9.9.9", b"9.9.9\n\n"])
def test_successor_requires_exact_release_bytes(tmp_path, content):
    remote, _ = _seed(tmp_path, "9.9.9")
    seed = tmp_path / "seed"
    (seed / "VERSION").write_bytes(content)
    _git(seed, "commit", "-am", "Change VERSION bytes")
    _git(seed, "push", "origin", "HEAD:master")
    before = _git(remote, "rev-parse", "master")
    proc = _run_step(SUCCESSOR, SSH_STUB, _push_env(tmp_path, remote),
                     cwd=tmp_path)
    assert proc.returncode == 1
    assert _git(remote, "rev-parse", "master") == before


@ubuntu_step_body
@pytest.mark.parametrize("version", ["", "v9.9.9", "9.9", "9.9.9!", "９.９.９"])
def test_successor_refuses_invalid_master_version(tmp_path, version):
    remote, before = _seed(tmp_path, version)
    env = _push_env(tmp_path, remote)
    proc = _run_step(SUCCESSOR, SSH_STUB, env, cwd=tmp_path)
    assert proc.returncode == 1
    assert "Refusing successor:" in proc.stderr
    assert "expected released '9.9.9'" in proc.stderr
    assert _git(remote, "rev-parse", "master") == before


@ubuntu_step_body
@pytest.mark.parametrize("step,races,race_version", [
    pytest.param(BUMP, 1, None, id="bump-one-race"),
    pytest.param(BUMP, 2, None, id="bump-two-races"),
    pytest.param(BUMP, 3, None, id="bump-exhausted"),
    pytest.param(SUCCESSOR, 1, None, id="successor-one-race"),
    pytest.param(SUCCESSOR, 2, None, id="successor-two-races"),
    pytest.param(SUCCESSOR, 3, None, id="successor-exhausted"),
    pytest.param(SUCCESSOR, 1, "10.0.0-dev", id="newer-version-race"),
    pytest.param(SUCCESSOR, 1, "10.0.0", id="newer-release-race"),
    pytest.param(SUCCESSOR, 1, "9.9.10-dev", id="successor-already-set-race"),
])
def test_push_retries_on_masters_new_tip(tmp_path, step, races, race_version):
    remote, _ = _seed(tmp_path, "9.9.9" if step == SUCCESSOR else "9.9.8-dev")
    seed = tmp_path / "seed"
    tips = []
    for number in range(1, races + 1):
        (seed / "concurrent.txt").write_bytes(f"concurrent change {number}\n".encode())
        _git(seed, "add", "concurrent.txt")
        if race_version is not None:
            (seed / "VERSION").write_bytes((race_version + "\n").encode())
            _git(seed, "add", "VERSION")
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
    if race_version is not None:
        assert proc.returncode == (0 if race_version == "9.9.10-dev" else 1), proc.stderr
        assert _git(remote, "rev-parse", "master") == tips[-1]
        assert (remote / "push-count").read_text(encoding="utf-8") == "1\n"
        if race_version != "9.9.10-dev":
            assert race_version in proc.stderr
            assert "9.9.9" in proc.stderr
        return
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
    env = _push_env(tmp_path, remote) | {"CLAUDIT_MASTER_PUSH_DEPLOY_KEY": ""}
    proc = _run_step(step, SSH_STUB, env, cwd=tmp_path)
    assert proc.returncode == 1
    assert "CLAUDIT_MASTER_PUSH_DEPLOY_KEY is empty" in proc.stderr
    assert _git(remote, "rev-parse", "master") == before
