"""Workflows leave apt to the shared composite that sanitizes runner sources."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from tests.workflow_checks import assert_apt_sanitizer

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
ACTION = ROOT / ".github" / "actions" / "apt-install" / "action.yml"
ACTION_REF = "$/.github/actions/apt-install"


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


def test_sanitization_always_runs():
    removals = [step for step in _action_steps() if re.search(
        r"sudo timeout \d+s rm -f\s+/etc/apt/sources.list.d/microsoft-prod.list"
        r"\s+/etc/apt/sources.list.d/microsoft-prod.sources", step.get("run", ""))]
    assert len(removals) == 1
    assert "if" not in removals[0]
    assert "apt-get" not in removals[0]["run"]


def test_composite_shell_timeouts_fit_calling_steps():
    bounds = [int(value) for step in _action_steps()
              for value in re.findall(r"timeout (\d+)s ", step.get("run", ""))]
    assert bounds and all(value > 0 for value in bounds)
    callers = [step for _, _, step in _workflow_steps()
               if step.get("uses") == ACTION_REF]
    assert callers
    assert all(sum(bounds) < int(step.get("timeout-minutes", "0")) * 60
               for step in callers)
