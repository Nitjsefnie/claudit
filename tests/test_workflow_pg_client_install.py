"""Every postgresql-client install step is bounded, retried and attributed.

Five workflows install the PostgreSQL client from the PGDG apt repo:
tests.yml, test-data.yml, test-data-explore.yml, smoke.yml and
refresh-pricing.yml. The step shells out to psql, and the client major
must match the container major, so the install cannot be dropped.

The invariants pinned here:

- every install step declares a step-level `timeout-minutes` (issue #845;
  a stalled install had held jobs for the full 45-minute job timeout
  three times on 2026-10-07);
- the key download bounds each attempt (`--connect-timeout`,
  `--max-time`) and retries all errors (`--retry`, `--retry-all-errors`),
  so a stalled connection is abandoned per attempt and retried inside the
  step budget (issue #862: the step's own 5-minute bound cancelled the
  refresh run 37738499924 because neither curl nor apt set a per-attempt
  timeout, so the configured retries never ran a second attempt);
- both apt calls carry the same per-attempt bound (`Acquire::Retries`,
  `Acquire::http::Timeout`, `Acquire::https::Timeout`);
- no install command swallows its own failure, so a stall fails the step
  visibly instead of greening it;
- each command is announced with a non-empty echo, so the step's log
  names the command that stalled instead of ending at the group header
  (issue #862: the log printed nothing after the first command echo,
  because `-qq` and `-s` leave no per-command output).

Steps are selected by PROPERTY — a step whose run block installs the
client — never by step name, so a re-named sixth site cannot escape the
pins below; the site list is pinned by exact set equality, and the step
count by `len(steps) == len(INSTALL_SITES)`.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

# What an install step's run block does, independent of how it is named.
_INSTALL_MARKERS = ("apt.postgresql.org", "postgresql-client")

# The workflows that install the client, and nothing else. A sixth site
# must be added here (and to every assertion below) in the same commit.
INSTALL_SITES = (
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


def test_the_install_sites_are_exactly_the_known_five():
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
    # Issue #862's point is that a stall FAILS the step, visibly. A
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
    # Issue #862: the step's log must name the command that stalled. Each
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
