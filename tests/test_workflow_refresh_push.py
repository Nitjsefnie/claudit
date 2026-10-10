"""Workflow shape + push simulation: the pricing bot's key-only push job.

Issue #359: the deploy key that bypasses the required aggregate gate used
to be loaded inside jobs that also ran the suite, unpinned installs and
upstream-response parsing. The contract pinned here: the key is loaded
ONLY by the push job, which receives the tested data as an artifact, runs
no checkout, no pip, no npm, no suite and no upstream parsing before or
while the key is loaded, and stops its ssh-agent at the step's end. The
in-run retry is gone: a push refused because master moved is dropped with
a notice, and the NEXT hourly run re-fetches, re-tests in the keyless
refresh job, and lands it. The verdict job runs with always(), so a
refused fetch turns the run red even when nothing moved and the push
skipped.

The simulation tests execute the push step's own `run:` block under
`bash -e` in a scratch repository whose remote moves mid-run — which is
the race itself. The simulation pre-places the staging directory the
download action would have produced at `$RUNNER_TEMP/push-data`.
"""
from __future__ import annotations

from collections.abc import Mapping
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "refresh-pricing.yml"
PUSH_STEP_NAME = "Push the tested tree to master"

# GitHub's published SSH host keys for github.com; both push steps in the
# repository carry them verbatim.
ED25519_HOST_KEY = "AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl'"
RSA_HOST_KEY = "AAAAB3NzaC1yc2EAAAADAQABAAABgQCj7ndNxQowgcQnjshcLrqPEiiphnt+VTTvDP6mHBL9j1aNUkY4Ue1gvwnGLVlOhGeYrnZaMgRK6+PKCUXaDbC7qtbW8gIkhL7aGCsOr/C56SJMy/BCZfxd1nWzAOxSDPgVsmerOBYfNqltV9/hWCqBywINIR+5dIg6JTJ72pcEpEjcYgXkE2YEFXV1JHnsKgbLWNlhScqb2UmyRkQyytRLtL+38TGxkxCflmO+5Z8CSSNY7GidjMIZ7Q4zMjA2n1nGrlTDkzwDCsw+wqFPGQA179cnfGWOWRVruj16z6XyvxvjJwbz0wQZ75XK5tKSb7FNyeIEs4TT4jk+S4dhPeAUC5y+bDYirYgM4GC7uEnztnZyaVWQ7B381AK4Qdrwt51ZqExKbQpTUNn+EjqoTwvqNj4kqx5QUCI0ThS/YkOxJCXmPUWZbhjpCg56i+2aB6CmK2JGhn57K5mj0MNdBXA4/WnwH6XoPWJzK5Nyu2zB3nAZp+S5hpQs+p1vN1/wsjk='"

BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"

# The shim switches _env pops, so a leak from the outer environment can
# never flip a shim.
FAKE_SWITCHES = ("FAKE_FAIL_GIT_PUSH", "FAKE_RACE_PUSH")

# The simulation execs a POSIX-bash shebang shim from PATH to drive a GitHub
# Actions ubuntu run block — the block itself only ever runs on
# ubuntu-latest, and Windows cannot exec the shim (WinError 193).
ubuntu_run_block = pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="the simulation drives a GitHub Actions ubuntu run block through "
           "POSIX bash and PATH shims; the block only ever runs on "
           "ubuntu-latest",
)


# --- shape: parse the workflow ---------------------------------------------


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _job(job_id: str) -> dict:
    return _workflow()["jobs"][job_id]


def _step(job_id: str, name: str) -> dict:
    matches = [step for step in (_job(job_id).get("steps") or [])
               if step.get("name") == name]
    assert len(matches) == 1, f"expected exactly one step named {name!r}"
    step, = matches
    return step


def _push_run() -> str:
    return _step("push", PUSH_STEP_NAME)["run"]


def _all_steps() -> list[tuple[str, dict]]:
    return [(job_id, step)
            for job_id, job in _workflow()["jobs"].items()
            for step in (job.get("steps") or [])]


def test_jobs_are_exactly_refresh_push_and_verdict():
    assert set(_workflow()["jobs"]) == {"refresh", "push", "verdict"}


def test_refresh_job_stays_the_keyless_suite_runner():
    job = _job("refresh")
    assert job["if"] == "github.ref == 'refs/heads/master'"
    assert job["timeout-minutes"] == 30
    assert job["permissions"] == {"contents": "read"}
    image = job["services"]["postgres"]["image"]
    assert image.startswith("public.ecr.aws/docker/library/postgres:16@sha256:"), image
    assert (job["env"]["PGHOST"], job["env"]["PGPORT"],
            job["env"]["PGUSER"], job["env"]["PGPASSWORD"]) == (
        "localhost", "5432", "postgres", "postgres")


def test_refresh_job_outputs_feed_the_push_gate():
    outputs = _job("refresh")["outputs"]
    assert outputs == {
        "moved": "${{ steps.moved.outputs.moved }}",
        "fetch_rc": "${{ steps.fetch.outputs.rc }}",
    }


def test_the_key_appears_once_in_the_file_and_only_in_the_push_job():
    raw = WORKFLOW.read_text(encoding="utf-8")
    # The only load site in the whole file is the push step's env mapping:
    # the secret is wired exactly once.
    env_lines = [line.strip() for line in raw.splitlines()
                 if "secrets.MASTER_PUSH_DEPLOY_KEY" in line]
    assert env_lines == [
        "MASTER_PUSH_DEPLOY_KEY: ${{ secrets.MASTER_PUSH_DEPLOY_KEY }}"]
    assert raw.count("secrets.MASTER_PUSH_DEPLOY_KEY") == 1
    for job_id in ("refresh", "verdict"):
        for step in (_job(job_id).get("steps") or []):
            blob = repr(step.get("env") or {}) + (step.get("run") or "")
            assert "MASTER_PUSH_DEPLOY_KEY" not in blob, (job_id, step.get("name"))
            assert "git push" not in (step.get("run") or ""), (job_id, step.get("name"))
            assert "ssh-agent" not in (step.get("run") or ""), (job_id, step.get("name"))


def test_refresh_job_stages_and_uploads_the_tested_data():
    stage = _step("refresh", "Stage the tested data")
    assert stage["if"] == "steps.moved.outputs.moved == 'true'"
    run = stage["run"]
    assert 'mkdir -p "$RUNNER_TEMP/push-data"' in run
    assert 'cp src/pricing.json "$RUNNER_TEMP/push-data/pricing.json"' in run
    assert 'cp backend/constants.py "$RUNNER_TEMP/push-data/constants.py"' in run
    assert ('cp "$RUNNER_TEMP/commit-msg.txt" '
            '"$RUNNER_TEMP/push-data/commit-msg.txt"' in run)
    assert 'git rev-parse HEAD > "$RUNNER_TEMP/push-data/base.txt"' in run

    upload = _step("refresh", "Upload the tested data")
    assert upload["if"] == "steps.moved.outputs.moved == 'true'"
    assert upload["uses"].startswith("actions/upload-artifact@"), upload
    # The action's own inputs (name, path, if-no-files-found, retention)
    # are contract: fleet-rules "Merging and CI" with:-inputs ruling — no
    # assertion on them; a legitimate reconfiguration must pass.


def test_push_job_is_the_only_job_that_loads_the_key():
    job = _job("push")
    assert job["needs"] == "refresh"
    assert job["if"] == "needs.refresh.outputs.moved == 'true'"
    assert job["environment"] == "master-push"
    assert job["runs-on"] == "ubuntu-latest"
    assert job["timeout-minutes"] == 15
    assert job["permissions"] == {"contents": "read"}
    steps = job["steps"]
    assert len(steps) == 2
    download, = [step for step in steps if "uses" in step]
    assert download["uses"].startswith(
        "actions/download-artifact@"), download


def test_push_run_block_pins_the_key_preamble_and_host_keys():
    run = _push_run()
    fail_fast = 'if [ -z "${MASTER_PUSH_DEPLOY_KEY:-}" ]; then'
    assert fail_fast in run
    # Fail fast BEFORE anything else: no agent, no network, no git.
    assert run.index(fail_fast) < run.index("ssh-agent")
    assert run.index(fail_fast) < run.index("git ls-remote")
    assert "trap 'kill \"$SSH_AGENT_PID\" 2>/dev/null || true' EXIT" in run
    assert "ssh-add <(printf '%s\\n' \"$MASTER_PUSH_DEPLOY_KEY\")" in run
    assert "StrictHostKeyChecking=yes" in run
    assert ED25519_HOST_KEY in run
    assert RSA_HOST_KEY in run
    assert 'remote="${PUSH_REMOTE:-git@github.com:${REPO}.git}"' in run
    assert WORKFLOW.read_text(encoding="utf-8").count("PUSH_REMOTE") == 1


def test_push_run_block_pins_the_order_and_never_forces_or_tokens():
    run = _push_run()
    assert run.index("git ls-remote") < run.index("git init")
    assert ('cp "$RUNNER_TEMP/push-data/pricing.json" '
            '"$RUNNER_TEMP/push-repo/src/pricing.json"' in run)
    assert ('cp "$RUNNER_TEMP/push-data/constants.py" '
            '"$RUNNER_TEMP/push-repo/backend/constants.py"' in run)
    assert ('git -C "$RUNNER_TEMP/push-repo" '
            'add src/pricing.json backend/constants.py' in run)
    assert ("git -C \"$RUNNER_TEMP/push-repo\" "
            "config user.name 'github-actions[bot]'" in run)
    assert (f'git -C "$RUNNER_TEMP/push-repo" '
            f"config user.email '{BOT_EMAIL}'" in run)
    assert ('git -C "$RUNNER_TEMP/push-repo" '
            'commit -F "$RUNNER_TEMP/push-data/commit-msg.txt"' in run)
    assert 'git -C "$RUNNER_TEMP/push-repo" push origin HEAD:master' in run
    assert "rejected while master stood still" in run
    assert "rev-parse HEAD^" in run
    assert "rev-parse FETCH_HEAD" in run
    assert "--force" not in run
    assert "GH_TOKEN" not in run
    assert "x-access-token" not in WORKFLOW.read_text(encoding="utf-8")


def test_verdict_job_turns_a_refused_fetch_red_even_when_nothing_moved():
    job = _job("verdict")
    assert job["needs"] == ["refresh", "push"]
    assert job["if"] == "${{ always() }}"
    assert job["permissions"] == {}
    assert job["timeout-minutes"] == 15
    step, = job["steps"]
    assert step["name"] == "Fail on a refusal"
    assert step["env"] == {
        "FETCH_RC": "${{ needs.refresh.outputs.fetch_rc }}"}
    run = step["run"]
    assert 'if [ -n "$FETCH_RC" ] && [ "$FETCH_RC" != "0" ]; then' in run
    assert "exit 1" in run


def test_the_push_never_forces():
    for job_id, step in _all_steps():
        run = step.get("run") or ""
        assert "--force" not in run, (job_id, step.get("name"))
        for line in run.splitlines():
            if "git push" in line:
                assert not re.search(r"(?:^|\s)-f(?:\s|$)", line), (
                    job_id, step.get("name"))


# --- the push simulation ---------------------------------------------------


def _run(cmd: list[str], cwd: Path | None = None,
         env: dict[str, str] | None = None,
         check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=None if cwd is None else str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=check,
    )


def _git(repo: Path, *args: str,
         check: bool = True) -> subprocess.CompletedProcess:
    return _run(["git", "-C", str(repo), *args], check=check)


def _sha(repo: Path, *args: str) -> str:
    return _git(repo, "rev-parse", *args).stdout.strip()


def _configure_identity(repo: Path) -> None:
    _git(repo, "config", "user.name", "Test Bot")
    _git(repo, "config", "user.email", "bot@example.invalid")


def _peer_move(scratch: Path, mark: str) -> str:
    """Commit and push a move on the peer clone; return the new tip."""
    peer = scratch / "peer"
    with open(peer / "README.md", "a", encoding="utf-8") as handle:
        handle.write(f"{mark}\n")
    _git(peer, "add", "README.md")
    _git(peer, "commit", "-q", "-m", mark)
    _git(peer, "push", "-q", "origin", "master")
    return _sha(peer, "HEAD")


def _git_shim() -> str:
    # Every git call passes through to the real git; the FAKE switches
    # bend only `git push`, which is the race surface. The push step
    # invokes `git -C <dir> push`, so the subcommand sits after the -C
    # pair.
    return r"""#!/usr/bin/env bash
set -eu
if [ "${1:-}" = "-C" ]; then
  sub="${3:-}"
else
  sub="${1:-}"
fi
if [ "${FAKE_FAIL_GIT_PUSH:-0}" -eq 1 ] && [ "$sub" = "push" ]; then
  exit 1
fi
if [ "${FAKE_RACE_PUSH:-0}" -eq 1 ] && [ "$sub" = "push" ]; then
  (
    cd "$FAKE_PEER" || exit 1
    echo race >> README.md
    git add README.md
    git commit -q -m "race move"
    FAKE_RACE_PUSH=0 FAKE_FAIL_GIT_PUSH=0 git push -q origin master
  )
  exit 1
fi
exec "$REAL_GIT" "$@"
"""


def _ssh_agent_shim() -> str:
    # Return harmless shell assignments for the workflow's eval, without
    # starting a real agent during the simulation. The backgrounded sleep
    # stands in for the agent process the trap must stop.
    return r"""#!/usr/bin/env bash
set -eu
printf 'called\n' > "$FAKE_STATE/ssh-agent-called"
if [ -z "${MASTER_PUSH_DEPLOY_KEY:-}" ]; then
  echo 'ssh-agent called without the deploy key' >&2
  exit 1
fi
sleep 300 >/dev/null 2>&1 &
agent_pid=$!
printf '%s\n' "$agent_pid" > "$FAKE_STATE/ssh-agent-pid"
printf '%s\n' "SSH_AUTH_SOCK=$FAKE_STATE/agent.sock; export SSH_AUTH_SOCK;" \
  "SSH_AGENT_PID=$agent_pid; export SSH_AGENT_PID;"
"""


def _ssh_add_shim() -> str:
    return r"""#!/usr/bin/env bash
set -eu
printf 'called\n' > "$FAKE_STATE/ssh-add-called"
if [ -z "${MASTER_PUSH_DEPLOY_KEY:-}" ]; then
  echo 'ssh-add called without the deploy key' >&2
  exit 1
fi
if [ "$#" -ne 1 ] || [ ! -r "$1" ]; then
  echo 'ssh-add did not receive a readable key file' >&2
  exit 1
fi
bytes=$(wc -c < "$1")
if (( bytes <= 0 )); then
  echo 'ssh-add received an empty key file' >&2
  exit 1
fi
printf '%s\n' "$bytes" > "$FAKE_STATE/ssh-add-bytes"
"""


@pytest.fixture(name="scratch")
def _scratch_fixture(tmp_path: Path) -> Path:
    base = tmp_path / "scratch"
    (base / "bin").mkdir(parents=True)
    (base / "state").mkdir()
    (base / "runner-temp").mkdir()
    (base / "home").mkdir()
    (base / "github-summary.md").touch()
    bare = base / "remote.git"
    _run(["git", "init", "--bare", "-b", "master", str(bare)])
    seed = base / "seed"
    seed.mkdir()
    _run(["git", "init", "-b", "master"], cwd=seed)
    (seed / "backend").mkdir()
    (seed / "src").mkdir()
    (seed / "README.md").write_text("seed\n", encoding="utf-8")
    (seed / "backend" / "constants.py").write_text(
        'PRICING_VERSION = "6"\n', encoding="utf-8")
    (seed / "src" / "pricing.json").write_text("{\n", encoding="utf-8")
    _configure_identity(seed)
    _run(["git", "add", "README.md", "backend/constants.py",
          "src/pricing.json"], cwd=seed)
    _run(["git", "commit", "-q", "-m", "seed"], cwd=seed)
    _run(["git", "remote", "add", "origin", str(bare)], cwd=seed)
    _run(["git", "push", "-q", "origin", "master"], cwd=seed)
    for name in ("origin", "peer"):
        repo = base / name
        _run(["git", "clone", "-q", str(bare), str(repo)])
        _configure_identity(repo)
    for shim, body in (("git", _git_shim()),
                       ("ssh-agent", _ssh_agent_shim()),
                       ("ssh-add", _ssh_add_shim())):
        path = base / "bin" / shim
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    return base


def _env(scratch: Path,
         extra: Mapping[str, str | None]) -> dict[str, str]:
    env = os.environ.copy()
    for key in FAKE_SWITCHES:
        env.pop(key, None)
    env["PATH"] = str(scratch / "bin") + os.pathsep + env["PATH"]
    env["RUNNER_TEMP"] = str(scratch / "runner-temp")
    env["GITHUB_STEP_SUMMARY"] = str(scratch / "github-summary.md")
    env.pop("MASTER_PUSH_DEPLOY_KEY", None)
    env.pop("GIT_SSH_COMMAND", None)
    env.pop("SSH_AUTH_SOCK", None)
    env["REAL_GIT"] = shutil.which("git") or "git"
    env["HOME"] = str(scratch / "home")
    env["REPO"] = "Nitjsefnie/claudit"
    env["PUSH_REMOTE"] = str(scratch / "remote.git")
    env["FAKE_STATE"] = str(scratch / "state")
    env["FAKE_PEER"] = str(scratch / "peer")
    for key, value in extra.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def _stage_push_data(scratch: Path) -> str:
    """Pre-place the staging directory the download action would have
    produced, with the CHANGED data a refresh run detected, and return
    the tested base (the remote tip)."""
    data = scratch / "runner-temp" / "push-data"
    data.mkdir(parents=True, exist_ok=True)
    (data / "pricing.json").write_text(
        '{\n  "refreshed": true\n', encoding="utf-8")
    (data / "constants.py").write_text(
        'PRICING_VERSION = "7"\n', encoding="utf-8")
    (data / "commit-msg.txt").write_text(
        "refresh report, invocation 1\n", encoding="utf-8")
    base = _sha(scratch / "remote.git", "master")
    (data / "base.txt").write_text(f"{base}\n", encoding="utf-8")
    return base


def _run_push_step(scratch: Path,
                   extra_env: Mapping[str, str | None]
                   ) -> subprocess.CompletedProcess:
    script = scratch / "push-step.sh"
    script.write_text(_push_run(), encoding="utf-8")
    return subprocess.run(
        ["bash", "-e", str(script)],
        cwd=str(scratch / "origin"),
        env=_env(scratch, extra_env),
        capture_output=True,
        text=True,
        check=False,
    )


def _state_lines(scratch: Path, name: str) -> list[str]:
    path = scratch / "state" / name
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def _remote_show(scratch: Path, path: str) -> str:
    return _git(scratch / "remote.git", "show", f"master:{path}").stdout


def _pid_gone(pid: int, deadline_s: float = 5.0) -> bool:
    """Whether the process stopped existing within the deadline (the EXIT
    trap kills the shimmed agent; a killed child is reaped within
    moments of the script's exit)."""
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        time.sleep(0.05)
    return False


@ubuntu_run_block
def test_push_lands_the_staged_tree_when_master_stood_still(scratch: Path):
    _stage_push_data(scratch)
    staged_pricing = (scratch / "runner-temp" / "push-data"
                      / "pricing.json").read_text(encoding="utf-8")
    staged_constants = (scratch / "runner-temp" / "push-data"
                        / "constants.py").read_text(encoding="utf-8")
    commit_msg = (scratch / "runner-temp" / "push-data"
                  / "commit-msg.txt").read_text(encoding="utf-8").strip()

    proc = _run_push_step(scratch, {"MASTER_PUSH_DEPLOY_KEY": "test-key"})

    bare = scratch / "remote.git"
    assert proc.returncode == 0, proc.stderr
    built = _sha(scratch / "runner-temp" / "push-repo", "HEAD")
    assert _sha(bare, "master") == built
    assert _git(bare, "log", "-1", "--format=%ce",
                "master").stdout.strip() == BOT_EMAIL
    assert _remote_show(scratch, "src/pricing.json") == staged_pricing
    assert _remote_show(scratch, "backend/constants.py") == staged_constants
    assert commit_msg in _git(
        bare, "log", "-1", "--format=%B", "master").stdout
    assert _state_lines(scratch, "ssh-agent-called") == ["called"]
    assert _state_lines(scratch, "ssh-add-called") == ["called"]
    assert _state_lines(scratch, "ssh-add-bytes") and (
        int(_state_lines(scratch, "ssh-add-bytes")[0]) > 0)
    # The EXIT trap stopped the shimmed agent: the key never outlives the
    # step that loaded it.
    agent_pid = int(_state_lines(scratch, "ssh-agent-pid")[0])
    assert _pid_gone(agent_pid), "the EXIT trap did not stop the agent"


@ubuntu_run_block
def test_push_drops_the_data_with_a_notice_when_master_moved_under_the_run(
        scratch: Path):
    base = _stage_push_data(scratch)
    peer_full = _peer_move(scratch, "peer move after staging")

    proc = _run_push_step(scratch, {"MASTER_PUSH_DEPLOY_KEY": "test-key"})

    bare = scratch / "remote.git"
    assert proc.returncode == 0, proc.stderr
    assert _sha(bare, "master") == peer_full
    assert "::notice::" in proc.stdout
    summary = (scratch / "github-summary.md").read_text(encoding="utf-8")
    assert "Dropped: master moved under the run" in summary
    assert "rejected while master stood still" not in proc.stdout
    assert "rejected while master stood still" not in proc.stderr
    authors = _git(bare, "log", "--format=%ae",
                   "master").stdout.splitlines()
    assert BOT_EMAIL not in authors, authors
    assert base != peer_full


@ubuntu_run_block
def test_a_push_refused_while_master_stood_still_goes_red(scratch: Path):
    base = _stage_push_data(scratch)

    proc = _run_push_step(scratch, {"MASTER_PUSH_DEPLOY_KEY": "test-key",
                                    "FAKE_FAIL_GIT_PUSH": "1"})

    assert proc.returncode != 0
    assert "rejected while master stood still" in proc.stderr
    assert _sha(scratch / "remote.git", "master") == base


@ubuntu_run_block
def test_a_push_raced_by_a_master_move_is_dropped_with_a_notice(
        scratch: Path):
    _stage_push_data(scratch)

    proc = _run_push_step(scratch, {"MASTER_PUSH_DEPLOY_KEY": "test-key",
                                    "FAKE_RACE_PUSH": "1"})

    bare = scratch / "remote.git"
    peer_full = _sha(scratch / "peer", "HEAD")
    assert proc.returncode == 0, proc.stderr
    assert "::notice::" in proc.stdout
    assert _sha(bare, "master") == peer_full
    built = _sha(scratch / "runner-temp" / "push-repo", "HEAD")
    assert _git(bare, "merge-base", "--is-ancestor", built, "master",
                check=False).returncode != 0
    summary = (scratch / "github-summary.md").read_text(encoding="utf-8")
    assert "Dropped: master moved before the push landed" in summary


@ubuntu_run_block
def test_an_empty_key_fails_fast_before_any_agent_or_network(scratch: Path):
    base = _stage_push_data(scratch)

    proc = _run_push_step(scratch, {"MASTER_PUSH_DEPLOY_KEY": ""})

    assert proc.returncode != 0
    assert "MASTER_PUSH_DEPLOY_KEY is empty" in proc.stderr
    assert "master-push environment" in proc.stderr
    assert _state_lines(scratch, "ssh-agent-called") == []
    assert _state_lines(scratch, "ssh-add-called") == []
    assert _sha(scratch / "remote.git", "master") == base
