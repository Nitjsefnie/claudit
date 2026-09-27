"""Workflow shape + race simulation: the pricing bot's push retries a refusal.

Issue #224: master can move between refresh-pricing's checkout and its
push — run 36271055836's push was refused because PR #223 merged in that
window, and the detected move sat waiting for the next hourly run. The
contract pinned here: a refused push is retried, not abandoned — fetch the
new master, reset to it, re-run scripts/ci/refresh_provider_rates.py
against the fetched tree (the PRICING_VERSION bump and the appended
entries are recomputed there, never a textual rebase — SV-RATE-REFRESH),
re-run the suite, and push again. At most three pushes; the run goes red
when the retries are exhausted or when the suite fails on the recomputed
tree; every push is HEAD:master from a commit whose parent is a fetched
master tip, so each is a plain fast-forward.

The simulation tests execute the push step's own `run:` block under
`bash -e` in a scratch repository whose remote moves mid-run — which is
the race itself.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "refresh-pricing.yml"
PUSH_STEP_NAME = "Commit and push, retrying when master moved"
REFUSAL_STEP_NAME = "Fail on a refusal"

# The shim switches _env pops so a leak from the outer environment can
# never flip a shim.
FAKE_SWITCHES = (
    "FAKE_PEER_EVERY",
    "FAKE_PEER_ON",
    "FAKE_QUIET_ON",
    "FAKE_QUIET_EVERY",
    "FAKE_PYTEST_FAIL",
    "FAKE_CAPTURE_GIT_PUSH",
)

# The simulation execs a POSIX-bash shebang shim from PATH to drive a GitHub
# Actions ubuntu run block — the block itself only ever runs on
# ubuntu-latest, and Windows cannot exec the shim (WinError 193).
ubuntu_run_block = pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="the simulation drives a GitHub Actions ubuntu run block through "
           "POSIX bash and PATH shims; the block only ever runs on "
           "ubuntu-latest",
)


# --- shape: parse the workflow -------------------------------------------


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _steps() -> list:
    job, = (_workflow().get("jobs") or {}).values()
    return job.get("steps") or []


def _step(name: str) -> dict:
    matches = [step for step in _steps() if step.get("name") == name]
    assert len(matches) == 1, f"expected exactly one step named {name!r}"
    step, = matches
    return step


def test_push_retry_reruns_the_refresh_on_the_fetched_master():
    run = _step(PUSH_STEP_NAME)["run"]
    assert "git fetch" in run
    assert "git reset --hard FETCH_HEAD" in run
    assert run.count("scripts/ci/refresh_provider_rates.py") == 1
    assert '--commit-msg "$RUNNER_TEMP/commit-msg.txt"' in run
    assert 'echo "rc=$rc" >> "$GITHUB_OUTPUT"' in run


def test_push_retry_reruns_the_suite_before_recommitting():
    run = _step(PUSH_STEP_NAME)["run"]
    assert run.count("python -m pytest") == 1
    assert run.index("git reset --hard FETCH_HEAD") < run.index("python -m pytest")
    assert run.index("python -m pytest") < run.rindex("git commit")
    assert run.index("::notice::") < run.index("git fetch")


def test_push_attempts_are_bounded_and_exhaustion_is_red():
    run = _step(PUSH_STEP_NAME)["run"]
    assert "attempts=3" in run
    assert 'for attempt in $(seq 2 "$attempts")' in run
    lines = [line for line in run.splitlines() if line.strip()]
    assert lines[-1].strip() == "exit 1"
    exhaustion = lines[-2]
    assert ">&2" in exhaustion
    assert "exhausted" in exhaustion


def test_the_push_never_forces():
    for step in _steps():
        run = step.get("run") or ""
        assert "--force" not in run, step.get("name")
        for line in run.splitlines():
            if "git push" in line:
                assert not re.search(r"(?:^|\s)-f(?:\s|$)", line), step.get("name")


def test_refusal_gate_covers_the_retried_refresh():
    gate = _step(REFUSAL_STEP_NAME)["if"]
    assert "steps.fetch.outputs.rc" in gate
    assert "steps.push.outputs.rc" in gate
    assert gate == "steps.fetch.outputs.rc != '0' || steps.push.outputs.rc == '1'"


def test_push_remote_seam_defaults_to_github_ssh():
    raw = WORKFLOW.read_text(encoding="utf-8")
    assert raw.count("PUSH_REMOTE") == 1
    assert (
        'remote="${PUSH_REMOTE:-git@github.com:${REPO}.git}"'
        in raw
    )
    assert "PUSH_REMOTE" not in (_step(PUSH_STEP_NAME).get("env") or {})


def test_push_uses_the_deploy_key_and_pinned_github_host_keys():
    workflow = _workflow()
    step = _step(PUSH_STEP_NAME)
    run = step["run"]

    assert step["env"]["MASTER_PUSH_DEPLOY_KEY"] == (
        "${{ secrets.MASTER_PUSH_DEPLOY_KEY }}")
    assert "GH_TOKEN" not in step["env"]
    assert "if [ -n \"${MASTER_PUSH_DEPLOY_KEY:-}\" ]; then" in run
    assert "StrictHostKeyChecking=yes" in run
    assert (
        "github.com ssh-ed25519 "
        "AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
        in run
    )
    assert (
        "github.com ssh-rsa "
        "AAAAB3NzaC1yc2EAAAADAQABAAABgQCj7ndNxQowgcQnjshcLrqPEiiphnt+VTTvDP6mHBL9j1aNUkY4Ue1gvwnGLVlOhGeYrnZaMgRK6+PKCUXaDbC7qtbW8gIkhL7aGCsOr/C56SJMy/BCZfxd1nWzAOxSDPgVsmerOBYfNqltV9/hWCqBywINIR+5dIg6JTJ72pcEpEjcYgXkE2YEFXV1JHnsKgbLWNlhScqb2UmyRkQyytRLtL+38TGxkxCflmO+5Z8CSSNY7GidjMIZ7Q4zMjA2n1nGrlTDkzwDCsw+wqFPGQA179cnfGWOWRVruj16z6XyvxvjJwbz0wQZ75XK5tKSb7FNyeIEs4TT4jk+S4dhPeAUC5y+bDYirYgM4GC7uEnztnZyaVWQ7B381AK4Qdrwt51ZqExKbQpTUNn+EjqoTwvqNj4kqx5QUCI0ThS/YkOxJCXmPUWZbhjpCg56i+2aB6CmK2JGhn57K5mj0MNdBXA4/WnwH6XoPWJzK5Nyu2zB3nAZp+S5hpQs+p1vN1/wsjk="
        in run
    )
    assert "git config user.name 'github-actions[bot]'" in run
    assert (
        "git config user.email '41898282+github-actions[bot]@users.noreply.github.com'"
        in run
    )
    assert run.index("if [ -n") < run.index("git push")
    assert run.index("if [ -n") < run.index("git fetch")
    assert workflow["jobs"]["refresh"]["permissions"]["contents"] == "read"
    assert "x-access-token" not in WORKFLOW.read_text(encoding="utf-8")


def test_push_step_gates_on_something_moving():
    assert _step(PUSH_STEP_NAME)["if"] == "steps.moved.outputs.moved == 'true'"


# --- the race simulation --------------------------------------------------


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


def _python3_shim() -> str:
    # Stands in for the refresh tool (the shape tests pin that the
    # re-invocation names the real script path; the loop's contract is
    # to re-run THE REFRESH TOOL).
    return r"""#!/usr/bin/env bash
set -eu
count=$(cat "$FAKE_STATE/count" 2>/dev/null || echo 0)
count=$((count + 1))
echo "$count" > "$FAKE_STATE/count"
if [ "${FAKE_PEER_EVERY:-0}" -eq 1 ] || [ "$count" -eq "${FAKE_PEER_ON:-0}" ]; then
  echo "master move $count" >> "$FAKE_PEER/README.md"
  git -C "$FAKE_PEER" add -A
  git -C "$FAKE_PEER" commit -m "peer master move $count"
  git -C "$FAKE_PEER" push -q origin master
fi
if [ "$count" -eq "${FAKE_QUIET_ON:-0}" ] \
    || [ "${FAKE_QUIET_EVERY:-0}" -eq 1 ]; then
  exit 0
  # like the real script with no moves: writes nothing
fi
sha=$(git rev-parse --short HEAD)
printf 'refreshed on %s\n' "$sha" >> src/pricing.json
version=$(sed -n 's/^PRICING_VERSION = "\([0-9]*\)"$/\1/p' backend/constants.py)
echo "PRICING_VERSION = \"$((version + 1))\"" > backend/constants.py
printf 'refresh report, invocation %s on %s\n' "$count" "$sha" \
  > "$RUNNER_TEMP/commit-msg.txt"
"""


def _python_shim() -> str:
    # Stands in for the suite (python -m pytest ...).
    return r"""#!/usr/bin/env bash
set -eu
echo "$(git rev-parse HEAD)" >> "$FAKE_STATE/python.log"
if [ "${FAKE_PYTEST_FAIL:-0}" -eq 1 ]; then
  exit 1
fi
"""


def _git_shim() -> str:
    # The default-remote case must observe the real command argument but
    # must not contact github.com. Other git operations still use Git.
    return r"""#!/usr/bin/env bash
set -eu
if [ "${FAKE_CAPTURE_GIT_PUSH:-0}" -eq 1 ] && [ "${1:-}" = "push" ]; then
  printf '%s\n' "$@" > "$FAKE_STATE/git-push-args"
  printf '%s\n' "${GIT_SSH_COMMAND:-}" > "$FAKE_STATE/git-ssh-command"
  exit 0
fi
exec "$REAL_GIT" "$@"
"""


def _ssh_agent_shim() -> str:
    # Return harmless shell assignments for the workflow's eval, without
    # starting a real agent during the simulation.
    return r"""#!/usr/bin/env bash
set -eu
printf 'called\n' > "$FAKE_STATE/ssh-agent-called"
if [ -z "${MASTER_PUSH_DEPLOY_KEY:-}" ]; then
  echo 'ssh-agent called without the deploy key' >&2
  exit 1
fi
printf '%s\n' 'SSH_AUTH_SOCK=/tmp/fake-agent.sock; export SSH_AUTH_SOCK;' \
  'SSH_AGENT_PID=1; export SSH_AGENT_PID;'
"""


def _ssh_add_shim() -> str:
    return r"""#!/usr/bin/env bash
set -eu
printf 'called\n' > "$FAKE_STATE/ssh-add-called"
if [ -z "${MASTER_PUSH_DEPLOY_KEY:-}" ]; then
  echo 'ssh-add called without the deploy key' >&2
  exit 1
fi
"""


@pytest.fixture(name="scratch")
def _scratch_fixture(tmp_path: Path) -> Path:
    base = tmp_path / "scratch"
    (base / "bin").mkdir(parents=True)
    (base / "state").mkdir()
    (base / "runner-temp").mkdir()
    (base / "github-output.txt").touch()
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
    for shim, body in (("python3", _python3_shim()),
                       ("python", _python_shim()),
                       ("git", _git_shim()),
                       ("ssh-agent", _ssh_agent_shim()),
                       ("ssh-add", _ssh_add_shim())):
        path = base / "bin" / shim
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    return base


def _env(scratch: Path,
         extra: dict[str, str | None]) -> dict[str, str]:
    env = os.environ.copy()
    for key in FAKE_SWITCHES:
        env.pop(key, None)
    env["PATH"] = str(scratch / "bin") + os.pathsep + env["PATH"]
    env["RUNNER_TEMP"] = str(scratch / "runner-temp")
    env["GITHUB_OUTPUT"] = str(scratch / "github-output.txt")
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


def _run_push_step(scratch: Path,
                   extra_env: dict[str, str]) -> subprocess.CompletedProcess:
    script = scratch / "push-step.sh"
    script.write_text(_step(PUSH_STEP_NAME)["run"], encoding="utf-8")
    return subprocess.run(
        ["bash", "-e", str(script)],
        cwd=str(scratch / "origin"),
        env=_env(scratch, extra_env),
        capture_output=True,
        text=True,
        check=False,
    )


def _refresh_once(scratch: Path,
                  extra_env: dict[str, str]) -> subprocess.CompletedProcess:
    # Simulates the workflow's earlier "Fetch and append" step: mutates the
    # clone's tree, writes commit-msg, and lands the mid-run master move
    # when FAKE_PEER_ON=1.
    return subprocess.run(
        [str(scratch / "bin" / "python3")],
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


def _last_rc(scratch: Path) -> str:
    lines = [
        line
        for line
        in (scratch / "github-output.txt").read_text(encoding="utf-8").splitlines()
        if line.startswith("rc=")
    ]
    assert lines, "no rc= line was written to GITHUB_OUTPUT"
    return lines[-1]


@ubuntu_run_block
def test_retry_recomputes_on_the_fetched_master_and_lands_a_fast_forward(
        scratch: Path):
    base_sha = _sha(scratch / "origin", "--short", "HEAD")
    _refresh_once(scratch, {"FAKE_PEER_ON": "1"})
    peer_sha = _sha(scratch / "peer", "--short", "HEAD")
    peer_full = _sha(scratch / "peer", "HEAD")

    proc = _run_push_step(scratch, {})

    bare = scratch / "remote.git"
    assert proc.returncode == 0, proc.stderr
    assert _state_lines(scratch, "ssh-agent-called") == []
    assert _state_lines(scratch, "ssh-add-called") == []
    assert _sha(bare, "master") == _sha(scratch / "origin", "HEAD")
    assert _sha(scratch / "origin", "HEAD^") == peer_full
    assert _git(scratch / "origin", "merge-base", "--is-ancestor",
                peer_full, "master", check=False).returncode == 0
    pricing = _remote_show(scratch, "src/pricing.json")
    assert f"refreshed on {peer_sha}" in pricing
    assert f"refreshed on {base_sha}" not in pricing
    assert _remote_show(
        scratch, "backend/constants.py") == 'PRICING_VERSION = "7"\n'
    assert _state_lines(scratch, "count") == ["2"]
    assert _state_lines(scratch, "python.log") == [peer_full]
    summary = (scratch / "github-summary.md").read_text(encoding="utf-8")
    assert "recomputed on the fetched master" in summary
    assert _last_rc(scratch) == "rc=0"


@ubuntu_run_block
def test_retry_exhaustion_goes_red_and_lands_nothing(scratch: Path):
    peer = scratch / "peer"
    _refresh_once(scratch, {"FAKE_PEER_EVERY": "1"})

    proc = _run_push_step(scratch, {"FAKE_PEER_EVERY": "1"})

    assert proc.returncode != 0
    assert "exhausted its 3 attempts" in proc.stderr
    assert _sha(peer, "HEAD") == _sha(scratch / "remote.git", "master")
    assert _state_lines(scratch, "count") == ["3"]
    assert len(_state_lines(scratch, "python.log")) == 2


@ubuntu_run_block
def test_retry_when_master_already_carries_the_move(scratch: Path):
    _refresh_once(scratch, {"FAKE_PEER_ON": "1"})
    peer_sha = _sha(scratch / "peer", "HEAD")

    proc = _run_push_step(
        scratch, {"FAKE_PEER_ON": "1", "FAKE_QUIET_ON": "2"})

    assert proc.returncode == 0, proc.stderr
    assert _sha(scratch / "remote.git", "master") == peer_sha
    assert _remote_show(
        scratch, "backend/constants.py") == 'PRICING_VERSION = "6"\n'
    assert _state_lines(scratch, "count") == ["2"]


@ubuntu_run_block
def test_suite_failure_on_the_recomputed_tree_goes_red(scratch: Path):
    _refresh_once(scratch, {"FAKE_PEER_ON": "1"})
    peer_sha = _sha(scratch / "peer", "HEAD")

    proc = _run_push_step(scratch, {"FAKE_PYTEST_FAIL": "1"})

    assert proc.returncode != 0
    assert _sha(scratch / "remote.git", "master") == peer_sha
    assert len(_state_lines(scratch, "python.log")) == 1


@ubuntu_run_block
def test_default_push_remote_is_the_github_ssh_url(scratch: Path):
    _refresh_once(scratch, {})

    proc = _run_push_step(scratch, {
        "MASTER_PUSH_DEPLOY_KEY": "test-private-key",
        "PUSH_REMOTE": None,
        "FAKE_CAPTURE_GIT_PUSH": "1",
    })

    assert proc.returncode == 0, proc.stderr
    assert _state_lines(scratch, "git-push-args") == [
        "push", "git@github.com:Nitjsefnie/claudit.git", "HEAD:master"]
    assert _state_lines(scratch, "git-ssh-command") == [
        "ssh -o StrictHostKeyChecking=yes"]
    assert _state_lines(scratch, "ssh-agent-called") == ["called"]
    assert _state_lines(scratch, "ssh-add-called") == ["called"]
    known_hosts = (scratch / "home" / ".ssh" / "known_hosts").read_text(
        encoding="utf-8")
    assert "github.com ssh-ed25519 " in known_hosts
    assert "github.com ssh-rsa " in known_hosts
