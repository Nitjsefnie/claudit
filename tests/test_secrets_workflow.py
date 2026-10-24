"""Check secret-scan wiring on platforms without a GitHub runner or gitleaks.
Source checks guard scan wiring, redacted public logs and binary digest
verification; they do not execute the remote workflow."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from tests.workflow_checks import is_checkout

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "secrets.yml"
CONFIG = ROOT / ".gitleaks.toml"


def _src() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _on_block() -> str:
    """Read the top-level trigger block; a trigger elsewhere would be a decoy."""
    m = re.search(r"^on:\n(.*?)^[A-Za-z]", _src(), re.S | re.M)
    assert m, "no top-level `on:` trigger block found in secrets.yml"
    return m.group(1)


def _step_block(marker: str) -> str:
    """Read one step so flags on a sibling cannot satisfy its checks."""
    lines = _src().splitlines()
    begin = next((i for i, ln in enumerate(lines) if marker in ln), None)
    assert begin is not None, f"no step matching {marker!r} in secrets.yml"
    for j in range(begin + 1, len(lines)):
        if re.match(r"^ {6}- |^ {4}[A-Za-z]", lines[j]):
            return "\n".join(lines[begin:j])
    return "\n".join(lines[begin:])


def test_workflow_file_exists():
    assert WORKFLOW.exists(), (
        ".github/workflows/secrets.yml is missing — the repository's only "
        "CI-resident secret scan cannot be edited away silently")


def test_push_trigger_is_limited_to_master():
    block = _on_block()
    assert re.search(r"^  push:", block, re.M), "no `push:` trigger"
    assert "branches: [master]" in block, (
        "push must be limited to [master] — a branch pushed while a pull "
        "request is open would run the scan a second time against the same "
        "SHA")
    assert not re.search(r"branches:\s*\[[^\]]*\bmain\b", block), (
        "a `main` branch filter does not belong here: this repo's default "
        "branch is master")


def test_schedule_has_exactly_one_daily_cron():
    crons = re.findall(r"^\s*-\s*cron:\s*'([^']*)'", _src(), re.M)
    assert len(crons) == 1, (
        f"expected exactly one cron in secrets.yml, found {len(crons)}: "
        f"{crons}")
    fields = crons[0].split()
    assert len(fields) == 5, f"cron {crons[0]!r} is not a 5-field schedule"
    # Daily: the day-of-week and day-of-month fields are both `*`.
    assert fields[2] == "*" and fields[4] == "*", (
        f"cron {crons[0]!r} is not a daily schedule")


def test_pull_request_and_dispatch_triggers_present():
    block = _on_block()
    assert re.search(r"^  pull_request:", block, re.M), (
        "pull_request must trigger the scan — the merge ref is what would "
        "land on master")
    assert re.search(r"^  workflow_dispatch:", block, re.M), (
        "workflow_dispatch must trigger the scan — a branch without a PR is "
        "checked by dispatch in this repo")


def test_history_scan_runs_after_checkout():
    doc = yaml.load(_src(), Loader=yaml.BaseLoader)
    scans = []
    for job in doc["jobs"].values():
        steps = job.get("steps", [])
        for index, step in enumerate(steps):
            if re.search(r"(?:^|\s)(?:\./)?gitleaks\s+(?:detect|git)\b",
                         step.get("run", "")):
                scans.append(step)
                assert any(is_checkout(previous) for previous in steps[:index])
    assert scans, "no Git history scan step found"


def test_permissions_block_and_timeout_present():
    src = _src()
    assert re.search(r"^permissions:\n  contents: read\s*$", src, re.M), (
        "the workflow must declare `permissions: contents: read`")
    assert re.search(r"^ {4}timeout-minutes: \d+", src, re.M), (
        "the job must carry a timeout-minutes bound")


def test_download_step_verifies_the_binary_digest():
    # Verify the downloaded tarball's digest; the release version may change.
    block = _step_block("Download gitleaks")
    url = re.search(
        r"https://github\.com/gitleaks/gitleaks/releases/download/"
        r"v\d+\.\d+\.\d+/(\S+)", block)
    assert url, "the download step must fetch a gitleaks release tarball"
    m = re.search(r"echo '([0-9a-f]{64})  (\S+)' \| sha256sum -c -", block)
    assert m, (
        "the download step must verify the tarball through "
        "`echo '<digest>  <file>' | sha256sum -c -`")
    assert m.group(2) == url.group(1), (
        "the digest must verify the tarball the download fetched, not "
        "some other file")


def test_scan_step_is_bare_verbose_and_redacted():
    block = _step_block("Scan the tree and the history")
    m = re.search(r"^ {8}run: (.+)$", block, re.M)
    assert m and "./gitleaks detect" in m.group(1), (
        "the scan step must run `./gitleaks detect`")
    cmd = m.group(1)
    for flag in ("--verbose", "--redact", "--config .gitleaks.toml"):
        assert flag in cmd, f"the scan command lost {flag}"
    assert "||" not in cmd and "&&" not in cmd and ";" not in cmd, (
        "this step's exit status IS the gate — no condition, fallback or "
        "error-swallowing suffix belongs on the command")
    assert "continue-on-error" not in _src(), (
        "continue-on-error anywhere in the workflow would turn findings "
        "green")


def test_gitleaks_config_exists_and_extends_the_default_ruleset():
    assert CONFIG.exists(), ".gitleaks.toml is missing at the repo root"
    cfg = CONFIG.read_text(encoding="utf-8")
    m = re.search(r"^\[extend\]\s*$(.*?)(?=^\[|\Z)", cfg, re.S | re.M)
    assert m, ".gitleaks.toml has no [extend] section"
    assert re.search(r"^useDefault\s*=\s*true\s*$", m.group(1), re.M), (
        "[extend] must set useDefault = true — the default ruleset is what "
        "makes this a pattern scanner instead of a literal list")
