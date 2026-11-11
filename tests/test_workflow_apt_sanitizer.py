"""Workflows leave apt to the shared composite, whose bounded install the
browser step follows.

The composite's install step and panel-layout's browser step run as written,
with sudo, env, apt-get, dpkg, npx and sleep as recording doubles and timeout
as the shared stand-in, which ends chosen attempts as an expired timeout(1)
does. A worst case is every attempt that may be ended ended, the last of each
loop answering just inside its bound, each kill after its grace."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from tests.test_workflow_release_dispatch import ubuntu_step_body
from tests.workflow_checks import TIMEOUT_STUB, assert_apt_sanitizer, bounded_calls

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
ACTION = ROOT / ".github" / "actions" / "apt-install" / "action.yml"
ACTION_REF = "$/.github/actions/apt-install"
INSTALL_STEPS_MINUTES = 10  # both install steps' backstops, so a slow healthy leg stays well under 20 minutes
PACKAGES = "xvfb fonts-liberation"
DOUBLES = r'''
record() { local IFS=$'\037'; printf '%s\n' "$*" >> "$CALLS"; }
sudo() { case "${1:-}" in timeout|env) "$@" ;; *) echo "sudo double: unmodelled $*" >&2; return 99 ;; esac; }
env() (
  while [[ "${1:-}" == DEBIAN_FRONTEND=* ]]; do export "$1"; shift; done
  [ "${1:-}" = timeout ] || { echo "env double: unmodelled $*" >&2; exit 99; }
  "$@"
)
apt-get() { record apt-get "${DEBIAN_FRONTEND:-}" "$@"; case "$*" in *"$APT_FAIL"*) return 100 ;; esac; }
dpkg() { record dpkg "${DEBIAN_FRONTEND:-}" "$@"; }
npx() {
  record npx "$@"
  case "$*" in
    "--no-install playwright install chromium") ;;
    "--no-install playwright install-deps --dry-run chromium") printf '%b' "$REPORT"; return "$REPORT_STATUS" ;;
    *) echo "npx double: unmodelled $*" >&2; return 99 ;;
  esac
}
sleep() { record sleep "$@"; }
export -f record npx sleep
'''


def _load(path: Path) -> dict:
    return yaml.load(path.read_text(encoding="utf-8"),
                     Loader=yaml.BaseLoader) or {}


def _workflow_steps() -> list[tuple[str, str, dict]]:
    found = []
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        for job_id, job in (_load(path).get("jobs") or {}).items():
            for step in (job or {}).get("steps") or []:
                found.append((path.name, job_id, step))
    return found


def _action_steps() -> list[dict]:
    assert ACTION.is_file(), "shared apt-install composite is missing"
    doc = _load(ACTION)
    assert doc["runs"]["using"] == "composite"
    return doc["runs"]["steps"]


def _panel_steps() -> list[dict]:
    return [step for name, _, step in _workflow_steps() if name == "panel-layout.yml"]


def _browser() -> tuple[dict, dict]:
    """(the apt composite step, the browser step after it)."""
    steps = _panel_steps()
    browsers = [index for index, step in enumerate(steps)
                if "playwright install" in step.get("run", "")]
    assert len(browsers) == 1 and browsers[0] > 0
    return steps[browsers[0] - 1], steps[browsers[0]]


def _run(script: str, tmp_path: Path, flags: list[str], **env: str) -> tuple[subprocess.CompletedProcess, list[list[str]]]:
    """Run `script` under the doubles from the checkout; (process, the doubles' calls)."""
    trip = tmp_path / "tripwire"
    trip.mkdir()
    for name in ("sudo", "apt-get", "dpkg", "npx", "timeout"):  # a call that escapes its double fails loudly
        (trip / name).write_text(f"#!/bin/sh\necho 'real {name} reached' >&2\nexit 97\n", encoding="utf-8", newline="\n")
        (trip / name).chmod(0o755)
    (tmp_path / "timeout-state").mkdir()
    calls = tmp_path / "calls"
    calls.touch()
    proc = subprocess.run(
        ["bash", "--noprofile", "--norc", *flags, "-c", TIMEOUT_STUB + DOUBLES + script],
        env={"PATH": f"{trip}{os.pathsep}{os.environ['PATH']}", "CALLS": str(calls),
             "TIMEOUT_STATE": str(tmp_path / "timeout-state"), "TIMEOUT_DOUBLES": str(tmp_path / "none"),
             "CLAUDIT_APT_PACKAGES": PACKAGES, "APT_FAIL": "@nothing@", "REPORT": "", "REPORT_STATUS": "0", **env},
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=False, timeout=30)
    return proc, [line.split("\x1f") for line in calls.read_text(encoding="utf-8").splitlines()]


def _worst_case(tmp_path: Path, calls: list[list[str]]) -> int:
    """Seconds: every bounded call at its bound plus its kill grace, plus every sleep."""
    state = tmp_path / "timeout-state"
    kills = (state / "kills").read_text(encoding="utf-8").split()
    assert "none" not in kills, "a bounded call without -k has no worst case"
    limits = [seconds for seconds, _ in bounded_calls(state)]
    return sum(limits) + sum(map(int, kills)) + sum(int(call[1]) for call in calls if call[0] == "sleep")


def _install(tmp_path: Path, **env: str) -> tuple[subprocess.CompletedProcess, list[list[str]]]:
    return _run(_action_steps()[1]["run"], tmp_path, ["-eo", "pipefail"], **env)


def _commands(tmp_path: Path) -> list[str]:
    return [" ".join(command.split()[:2]) for _, command in bounded_calls(tmp_path / "timeout-state")]


def test_workflows_delegate_every_apt_command():
    for name, job, step in _workflow_steps():
        assert "apt-get" not in step.get("run", ""), (name, job, step)


def test_browser_follows_the_apt_install_step():
    apt, _ = _browser()
    assert_apt_sanitizer(ROOT, apt)


def test_sanitization_always_runs_and_package_setup_is_optional():
    sanitize, install = _action_steps()
    assert re.search(
        r"sudo timeout \d+s rm -f\s+/etc/apt/sources.list.d/microsoft-prod.list"
        r"\s+/etc/apt/sources.list.d/microsoft-prod.sources", sanitize["run"])
    assert "if" not in sanitize
    assert "apt-get" not in sanitize["run"]
    assert install["if"] == "inputs.packages != ''"
    assert _load(ACTION)["inputs"]["packages"]["default"] == ""


@ubuntu_step_body
def test_every_apt_phase_retries_an_ended_attempt_and_fits_its_backstop(tmp_path):
    proc, calls = _install(tmp_path, TIMEOUT_EXPIRE="1")
    assert proc.returncode == 0, proc.stderr
    # Each first attempt was ended; the waits for an abandoned dpkg run once, between the installs.
    assert _commands(tmp_path) == ["apt-get update"] * 2 + ["apt-get install"] * 3 + [
        "apt-get check", "dpkg --configure", "apt-get install"]
    update, download, install = [call for call in calls if call[0] == "apt-get"]
    assert "APT::Update::Error-Mode=any" in update[update.index("-o") + 1::2]
    assert "--download-only" in download and "--no-download" in install
    assert all("-q" in call and "-qq" not in call for call in (update, download))  # apt's Err/W/E lines reach the log
    for call in (download, install):
        assert call[-2:] == PACKAGES.split()
    assert install[1] == "noninteractive"
    said = re.findall(r"^(?:==> |::warning::)(.+?)(?: attempt 1)? timed out after (\d+) s$", proc.stdout, re.M)
    assert [label for label, _ in said] == [
        "apt-get update", "apt-get install --download-only", "apt-get install --no-download",
        "apt-get check", "dpkg --configure -a"]
    for label, seconds in said:  # each names the bound that ended it
        assert int(seconds) == next(limit for limit, command in bounded_calls(tmp_path / "timeout-state")
                                    if command.startswith(label))
    sanitize, = re.findall(r"timeout (\d+)s rm ", _action_steps()[0]["run"])
    worst = int(sanitize) + _worst_case(tmp_path, calls)
    callers = [step for _, _, step in _workflow_steps() if step.get("uses") == ACTION_REF]
    assert callers and all(worst < int(step["timeout-minutes"]) * 60 for step in callers), worst


@ubuntu_step_body
@pytest.mark.parametrize("phase,failure,later", [
    ("update", "apt-get update failed after 2 attempts", "install"),
    ("--download-only", "downloading xvfb fonts-liberation failed after 2 attempts", "--no-download"),
    ("--no-download", "apt-get install failed after 2 attempts", None),
])
def test_an_apt_phase_whose_every_attempt_fails_stops_the_step(tmp_path, phase, failure, later):
    proc, calls = _install(tmp_path, APT_FAIL=phase)
    assert proc.returncode == 1
    assert f"::error::{failure}; the last exited with status 100" in proc.stderr
    assert "attempt 1 exited with status 100" in proc.stdout
    failed = [call for call in calls if call[0] == "apt-get" and phase in call]
    assert len(failed) == 2
    if later:
        assert not [call for call in calls if later in call]
    else:  # the wait for an abandoned dpkg and its recovery run between the two installs
        ran = calls[calls.index(failed[0]):]
        assert [call[2] if call[0] == "apt-get" else call[0] for call in ran] == [
            "install", "sleep", "check", "dpkg", "install"]
        assert ran[0][1] == ran[3][1] == "noninteractive" and ran[3][2:] == ["--configure", "-a"]


@ubuntu_step_body
def test_an_update_the_bound_kills_says_it_timed_out(tmp_path):
    proc, calls = _install(tmp_path, TIMEOUT_EXPIRE="2", TIMEOUT_ONLY="apt-get update", TIMEOUT_STATUS="137")
    assert proc.returncode == 1
    (limit, _), = set(bounded_calls(tmp_path / "timeout-state"))
    assert f"::error::apt-get update failed after 2 attempts; the last timed out after {limit} s" in proc.stderr
    assert not [call for call in calls if call[0] == "apt-get"]


def _browser_run(tmp_path: Path, **env: str) -> tuple[subprocess.CompletedProcess, list[list[str]]]:
    return _run(_browser()[1]["run"], tmp_path, ["-e"], **env)


@ubuntu_step_body
def test_the_browser_install_retries_an_ended_attempt_and_both_steps_fit_their_backstops(tmp_path):
    proc, calls = _browser_run(tmp_path, TIMEOUT_EXPIRE="2", TIMEOUT_ONLY="playwright install chromium",
                               REPORT="All system dependencies are installed.\n")
    assert proc.returncode == 0, proc.stderr
    assert [command for _, command in bounded_calls(tmp_path / "timeout-state")] == [
        "npx --no-install playwright install chromium"] * 3 + [
        "npx --no-install playwright install-deps --dry-run chromium"]
    apt, browser = _browser()
    worst = _worst_case(tmp_path, calls)
    assert worst < int(browser["timeout-minutes"]) * 60, worst
    assert int(apt["timeout-minutes"]) + int(browser["timeout-minutes"]) <= INSTALL_STEPS_MINUTES


@ubuntu_step_body
@pytest.mark.parametrize("status,said", [("137", "timed out after {} s"), ("1", "exited with status 1")])
def test_a_browser_download_whose_every_attempt_fails_says_how(tmp_path, status, said):
    proc, calls = _browser_run(tmp_path, TIMEOUT_EXPIRE="3", TIMEOUT_ONLY="playwright install chromium",
                               TIMEOUT_STATUS=status)
    assert proc.returncode == 1
    (limit, _), = set(bounded_calls(tmp_path / "timeout-state"))
    assert "::error::the browser download's last attempt " + said.format(limit) in proc.stderr
    assert not [call for call in calls if "install-deps" in call]


@ubuntu_step_body
@pytest.mark.parametrize("report,status,said", [
    ("Missing system dependencies (1):\n  fonts-unifont\n", "1", "exited with status 1"),
    ("", "0", "exited with status 0"),  # a platform Playwright has no package list for: a warning on stderr, exit 0
    ("All system dependencies are installed.\n", "124", "timed out after {} s"),  # the bound ended it after its report
])
def test_a_dependency_the_apt_step_lacks_fails_the_browser_step(tmp_path, report, status, said):
    proc, _ = _browser_run(tmp_path, REPORT=report, REPORT_STATUS=status)
    assert proc.returncode == 1
    limit, _ = bounded_calls(tmp_path / "timeout-state")[-1]
    assert (f"::error::Playwright's dependency dry run {said.format(limit)} and printed: "
            + report.rstrip("\n")) in proc.stderr
