"""All apt installs share source sanitization and bounded failure handling."""
from __future__ import annotations

import re
import shlex
from pathlib import Path

import yaml

from tests.workflow_checks import assert_apt_sanitizer, dockerfile_from_reference

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
ACTION = ROOT / ".github" / "actions" / "apt-install" / "action.yml"
ACTION_REF = "$/.github/actions/apt-install"
INSTALL_SITES = {
    ("tests.yml", "pytest-shards"),
    ("tests.yml", "pytest-thresholds"),
    ("test-data.yml", "perturbed-suite"),
    ("test-data-explore.yml", "perturbed-suite-explore"),
    ("smoke.yml", "smoke"),
    ("refresh-pricing.yml", "suite"),
}
APT_BOUNDS = (
    "Acquire::Retries=3",
    "Acquire::http::Timeout=30",
    "Acquire::https::Timeout=30",
)
# (apt-get subcommand and flag, flags its command must carry), in run order.
APT_LOOPS = (
    ("update", None, APT_BOUNDS),
    ("install", "--download-only", APT_BOUNDS),
    ("install", "--no-download", ()),
)
CURL_BOUNDS = (
    "--connect-timeout 10", "--max-time 30", "--retry 3",
    "--retry-all-errors",
)


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


def _client_steps() -> list[tuple[str, str, dict]]:
    return [(name, job, step) for name, job, step in _workflow_steps()
            if "postgresql-client" in str(step)]


def _action_steps() -> list[dict]:
    assert ACTION.is_file(), "shared apt-install composite is missing"
    doc = _load(ACTION)
    assert doc["runs"]["using"] == "composite"
    return doc["runs"]["steps"]


def test_client_sites_use_the_shared_mechanism():
    steps = _client_steps()
    assert len(steps) == len(INSTALL_SITES)
    assert {(name, job) for name, job, _ in steps} == INSTALL_SITES
    for name, job, step in steps:
        assert step.get("uses") == ACTION_REF, (name, job)
        assert "run" not in step, (name, job)
        assert step.get("name") == "Install postgresql-client"
        assert step.get("timeout-minutes") == "15"


def test_workflows_delegate_every_apt_command():
    for name, job, step in _workflow_steps():
        assert "apt-get" not in step.get("run", ""), (name, job, step)


def test_browser_dependencies_follow_sanitize_only_step():
    steps = [step for name, _, step in _workflow_steps()
             if name == "panel-layout.yml"]
    browsers = [index for index, step in enumerate(steps)
                if "playwright install" in step.get("run", "")]
    assert len(browsers) == 1
    index = browsers[0]
    assert index > 0
    assert_apt_sanitizer(ROOT, steps[index - 1])
    assert 0 < int(steps[index - 1]["timeout-minutes"]) <= 5


def test_sanitization_always_runs_and_package_setup_is_optional():
    steps = _action_steps()
    assert len(steps) == 2
    sanitize, install = steps
    assert "if" not in sanitize
    assert re.search(r"sudo timeout \d+s rm -f\s+/etc/apt/sources.list.d/microsoft-prod.list"
                     r"\s+/etc/apt/sources.list.d/microsoft-prod.sources",
                     sanitize["run"])
    assert "apt-get" not in sanitize["run"]
    assert "inputs.packages != ''" in install["if"]
    packages = _load(ACTION)["inputs"]["packages"]
    assert packages["default"] == ""
    assert packages.get("required", "false") == "false"
    assert "apt.postgresql.org/pub/repos/apt" in install["run"]
    assert "pgdg.list" in install["run"]
    assert "ACCC4CF8.asc" in install["run"]
    assert "gpg --dearmor" in install["run"]
    assert "DEBIAN_FRONTEND=noninteractive apt-get install" in install["run"]


def _bound(tokens: list[str]) -> int:
    return int(tokens[tokens.index("timeout") + 1].rstrip("s"))


def _apt_loops(run: str) -> list[dict]:
    """Each retry loop: attempts, body, parsed apt-get command and sleep."""
    loops = []
    for words, body in re.findall(
            r"for attempt in ([\d ]+); do\n(.*?)\n *done", run, re.S):
        apts = [line.strip() for line in body.splitlines()
                if line.strip().startswith("if sudo ")]
        assert len(apts) == 1
        tokens = shlex.split(apts[0].removeprefix("if ").removesuffix("; then"))
        sleeps, = re.findall(r"^\s*sleep (\d+)$", body, re.M)
        lines = [line.strip() for line in body.splitlines()]
        recovery = [(index, shlex.split(line.removeprefix("if ! ")
                                        .removesuffix("; then")))
                    for index, line in enumerate(lines)
                    if line.startswith("if ! sudo ")]
        loops.append({
            "lines": lines, "recovery": recovery,
            "attempts": len(words.split()), "last": words.split()[-1],
            "body": body, "tokens": tokens, "sleep": int(sleeps),
            "timeout": _bound(tokens),
        })
    return loops


def test_composite_bounds_download_and_every_apt_retry_loop():
    run = _action_steps()[1]["run"]
    curls = [line for line in run.splitlines()
             if re.match(r"timeout \d+s curl ", line.strip())]
    assert len(curls) == 1
    for bound in CURL_BOUNDS:
        assert bound in curls[0]
    loops = _apt_loops(run)
    assert len(loops) == len(APT_LOOPS)
    for (operation, flag, bounds), loop in zip(APT_LOOPS, loops):
        tokens = loop["tokens"]
        assert tokens[:2] == ["sudo", "timeout"]
        assert tokens[tokens.index("apt-get") + 1] == operation
        assert loop["timeout"] > 0
        assert loop["attempts"] == 3
        for bound in bounds:
            assert bound in tokens
        for other in ("--download-only", "--no-download"):
            assert (other in tokens) == (other == flag), (operation, other)
        assert "break" in loop["body"]
        assert f'if [ "$attempt" -eq {loop["last"]} ]; then' in loop["body"]
        assert "::error::" in loop["body"]
        assert "exit 1" in loop["body"]
        assert loop["body"].index("exit 1") < loop["body"].index("sleep ")


def test_cache_install_waits_on_a_lingering_dpkg_lock_inside_its_attempt():
    cache = _apt_loops(_action_steps()[1]["run"])[-1]
    waits = [token.partition("=")[2] for token in cache["tokens"]
             if token.startswith("DPkg::Lock::Timeout=")]
    assert len(waits) == 1
    assert 0 < int(waits[0]) < cache["timeout"]


def test_cache_install_outlasts_the_network_bound_that_failed():
    *network, cache = _apt_loops(_action_steps()[1]["run"])
    assert cache["timeout"] > max(loop["timeout"] for loop in network)


def test_cache_retry_waits_out_then_recovers_an_abandoned_dpkg():
    loops = _apt_loops(_action_steps()[1]["run"])
    assert [bool(loop["recovery"]) for loop in loops] == [False, False, True]
    cache = loops[-1]
    lines = cache["lines"]
    attempt = next(i for i, line in enumerate(lines)
                   if line.startswith("if sudo "))
    (wait_at, wait), (fix_at, fix) = cache["recovery"]
    sleep_at = lines.index(f"sleep {cache['sleep']}")
    assert attempt < sleep_at < wait_at < fix_at
    assert wait[wait.index("apt-get") + 1] == "check"
    lock, = [token for token in wait if token.startswith("DPkg::Lock::Timeout=")]
    assert 0 < int(lock.partition("=")[2]) < _bound(wait)
    assert fix[-5:] == ["env", "DEBIAN_FRONTEND=noninteractive",
                        "dpkg", "--configure", "-a"] and _bound(fix) > 0
    for at in (wait_at, fix_at):
        assert lines[at - 1].startswith("echo ")


def test_update_retries_any_index_fetch_failure():
    updates = [line for line in _action_steps()[1]["run"].splitlines()
               if line.strip().startswith("if sudo ") and "apt-get update " in line]
    assert len(updates) == 1
    assert "-o APT::Update::Error-Mode=any" in updates[0]


def test_composite_shell_timeouts_fit_calling_steps():
    sanitize, install = _action_steps()
    sanitize_bound, = re.findall(r"timeout (\d+)s rm ", sanitize["run"])
    assert 0 < int(sanitize_bound) < 60
    run = install["run"]
    setup = run.split("for attempt", 1)[0]
    for command in ("sh", "curl", "gpg"):
        assert re.search(rf"timeout \d+s {command} ", setup)
    bounds = [int(value) for value in re.findall(r"timeout (\d+)s ", setup)]
    setup_budget = sum(max(map(int, matches)) for line in setup.splitlines()
                       if (matches := re.findall(r"timeout (\d+)s ", line)))
    loops = _apt_loops(run)
    assert len(loops) == len(APT_LOOPS) and all(value > 0 for value in bounds)
    budget = int(sanitize_bound) + setup_budget + sum(
        loop["attempts"] * loop["timeout"] + (loop["attempts"] - 1) * (
            loop["sleep"] + sum(_bound(tokens) for _, tokens in loop["recovery"]))
        for loop in loops)
    callers = _client_steps()
    assert callers
    assert all(budget < int(step["timeout-minutes"]) * 60 for _, _, step in callers)


def test_composite_commands_are_announced_and_failures_remain_visible():
    for step in _action_steps():
        assert step.get("shell") == "bash"
        run = step["run"]
        assert "set -euo pipefail" in run
        assert "${{" not in run
        lines = [line.strip() for line in run.splitlines() if line.strip()]
        for index, line in enumerate(lines):
            code = line.split("#", 1)[0]
            segments = [part.strip() for part in re.split(r"\|\||&&|;", code)
                        if part.strip()]
            assert not segments or segments[-1] not in ("true", ":", "exit 0")
            if re.match(r"(?:if )?(?:sudo|timeout|curl|sleep|read|exit)\b", line):
                assert index > 0 and lines[index - 1].startswith("echo ")
                assert lines[index - 1][5:].strip("\"'")
    install = _action_steps()[1]
    assert install["env"]["CLAUDIT_APT_PACKAGES"] == "${{ inputs.packages }}"
    assert 'read -r -a packages <<< "$CLAUDIT_APT_PACKAGES"' in install["run"]
    assert '"${packages[@]}"' in install["run"]


def test_client_major_matches_postgres_container_major():
    reference = dockerfile_from_reference(
        ROOT / "compose/postgres/Dockerfile")
    match = re.search(r"/postgres:(\d+)(?:\.|@)", reference)
    assert match, reference
    for _, _, step in _client_steps():
        package = step.get("with", {}).get("packages", "")
        assert package == f"postgresql-client-{match[1]}"
