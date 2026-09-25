"""Source-level pins for the CI secret scan (issue #130).

Nothing here executes a workflow: the scan runs on GitHub's runners over
the pushed history, and this suite also runs on platforms where neither a
GitHub runner nor gitleaks exists, so the only pin that travels with the
repo is the workflow text itself. Same logic as test_panel_wiring.py's
source-level JSX guards — a workflow can pass the whole suite while
scanning half the history, leaking findings into a public log, or
downloading an unverified binary. Each test names the property it pins
and why an edit that drops it is a regression, not a refactor.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "secrets.yml"
CONFIG = ROOT / ".gitleaks.toml"

# The sha256 of gitleaks_8.30.1_linux_x64.tar.gz, verified by hand against
# the official v8.30.1 release the day the workflow shipped. A moved digest
# is a supply-chain event: the workflow may not scan with a binary whose
# checksum nobody re-verified, so the bump edits this constant too.
PINNED_DIGEST = (
    "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb")


def _src() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _on_block() -> str:
    """The top-level `on:` trigger block, sliced to the next top-level key.

    Searching the whole file for `pull_request:` would accept it spelled
    under the wrong trigger; the slice is what makes "push is limited to
    master" checkable at all.
    """
    m = re.search(r"^on:\n(.*?)^[A-Za-z]", _src(), re.S | re.M)
    assert m, "no top-level `on:` trigger block found in secrets.yml"
    return m.group(1)


def _step_block(marker: str) -> str:
    """One step's YAML, from its first line to the next step or job key.

    A whole-file search accepts `--verbose` sitting on the wrong step;
    slicing from the step's own line to the next `      - ` sibling (or a
    4-space job key) is what ties the flags to the step that must carry
    them.
    """
    lines = _src().splitlines()
    begin = next((i for i, ln in enumerate(lines) if marker in ln), None)
    assert begin is not None, f"no step matching {marker!r} in secrets.yml"
    for j in range(begin + 1, len(lines)):
        if re.match(r"^ {6}- |^ {4}[A-Za-z]", lines[j]):
            return "\n".join(lines[begin:j])
    return "\n".join(lines[begin:])


# --- the workflow exists -----------------------------------------------------

def test_workflow_file_exists():
    assert WORKFLOW.exists(), (
        ".github/workflows/secrets.yml is missing — the repository's only "
        "CI-resident secret scan cannot be edited away silently")


# --- triggers ----------------------------------------------------------------

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


# --- hardening pins ----------------------------------------------------------

def test_checkout_fetches_full_history_and_drops_credentials():
    block = _step_block("actions/checkout@")
    assert "fetch-depth: 0" in block, (
        "the scan reads `git log -p`, so a shallow clone would scan a "
        "suffix of history and call the rest clean")
    assert "persist-credentials: false" in block, (
        "nothing here pushes; the job token must not survive the checkout")


def test_permissions_block_and_timeout_present():
    src = _src()
    assert re.search(r"^permissions:\n  contents: read\s*$", src, re.M), (
        "the workflow must declare `permissions: contents: read`")
    assert re.search(r"^ {4}timeout-minutes: \d+", src, re.M), (
        "the job must carry a timeout-minutes bound")


# --- the scan itself ---------------------------------------------------------

def test_download_step_pins_the_binary_digest():
    block = _step_block("Download gitleaks")
    assert ("https://github.com/gitleaks/gitleaks/releases/download/"
            "v8.30.1/") in block, "the download URL must name v8.30.1"
    m = re.search(r"echo '([0-9a-f]{64})  (\S+)' \| sha256sum -c -", block)
    assert m, (
        "the download step must verify the tarball through "
        "`echo '<digest>  <file>' | sha256sum -c -`")
    assert m.group(1) == PINNED_DIGEST, (
        "the workflow's pinned digest moved without this test moving with "
        "it — re-verify the new checksum against the official release "
        "before scanning with it")


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


# --- action pinning ----------------------------------------------------------

def test_every_action_is_digest_pinned_with_a_version_comment():
    lines = [ln for ln in _src().splitlines() if re.search(r"\buses:", ln)]
    assert lines, "no `uses:` lines found — the guard would pass vacuously"
    for ln in lines:
        assert re.search(r"uses:\s*\S+@[0-9a-f]{40}\s+# v\S+$", ln), (
            f"action not hash-pinned with a `# v` version comment: {ln.strip()!r}")


# --- the gitleaks config -----------------------------------------------------

def test_gitleaks_config_exists_and_extends_the_default_ruleset():
    assert CONFIG.exists(), ".gitleaks.toml is missing at the repo root"
    cfg = CONFIG.read_text(encoding="utf-8")
    m = re.search(r"^\[extend\]\s*$(.*?)(?=^\[|\Z)", cfg, re.S | re.M)
    assert m, ".gitleaks.toml has no [extend] section"
    assert re.search(r"^useDefault\s*=\s*true\s*$", m.group(1), re.M), (
        "[extend] must set useDefault = true — the default ruleset is what "
        "makes this a pattern scanner instead of a literal list")
