"""Workflows leave apt to the shared composite, whose install retries each apt
phase until it succeeds, and the bounded browser step follows it.

The composite's install step and panel-layout's browser step run as written,
with sudo, env, apt-get, dpkg, npx and sleep as recording doubles, timeout as
the shared stand-in, which ends chosen attempts as an expired timeout(1) does,
and the progress watchdog as a stand-in that stalls chosen attempts as the real
one reports a stall. A worst case is every attempt that may be ended ended,
the last of each loop answering just inside its bound, each kill after its grace."""
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
WATCHDOG = ROOT / "scripts" / "ci" / "progress_watchdog.py"
WORKFLOWS = ROOT / ".github" / "workflows"
ACTION = ROOT / ".github" / "actions" / "apt-install" / "action.yml"
ACTION_REF = "$/.github/actions/apt-install"
INSTALL_STEPS_MINUTES = 10  # both install steps' backstops, so a slow healthy leg stays well under 20 minutes
PACKAGES = "xvfb fonts-liberation"
DOUBLES = r'''
record() { local IFS=$'\037'; printf '%s\n' "$*" >> "$CALLS"; }
# The watchdog execs its command, so it reads only `--stall N --sample N --grace N --watch PATH... -- COMMAND`, runs only a
# stubbed COMMAND and records each call; CLAUDIT_STALL_COUNT attempts of each command holding CLAUDIT_STALL_ONLY stall.
watchdog() {
  local options=() key="$CLAUDIT_WATCHDOG_CALLS.$(printf '%s' "$*" | sed 's/.* -- //' | cksum | cut -d ' ' -f 1)" seen
  while [ $# -gt 1 ] && { [[ "$1 $2" =~ ^--(stall|sample|grace)\ [1-9][0-9]*$ ]] || [[ "$1 $2" == "--watch /"* ]]; }; do
    options+=("$1" "$2"); shift 2
  done
  [ "${1:-}" = -- ] && declare -F "${2:-}" >/dev/null || { echo "watchdog stand-in: unread call: $*" >&2; return 125; }
  shift; printf '%s|%s\n' "${options[*]}" "$*" >> "$CLAUDIT_WATCHDOG_CALLS"; seen="$(cat "$key" 2>/dev/null || echo 0)"
  [[ "$*" != *"$CLAUDIT_STALL_ONLY"* || "$seen" -ge "$CLAUDIT_STALL_COUNT" ]] || { echo "$((seen + 1))" > "$key"; return 124; }
  "$@"
}
sudo() { case "${1:-}" in timeout|env) "$@" ;; */progress_watchdog.py) shift; watchdog "$@" ;; *) return 99 ;; esac; }
env() (
  while [[ "${1:-}" == DEBIAN_FRONTEND=* ]]; do export "$1"; shift; done
  case "${1:-}" in timeout) "$@" ;; */progress_watchdog.py) shift; watchdog "$@" ;; *) exit 99 ;; esac
)
apt-get() {
  record apt-get "${DEBIAN_FRONTEND:-}" "$@"
  [[ "$*" != *"$APT_FAIL"* ]] || [ "$(grep -c -- "$APT_FAIL" "$CALLS")" -gt "$APT_FAILS" ] || return 100
}
dpkg() { record dpkg "${DEBIAN_FRONTEND:-}" "$@"; }
npx() {
  record npx "$@"
  case "$*" in
    "--no-install playwright install chromium") ;;
    "--no-install playwright install-deps --dry-run chromium") printf '%b' "$REPORT"; return "$REPORT_STATUS" ;;
    *) echo "npx double: unmodelled $*" >&2; return 99 ;;
  esac
}
sleep() { record sleep "$@"; [ "$(grep -c '^sleep' "$CALLS")" -lt 50 ]; }  # the 50th pause ends a loop that never succeeds
export -f record npx sleep
'''


def _load(path: Path) -> dict:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader) or {}


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
    browsers = [index for index, step in enumerate(steps) if "playwright install" in step.get("run", "")]
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
             "CLAUDIT_TIMEOUT_STATE": str(tmp_path / "timeout-state"), "CLAUDIT_TIMEOUT_DOUBLES": str(tmp_path / "none"),
             "CLAUDIT_APT_PACKAGES": PACKAGES, "APT_FAIL": "@nothing@", "APT_FAILS": "0", "REPORT": "", "REPORT_STATUS": "0",
             "GITHUB_ACTION_PATH": str(ACTION.parent), "CLAUDIT_WATCHDOG_CALLS": str(tmp_path / "watchdog"),
             "CLAUDIT_STALL_ONLY": "@nothing@", "CLAUDIT_STALL_COUNT": "0", **env},
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


def _watched(tmp_path: Path) -> list[tuple[list[str], str]]:
    """(the watchdog's options, its command) per call."""
    lines = (tmp_path / "watchdog").read_text(encoding="utf-8").splitlines()
    return [(options.split(), command) for options, _, command in (line.partition("|") for line in lines)]


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
def test_every_apt_phase_retries_a_stalled_attempt_and_two_attempts_fit_the_step(tmp_path):
    proc, calls = _install(tmp_path, CLAUDIT_STALL_ONLY="apt-get", CLAUDIT_STALL_COUNT="1", CLAUDIT_TIMEOUT_EXPIRE="1")
    assert proc.returncode == 0, proc.stderr
    watched = _watched(tmp_path)
    assert [" ".join(command.split()[:2]) for _, command in watched] == ["apt-get update"] * 2 + ["apt-get install"] * 4
    assert [command for _, command in bounded_calls(tmp_path / "timeout-state")] == [
        "apt-get check -qq -o DPkg::Lock::Timeout=10", "dpkg --configure -a"]
    apt = [call for call in calls if call[0] == "apt-get" and call[2] != "check"]  # the update, download and install
    assert "APT::Update::Error-Mode=any" in apt[0] and "--download-only" in apt[1] and "--no-download" in apt[2]
    for call in apt[:2]:  # apt still ends a dead connection itself; its Err/W/E lines reach the log
        assert {"Acquire::Retries=3", "Acquire::http::Timeout=10", "Acquire::https::Timeout=10"} <= set(call)
        assert "-q" in call and "-qq" not in call
    for call in apt[1:]:
        assert call[-2:] == PACKAGES.split() and call[1] == "noninteractive"
    option, = {tuple(option) for option, _ in watched}  # every attempt of every phase is watched alike
    pairs = list(zip(option[::2], option[1::2]))
    assert {value for name, value in pairs if name == "--watch"} == {
        "/var/lib/apt/lists", "/var/cache", "/var/lib/dpkg", "/var/log/dpkg.log", "/var/log/apt"}
    stall, sample, grace = (int(dict(pairs)[name]) for name in ("--stall", "--sample", "--grace"))
    recovery = bounded_calls(tmp_path / "timeout-state")
    said = r"^(?:==> |::warning::)(.+?)(?: attempt 1)? (?:stalled|timed out) after (\d+) s"
    assert re.findall(said, proc.stdout, re.M) == [
        ("apt-get update", str(stall)), ("apt-get install --download-only", str(stall)),
        ("apt-get install --no-download", str(stall)), ("apt-get check", str(recovery[0][0])),
        ("dpkg --configure -a", str(recovery[1][0]))]
    assert subprocess.run([str(WATCHDOG), *option, "--", "true"], check=False, timeout=30).returncode == 0
    # A stalled attempt ends within its window, a sample and two graces; then the pause and the recovery's bounds.
    cycle = stall + sample + 2 * grace + max(int(call[1]) for call in calls if call[0] == "sleep") + sum(
        limit for limit, _ in recovery) + sum(map(int, (tmp_path / "timeout-state" / "kills").read_text("utf-8").split()))
    callers = [step for _, _, step in _workflow_steps() if step.get("uses") == ACTION_REF]
    cycle += int(re.findall(r"timeout (\d+)s rm ", _action_steps()[0]["run"])[0]) / 2  # the sanitizer runs once
    assert callers and all(2 * cycle < int(step["timeout-minutes"]) * 60 for step in callers), cycle


@ubuntu_step_body
@pytest.mark.parametrize("phase,label,later", [
    ("update", "apt-get update", "install"),
    ("--download-only", "apt-get install --download-only", "--no-download"),
    ("--no-download", "apt-get install --no-download", None),
])
def test_an_apt_phase_retries_a_failed_attempt_until_it_succeeds(tmp_path, phase, label, later):
    proc, calls = _install(tmp_path, APT_FAIL=phase, APT_FAILS="4")
    assert proc.returncode == 0, proc.stderr
    assert re.findall(rf"^==> {label} attempt (\d+) exited with status 100$", proc.stdout, re.M) == ["1", "2", "3", "4"]
    ran = calls[calls.index([call for call in calls if phase in call][0]):]
    phases = [call[2] if call[0] == "apt-get" else call[0] for call in ran]
    recovery = ["sleep", "check", "dpkg"] if later is None else ["sleep"]
    assert phases[:5 * (1 + len(recovery)) - len(recovery)] == ([phases[0]] + recovery) * 4 + [phases[0]], phases
    assert all(phase in ran[index] for index in range(0, 5 * (1 + len(recovery)) - len(recovery), 1 + len(recovery)))
    if later:
        assert [call for call in ran if later in call], "the next phase never ran"
    else:  # both recovery commands ran noninteractively and succeeded
        assert ran[3][1] == "noninteractive" and ran[3][2:] == ["--configure", "-a"]
        assert "::warning::" not in proc.stdout


@ubuntu_step_body
def test_only_the_recovery_command_that_failed_is_reported(tmp_path):
    proc, _ = _install(tmp_path, CLAUDIT_STALL_ONLY="apt-get install --no-download", CLAUDIT_STALL_COUNT="1",
                       APT_FAIL="check", APT_FAILS="99")
    assert proc.returncode == 0, proc.stderr
    assert re.findall(r"^::warning::.*$", proc.stdout, re.M) == ["::warning::apt-get check exited with status 100"]


def _browser_run(tmp_path: Path, **env: str) -> tuple[subprocess.CompletedProcess, list[list[str]]]:
    return _run(_browser()[1]["run"], tmp_path, ["-e"], **env)


@ubuntu_step_body
def test_the_browser_install_retries_an_ended_attempt_and_both_steps_fit_their_backstops(tmp_path):
    proc, calls = _browser_run(tmp_path, CLAUDIT_TIMEOUT_EXPIRE="2", CLAUDIT_TIMEOUT_ONLY="playwright install chromium",
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
    proc, calls = _browser_run(tmp_path, CLAUDIT_TIMEOUT_EXPIRE="3", CLAUDIT_TIMEOUT_ONLY="playwright install chromium",
                               CLAUDIT_TIMEOUT_STATUS=status)
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
    assert (f"::error::Playwright's dependency dry run {said.format(limit)} and printed: " + report.rstrip("\n")) in proc.stderr
