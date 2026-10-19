"""PostgreSQL client installs must be bounded, retried and attributed.
The client major must match the container. Bound each step, each key-download
attempt and both apt calls; retries cannot help an attempt that never exits.
Do not swallow failures. Announce each command because quiet curl/apt output
otherwise leaves only a group header when a command stalls.
Select install steps by the commands they run so renaming a step cannot
escape the checks; assert both the site set and step count."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

# What an install step's run block does, independent of how it is named.
_INSTALL_MARKERS = ("apt.postgresql.org", "postgresql-client")

# Each client-installing job contributes one site; both pytest modes need it.
INSTALL_SITES = (
    "tests.yml",
    "tests.yml",
    "test-data.yml",
    "test-data-explore.yml",
    "smoke.yml",
    "refresh-pricing.yml",
)

STEP_TIMEOUT_MINUTES = "5"

CURL_BOUNDS = (
    "--connect-timeout 10",
    "--max-time 30",
    "--retry 3",
    "--retry-all-errors",
)

APT_BOUNDS = (
    "Acquire::Retries=3",
    "Acquire::http::Timeout=30",
    "Acquire::https::Timeout=30",
)

# A line's exit status is its LAST command's, so a line ending in a shell
# no-op succeeds whatever ran before it. Read the last segment rather than
# matching a spelling, so a trailing comment or `;` cannot hide it.
_SHELL_SEPARATOR = re.compile(r"\|\||&&|;")
_SHELL_NO_OPS = ("true", ":", "exit 0")


def _swallows_failure(command: str) -> bool:
    # The comment goes first: bash reads `# …` as a comment, so a `|| true`
    # after it is text, while one before it is the line's last command.
    code = command.split("#", 1)[0]
    segments = [part for part in _SHELL_SEPARATOR.split(code) if part.strip()]
    if not segments:
        return False
    return segments[-1].strip() in _SHELL_NO_OPS


def _load(path: Path) -> dict:
    return yaml.load(path.read_text(encoding="utf-8"),
                     Loader=yaml.BaseLoader) or {}


def _installs_the_client(step: dict) -> bool:
    run = step.get("run") or ""
    return any(marker in run for marker in _INSTALL_MARKERS)


def _install_steps() -> list[tuple[str, dict]]:
    """Every (workflow name, step) whose run block installs the client."""
    found: list[tuple[str, dict]] = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        doc = _load(path)
        for job in (doc.get("jobs") or {}).values():
            for step in (job or {}).get("steps") or []:
                if _installs_the_client(step):
                    found.append((path.name, step))
    return found


def _run_text(step: dict) -> str:
    return step.get("run") or ""


def test_the_install_sites_are_exactly_the_known_jobs():
    # Anti-drift: the covered set is every workflow that actually installs
    # the client, so a new install site cannot pass by being unlisted.
    names = sorted(name for name, _ in _install_steps())
    assert names == sorted(INSTALL_SITES), names


def test_every_install_step_declares_the_step_timeout():
    steps = _install_steps()
    # The count pin catches a second install step added INSIDE a workflow
    # already on the list, which the site set alone would not.
    assert len(steps) == len(INSTALL_SITES), [name for name, _ in steps]
    for name, step in steps:
        assert step.get("timeout-minutes") == STEP_TIMEOUT_MINUTES, name


def test_every_install_step_bounds_the_key_download():
    # Bound to the download line itself, not to any sibling curl or echo:
    # a bare substring would be satisfied by the bound appearing anywhere
    # in the run block.
    for name, step in _install_steps():
        curls = [line.strip() for line in _run_text(step).splitlines()
                 if line.strip().startswith("curl ")]
        assert len(curls) == 1, (name, curls)
        download = curls[0]
        assert "ACCC4CF8.asc" in download and "gpg --dearmor" in download, name
        for bound in CURL_BOUNDS:
            assert bound in download, (name, bound)


def test_every_install_step_bounds_both_apt_calls():
    for name, step in _install_steps():
        apts = [line.strip() for line in _run_text(step).splitlines()
                if line.strip().startswith("sudo apt-get ")]
        assert len(apts) == 2, (name, apts)
        assert apts[0].startswith("sudo apt-get update "), name
        assert "postgresql-client-16" in apts[1], name
        for line in apts:
            for bound in APT_BOUNDS:
                assert bound in line, (name, bound)


def test_no_install_command_swallows_its_failure():
    # point is that a stall FAILS the step, visibly. A
    # trailing `|| true` would turn a failed install into a green step,
    # which is exactly what the step-level bound exists to prevent.
    for name, step in _install_steps():
        for line in _run_text(step).splitlines():
            stripped = line.strip()
            if not _is_command(stripped):
                continue
            assert not _swallows_failure(stripped), (name, stripped)


def _is_command(line: str) -> bool:
    return (line.startswith("curl ")
            or line.startswith("sudo apt-get ")
            or line.startswith("sudo sh -c "))


def test_every_install_command_is_announced():
    # the step's log must name the command that stalled. Each
    # command line is preceded by an echo carrying a non-empty message;
    # the last echo printed is the command in flight.
    for name, step in _install_steps():
        lines = [line.strip() for line in _run_text(step).splitlines()]
        lines = [line for line in lines if line]
        for index, line in enumerate(lines):
            if not _is_command(line):
                continue
            assert index > 0 and lines[index - 1].startswith("echo "), (
                name, line)
            message = lines[index - 1].removeprefix("echo ").strip()
            assert message.strip("\"'"), (name, lines[index - 1])
