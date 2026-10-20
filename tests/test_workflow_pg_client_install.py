"""All apt installs share source sanitization and bounded failure handling."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

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
        assert step.get("timeout-minutes") == "5"
        assert step.get("with") == {"packages": "postgresql-client-16"}


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
    assert steps[index - 1].get("uses") == ACTION_REF
    assert not steps[index - 1].get("with", {}).get("packages")


def test_sanitization_always_runs_and_package_setup_is_optional():
    steps = _action_steps()
    assert len(steps) == 2
    sanitize, install = steps
    assert "if" not in sanitize
    assert re.search(r"sudo rm -f\s+/etc/apt/sources.list.d/microsoft-prod.list"
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


def test_composite_bounds_download_and_both_apt_retry_loops():
    run = _action_steps()[1]["run"]
    curls = [line for line in run.splitlines()
             if line.strip().startswith("curl ")]
    assert len(curls) == 1
    for bound in CURL_BOUNDS:
        assert bound in curls[0]
    loops = re.findall(r"for attempt in 1 2 3; do\n(.*?)\n *done", run, re.S)
    assert len(loops) == 2
    for operation, loop in zip(("update", "install"), loops):
        apts = [line for line in loop.splitlines()
                if line.strip().startswith("if sudo ")]
        assert len(apts) == 1
        assert f"apt-get {operation} " in apts[0]
        for bound in APT_BOUNDS:
            assert bound in apts[0]
        assert "break" in loop
        assert re.search(r'if \[ "\$attempt" -eq 3 \]; then', loop)
        assert "::error::" in loop
        assert "exit 1" in loop
        assert loop.index("exit 1") < loop.index("sleep 5")


def test_composite_commands_are_announced_and_failures_remain_visible():
    for step in _action_steps():
        assert step.get("shell") == "bash"
        assert 0 < int(step["timeout-minutes"]) <= 5
        run = step["run"]
        assert "set -euo pipefail" in run
        assert "${{" not in run
        lines = [line.strip() for line in run.splitlines() if line.strip()]
        for index, line in enumerate(lines):
            code = line.split("#", 1)[0]
            segments = [part.strip() for part in re.split(r"\|\||&&|;", code)
                        if part.strip()]
            assert not segments or segments[-1] not in ("true", ":", "exit 0")
            if re.match(r"(?:if )?(?:sudo|curl|sleep|read|exit)\b", line):
                assert index > 0 and lines[index - 1].startswith("echo ")
                assert lines[index - 1][5:].strip("\"'")
    install = _action_steps()[1]
    assert install["env"]["APT_PACKAGES"] == "${{ inputs.packages }}"
    assert 'read -r -a packages <<< "$APT_PACKAGES"' in install["run"]
    assert '"${packages[@]}"' in install["run"]


def test_client_major_matches_postgres_container_major():
    image = _load(ROOT / ".github/actions/postgres-image/action.yml")
    repositories = [step.get("env", {}).get("POSTGRES_REPOSITORY", "")
                    for step in image["runs"]["steps"]]
    majors = [match[1] for repository in repositories
              if (match := re.search(r"/postgres:(\d+)$", repository))]
    assert len(majors) == 1
    for _, _, step in _client_steps():
        package = step.get("with", {}).get("packages", "")
        assert package == f"postgresql-client-{majors[0]}"
