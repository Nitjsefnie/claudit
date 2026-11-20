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
INSTALL_STEPS_MINUTES = 19  # apt 15 (a cache miss on a slow mirror) + browser 4, so with npm ci and the sweep the job fits its 30
PACKAGES = "xvfb fonts-liberation"
DISTRO = "/etc/apt/sources.list.d/ubuntu.sources"
SANITIZE, KEY, RESTORE, INSTALL, SAVE = STEPS = ("Remove the unused Microsoft apt source", "Key the package cache", "Restore the package archives", "Install packages, retrying each phase until it succeeds", "Save the package archives")
ARCHIVES = "apt-archives"  # under the runner's temp directory, which is each run's tmp_path
IMAGE = {"ImageOS": "ubuntu24", "RUNNER_ARCH": "X64"}
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
sudo() {
  case "${1:-}" in timeout|env) "$@" ;; */progress_watchdog.py) [ -x "$1" ] || return 98; shift; watchdog "$@" ;;
    *) echo "sudo double: unmodelled $*" >&2; return 99 ;; esac
}
env() (
  while [[ "${1:-}" == DEBIAN_FRONTEND=* ]]; do export "$1"; shift; done
  case "${1:-}" in timeout) "$@" ;; */progress_watchdog.py) [ -x "$1" ] || exit 98; shift; watchdog "$@" ;;
    *) echo "env double: unmodelled $*" >&2; exit 99 ;; esac
)
apt-get() {
  record apt-get "${DEBIAN_FRONTEND:-}" "$@"; local option
  for option; do [[ "$option" != Dir::Cache::archives=* || -d "${option#*=}partial" ]] || return 100; done  # as apt refuses it
  [[ "$*" != *"$APT_FAIL"* ]] || [ "$(grep -c -- "$APT_FAIL" "$CALLS")" -gt "$APT_FAILS" ] || return 100
}
dpkg() { record dpkg "${DEBIAN_FRONTEND:-}" "$@"; [ "$*" = "--configure -a" ] || { echo "dpkg double: unmodelled $*" >&2; return 99; }; }
test() { [ "$*" = "-f $CLAUDIT_DISTRO" ] || { echo "test double: unmodelled $*" >&2; return 99; }; [ -z "${CLAUDIT_DISTRO_MISSING:-}" ]; }
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


def _step(name: str) -> dict:
    found = [step for step in _action_steps() if step.get("name") == name]
    assert len(found) == 1, (name, [step.get("name") for step in _action_steps()])
    return found[0]


def _browser() -> tuple[dict, dict]:
    """(the apt composite step, the browser step after it)."""
    steps = [step for name, _, step in _workflow_steps() if name == "panel-layout.yml"]
    browsers = [index for index, step in enumerate(steps) if "playwright install" in step.get("run", "")]
    assert len(browsers) == 1 and browsers[0] > 0
    return steps[browsers[0] - 1], steps[browsers[0]]


def _run(script: str, tmp_path: Path, flags: list[str], **env: str) -> tuple[subprocess.CompletedProcess, list[list[str]]]:
    """Run `script` under the doubles from the checkout; (process, the doubles' calls)."""
    (trip := tmp_path / "tripwire").mkdir(parents=True)
    for name in ("sudo", "apt-get", "dpkg", "npx", "timeout"):  # a call that escapes its double fails loudly
        (trip / name).write_text(f"#!/bin/sh\necho 'real {name} reached' >&2\nexit 97\n", encoding="utf-8", newline="\n")
        (trip / name).chmod(0o755)
    (tmp_path / "timeout-state").mkdir()
    calls = tmp_path / "calls"
    calls.touch()
    (tmp_path / "output").touch()
    proc = subprocess.run(
        ["bash", "--noprofile", "--norc", *flags, "-c", TIMEOUT_STUB + DOUBLES + script],
        env={"PATH": f"{trip}{os.pathsep}{os.environ['PATH']}", "CALLS": str(calls),
             "CLAUDIT_TIMEOUT_STATE": str(tmp_path / "timeout-state"), "CLAUDIT_TIMEOUT_DOUBLES": str(tmp_path / "none"),
             "CLAUDIT_APT_PACKAGES": PACKAGES, "APT_FAIL": "@nothing@", "APT_FAILS": "0", "REPORT": "", "REPORT_STATUS": "0",
             "GITHUB_ACTION_PATH": str(ACTION.parent), "RUNNER_TEMP": str(tmp_path),
             "GITHUB_OUTPUT": str(tmp_path / "output"), "CLAUDIT_WATCHDOG_CALLS": str(tmp_path / "watchdog"),
             "CLAUDIT_STALL_ONLY": "@nothing@", "CLAUDIT_STALL_COUNT": "0", "CLAUDIT_DISTRO": DISTRO, **env},
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=False, timeout=30)
    return proc, [line.split("\x1f") for line in calls.read_text(encoding="utf-8").splitlines()]


def _worst_case(tmp_path: Path, calls: list[list[str]]) -> int:
    """Seconds: every bounded call at its bound plus its kill grace, plus every sleep."""
    state = tmp_path / "timeout-state"
    kills = (state / "kills").read_text(encoding="utf-8").split()
    assert "none" not in kills, "a bounded call without -k has no worst case"
    limits = [seconds for seconds, _ in bounded_calls(state)]
    return sum(limits) + sum(map(int, kills)) + sum(int(call[1]) for call in calls if call[0] == "sleep")


def _run_step(tmp_path: Path, step: str = INSTALL, **env: str) -> tuple[subprocess.CompletedProcess, list[list[str]]]:
    """Run an action step as its `shell: bash` runs it; (process, the doubles' calls)."""
    return _run(_step(step)["run"], tmp_path, ["-eo", "pipefail"], **env)


def test_workflows_delegate_every_apt_command():
    for name, job, step in _workflow_steps():
        assert "apt-get" not in step.get("run", ""), (name, job, step)


def test_browser_follows_the_apt_install_step():
    apt, _ = _browser()
    assert_apt_sanitizer(ROOT, apt)


def test_sanitization_always_runs_and_the_package_steps_restore_and_save_one_cache():
    sanitize, install = _step(SANITIZE), _step(INSTALL)
    assert re.search(
        r"sudo timeout \d+s rm -f\s+/etc/apt/sources.list.d/microsoft-prod.list"
        r"\s+/etc/apt/sources.list.d/microsoft-prod.sources", sanitize["run"])
    assert "if" not in sanitize
    assert "apt-get" not in sanitize["run"]
    assert install["if"] == "inputs.packages != ''"
    assert _load(ACTION)["inputs"]["packages"]["default"] == ""
    assert [step.get("name") for step in _action_steps()] == list(STEPS)
    key, restore, save = (_step(name) for name in (KEY, RESTORE, SAVE))
    archive = {"path": f"${{{{ runner.temp }}}}/{ARCHIVES}/*.deb", "key": "${{ steps.key.outputs.key }}"}  # exact key: no restore-keys
    assert (key.get("id"), key.get("if"), key.get("shell"), key.get("env")) == ("key", "inputs.packages != ''", "bash", {"CLAUDIT_APT_PACKAGES": "${{ inputs.packages }}"})
    assert (restore.get("id"), restore["uses"].partition("@")[0], restore.get("if"), restore.get("with")) == ("cache", "actions/cache/restore", "steps.key.outputs.key != ''", archive)
    assert (save["uses"].partition("@")[0], save.get("continue-on-error"), save.get("with"), save.get("if")) == ("actions/cache/save", "true", archive, (
        "steps.key.outputs.key != '' && steps.cache.outputs.cache-hit != 'true' && github.event_name == 'push' && github.ref == 'refs/heads/master'"))


@ubuntu_step_body
def test_every_apt_phase_retries_a_stalled_attempt_and_two_attempts_fit_the_step(tmp_path):
    proc, calls = _run_step(tmp_path, CLAUDIT_STALL_ONLY="apt-get", CLAUDIT_STALL_COUNT="1", CLAUDIT_TIMEOUT_EXPIRE="1")
    assert proc.returncode == 0, proc.stderr
    watched = [(options.split(), command) for options, _, command in  # (the watchdog's options, its command) per call
               (line.partition("|") for line in (tmp_path / "watchdog").read_text("utf-8").splitlines())]
    assert [" ".join(command.split()[:2]) for _, command in watched] == ["apt-get update"] * 2 + ["apt-get install"] * 4
    assert [command for _, command in bounded_calls(tmp_path / "timeout-state")] == [
        f"apt-get check -qq -o Dir::Etc::SourceList={DISTRO} -o Dir::Etc::SourceParts=/dev/null -o DPkg::Lock::Timeout=10",
        "dpkg --configure -a"]
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
        "/var/lib/apt/lists", "/var/cache", "/var/lib/dpkg", "/var/log/dpkg.log", "/var/log/apt", str(tmp_path / ARCHIVES)}
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
    callers = [step for _, _, step in _workflow_steps()  # every spelling that resolves to this action
               if step.get("uses", "").partition("@")[0].rstrip("/").endswith("/.github/actions/apt-install")]
    cycle += int(re.findall(r"timeout (\d+)s rm ", _step(SANITIZE)["run"])[0]) / 2  # the sanitizer runs once
    assert callers and all(2 * cycle < int(step["timeout-minutes"]) * 60 for step in callers), cycle


@ubuntu_step_body
@pytest.mark.parametrize("phase,label,later", [
    ("update", "apt-get update", "install"),
    ("--download-only", "apt-get install --download-only", "--no-download"),
    ("--no-download", "apt-get install --no-download", None),
])
def test_an_apt_phase_retries_a_failed_attempt_until_it_succeeds(tmp_path, phase, label, later):
    proc, calls = _run_step(tmp_path, APT_FAIL=phase, APT_FAILS="4")
    assert proc.returncode == 0, proc.stderr
    only = {f"Dir::Etc::SourceList={DISTRO}", "Dir::Etc::SourceParts=/dev/null"}  # no third-party source can fail it
    assert all(only <= set(call) for call in calls if call[0] == "apt-get"), calls
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
def test_a_missing_distribution_source_list_stops_the_step_at_once(tmp_path):
    proc, calls = _run_step(tmp_path, CLAUDIT_DISTRO_MISSING="1")
    assert (proc.returncode, calls, proc.stderr) == (1, [], f"::error::{DISTRO}, the distribution's apt sources, is missing\n")


@ubuntu_step_body
def test_only_the_recovery_command_that_failed_is_reported(tmp_path):
    proc, _ = _run_step(tmp_path, CLAUDIT_STALL_ONLY="apt-get install --no-download", CLAUDIT_STALL_COUNT="1",
                        APT_FAIL="check", APT_FAILS="99")
    assert proc.returncode == 0, proc.stderr
    assert re.findall(r"^::warning::.*$", proc.stdout, re.M) == ["::warning::apt-get check exited with status 100"]


@ubuntu_step_body
def test_the_install_points_apt_at_the_restored_archives_and_keeps_them(tmp_path):
    (archives := tmp_path / ARCHIVES).mkdir()
    debs = {archives / name: name.encode() for name in ("xvfb_2_amd64.deb", "fonts-liberation_1_all.deb")}
    for path, content in debs.items():
        path.write_bytes(content)
    proc, calls = _run_step(tmp_path)
    assert proc.returncode == 0, proc.stderr
    installs = [call for call in calls if call[:3] == ["apt-get", "noninteractive", "install"]]
    assert len(installs) == 2 and all(("-o", f"Dir::Cache::archives={archives}/") in zip(call, call[1:]) for call in installs), installs
    assert (archives / "partial").is_dir() and not [call for call in calls if call[0] == "dpkg"], calls
    assert {path: path.read_bytes() for path in archives.glob("*.deb")} == debs  # the step neither deletes nor replaces them; apt's reuse is not modelled here


@ubuntu_step_body
def test_the_cache_key_names_the_image_and_the_package_set(tmp_path):
    for empty in IMAGE:  # an image the key cannot name skips the cache, saying which name is empty
        proc, _ = _run_step(tmp_path / empty, KEY, **{**IMAGE, empty: ""})
        lines = (tmp_path / empty / "output").read_text("utf-8").splitlines()
        warnings = re.findall(r"^::warning::.*$", proc.stdout, re.M)
        assert (proc.returncode, lines, len(warnings)) == (0, [], 1) and "cache" in warnings[0] and [name for name in IMAGE if name in warnings[0]] == [empty], proc
    image = tuple(IMAGE.values())
    rows = [(PACKAGES, *image), (" fonts-liberation  xvfb xvfb ", *image), ("xvfb", *image),  # the first two name one set
            (PACKAGES + " fonts-unifont", *image), (PACKAGES, "ubuntu22", "X64"), (PACKAGES, "ubuntu24", "ARM64")]
    keys = []
    for index, (packages, image_os, arch) in enumerate(rows):
        proc, _ = _run_step(tmp_path / str(index), KEY, CLAUDIT_APT_PACKAGES=packages, ImageOS=image_os, RUNNER_ARCH=arch, ImageVersion=str(index))
        lines = (tmp_path / str(index) / "output").read_text("utf-8").splitlines()
        assert proc.returncode == 0 and len(lines) == 1 and re.fullmatch(rf"key=apt-debs-{image_os}-{arch}-[0-9a-f]{{64}}", lines[0]), proc
        keys.append(lines[0])
    assert keys[0] == keys[1] and len(set(keys)) == len(rows) - 1, keys


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
