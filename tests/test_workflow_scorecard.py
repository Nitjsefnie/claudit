"""Tripwires for the OpenSSF Scorecard workflow (issue #554).

Scorecard is a repository trend signal, not a gate, and two properties of it
are load-bearing rather than incidental:

  - it never runs on a contribution event. A `push` or `pull_request`
    trigger would make it a second verdict on a commit ci-gate already
    judges, and release.yml's waiter — `scripts/ci/release_gate.py` — treats
    every check run produced by a push- or pull-request-event run on the
    SHA it is about to tag as a gate. A red scorecard on a master push
    would therefore refuse the release. These tests read that module's own
    event set rather than restating it, so widening `GATE_EVENTS` re-arms
    the check here instead of silently making scorecard a release blocker.

  - it publishes. `publish_results` plus the SARIF upload are what put the
    findings in the Security tab and the score in the public record; a
    refactor that quietly drops either leaves a workflow that scans and
    reports nothing.

Asserted on the YAML-DECODED structure, the way GitHub reads it, except the
action pins, which are comments and do not survive parsing.
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
SCORECARD = WORKFLOWS / "scorecard.yml"
CODEQL = WORKFLOWS / "codeql.yml"

# Any commit; release_gate.py takes it as an opaque handle.
SHA = "d" * 40

# The one guard this workflow's job may carry, in full. Compared for
# EQUALITY, not by substring: an `if:` is a Boolean expression, and every
# way of widening one keeps the context names it already mentions.
FORK_GUARD = (
    "${{ !github.event.repository.fork && github.ref == "
    "format('refs/heads/{0}', github.event.repository.default_branch) }}"
)

# The trigger events that make a check run a gate of its commit, by way of
# release_gate.py. Kept as names here and cross-checked against the module
# below, so the list cannot drift into a second, laxer copy.
CONTRIBUTION_EVENTS = ("push", "pull_request", "pull_request_target",
                       "merge_group")


def _load(name):
    """Import scripts/ci/<name>.py by path (scripts/ci is not a package)."""
    path = REPO_ROOT / "scripts" / "ci" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


release_gate = _load("release_gate")


def _load_workflow(path: Path) -> dict:
    # BaseLoader, not safe_load: YAML 1.1 parses the bare key `on` as the
    # boolean True, and every test here reads the trigger map by name.
    return yaml.load(path.read_text(encoding="utf-8"),
                     Loader=yaml.BaseLoader) or {}


def _triggers() -> dict:
    # the oracle must be live: a file that stopped parsing its triggers
    # must not silence this into a vacuous pass
    on = _load_workflow(SCORECARD).get("on") or {}
    assert on, f"{SCORECARD.name} declares no trigger"
    return on


def _steps() -> list[dict]:
    steps = []
    for job in (_load_workflow(SCORECARD).get("jobs") or {}).values():
        steps.extend((job or {}).get("steps") or [])
    return steps


def _step_using(action: str) -> dict:
    for step in _steps():
        ref = str((step or {}).get("uses") or "")
        if ref.split("@", 1)[0].rstrip("/") == action:
            return step
    raise AssertionError(f"no step uses {action}")


# --- not a gate -----------------------------------------------------------

def _cron_expressions(path: Path) -> list[str]:
    """Every five-field cron a workflow schedules, in file order."""
    entries = (_load_workflow(path).get("on") or {}).get("schedule") or []
    return [str((entry or {}).get("cron") or "") for entry in entries]


def test_the_gate_event_set_this_file_protects_is_the_one_in_force():
    """The claim this file rests on, read from the module rather than
    restated: `GATE_EVENTS` is exactly the two events a red scorecard run on
    them would refuse a release over. Both directions matter. Widening it
    there makes a scorecard run on the new event a row release.yml waits on,
    and the trigger test below stops being the whole story; NARROWING it
    makes this file's docstring — and the comment in the workflow — assert a
    release-blocking path the module no longer has.
    """
    assert sorted(release_gate.GATE_EVENTS) == ["pull_request", "push"]
    assert set(CONTRIBUTION_EVENTS) >= release_gate.GATE_EVENTS, (
        "CONTRIBUTION_EVENTS must cover every gate event release_gate.py "
        "names; a trigger test that misses one lets a release-blocking run "
        "through"
    )


def _release_runs(*runs: tuple[int, str, str]) -> list[dict]:
    return [{"id": run_id, "head_sha": SHA, "event": event, "path": path}
            for run_id, event, path in runs]


def _path() -> str:
    """This workflow's own repo-relative path, read off the file — a
    release_gate.py run's `path` is what the dispatch case compares, so a
    literal here would keep asserting about a file that no longer exists."""
    return str(SCORECARD.relative_to(REPO_ROOT))


def _scorecard_check(run_id: int) -> dict:
    return {"name": "Scorecard analysis", "status": "completed",
            "conclusion": "failure", "app": {"slug": "github-actions"},
            "html_url": f"https://github.com/Nitjsefnie/claudit/actions/runs/"
                        f"{run_id}/job/1"}


def _judged_by_release(check_run: dict, runs: list[dict]) -> bool:
    rows, _notes = release_gate.select(
        [check_run], runs, SHA, self_run_id="1", self_job="release",
        self_path=".github/workflows/release.yml")
    return bool(rows)


def test_a_scheduled_or_dispatched_scorecard_run_is_not_a_release_gate():
    """The workflow's own comment claims release.yml's waiter drops this
    workflow's rows. Drive release_gate.py with exactly that shape — a RED
    scorecard check run, produced by a schedule run and then by a dispatch
    run, on a commit that also carries ci-gate's push run — and let the
    module answer, rather than restating its filter here.
    """
    gates = _release_runs((2, "push", ".github/workflows/ci-gate.yml"))
    for event, label in (("schedule", "a weekly run"),
                         ("workflow_dispatch", "a manual run")):
        runs = gates + _release_runs((3, event, _path()))
        assert not _judged_by_release(_scorecard_check(3), runs), (
            f"{label} of scorecard.yml is a row release.yml waits on, so a "
            f"{event} run that goes red refuses the release"
        )


def test_the_dispatch_case_is_dropped_for_its_path_not_for_its_event():
    """The negative space of the assertion above. release_gate.py judges a
    `workflow_dispatch` of a workflow that also gates the commit, so the same
    event on ci-gate's own path IS judged — which is what makes the drop
    above a property of this file's triggers rather than of the filter
    dropping every dispatch."""
    runs = _release_runs(
        (2, "push", ".github/workflows/ci-gate.yml"),
        (3, "workflow_dispatch", ".github/workflows/ci-gate.yml"))
    assert _judged_by_release(_scorecard_check(3), runs), (
        "release_gate.py stopped judging a dispatch of a gating workflow, so "
        "the drop asserted above no longer says anything about scorecard"
    )


def test_scorecard_never_runs_on_a_contribution_event():
    triggers = _triggers()
    offending = sorted(set(triggers) & set(CONTRIBUTION_EVENTS))
    assert not offending, (
        "scorecard runs on "
        f"{offending}, which makes it a second verdict on a commit "
        "ci-gate already gates and — because release_gate.py judges every "
        "check run a push- or pull-request-event run puts on the SHA it "
        "tags — a red scorecard would refuse the release. Schedule and "
        "workflow_dispatch only."
    )


def test_it_still_runs_weekly_and_on_demand():
    triggers = _triggers()
    assert "workflow_dispatch" in triggers, (
        "a manual re-scan after landing a hardening change is how the next "
        "run's score gets verified; drop the trigger and the only way to "
        "see a fix is to wait for Saturday"
    )
    crons = _cron_expressions(SCORECARD)
    assert crons, "the weekly scan is the whole point of the workflow"
    for entry in crons:
        assert len(entry.split()) == 5, (
            f"cron is not a five-field expression: {entry!r}")
        minute, hour, dom, month, dow = entry.split()
        assert dom == "*" and month == "*", (
            f"{entry} runs on named calendar days; a weekly scan must be "
            "the day-of-week field alone, or it fires several times a year"
        )
        assert dow != "*", f"{entry} runs daily, not weekly"
        assert minute.isdigit() and hour.isdigit(), (
            f"{entry} is not at a fixed minute — GitHub queues scheduled "
            "workflows onto the hour, and the offset is what keeps this run "
            "off the slots the other workflows already hold"
        )


def _field_values(field: str, low: int, high: int) -> set[int]:
    """Every value one cron field takes: `*`, `*/n`, `a-b`, `a-b/n`, a bare
    value, and comma lists of those.

    A literal `(minute, hour)` pair is not a schedule. `23 * * * *` fires at
    02:23 as surely as at 23:00, so comparing it against a `41 2 * * 6` as
    the pair `("23", "*")` never meets it and the collision goes unseen —
    which is exactly the case this repository's hourly pricing refresh is.
    """
    values: set[int] = set()
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, _, raw_step = part.partition("/")
            step = int(raw_step)
        if part == "*":
            start, end = low, high
        elif "-" in part:
            start_s, _, end_s = part.partition("-")
            start, end = int(start_s), int(end_s)
        else:
            start = end = int(part)
        values.update(range(start, end + 1, step))
    return values


def test_its_slot_collides_with_no_other_workflow_cron():
    """One repository, one runner queue: two workflows due in the same
    minute can both land late. This file's slot must fall outside every
    other workflow's, with each schedule expanded to the minutes it actually
    fires at — an hourly or wildcard schedule covers most of the day."""
    def minutes(path: Path) -> set[tuple[int, int]]:
        fired: set[tuple[int, int]] = set()
        for entry in _cron_expressions(path):
            fields = entry.split()
            if len(fields) != 5:
                continue
            minute, hour = fields[0], fields[1]
            hours = _field_values(hour, 0, 23)
            for value in _field_values(minute, 0, 59):
                for hour_value in hours:
                    fired.add((value, hour_value))
        return fired

    mine = minutes(SCORECARD)
    assert mine, "no schedule to check"
    for path in sorted(WORKFLOWS.glob("*.yml")) + sorted(
            WORKFLOWS.glob("*.yaml")):
        if path == SCORECARD:
            continue
        clash = mine & minutes(path)
        assert not clash, (
            f"{path.name} and scorecard.yml both fire at "
            f"{sorted(clash)[0][1]:02d}:{sorted(clash)[0][0]:02d} UTC — "
            "GitHub queues scheduled workflows, so both start late"
        )


# --- it publishes ---------------------------------------------------------

def test_the_job_publishes_and_uploads_sarif():
    scorecard_step = _step_using("ossf/scorecard-action")
    with_ = scorecard_step.get("with") or {}
    assert with_.get("publish_results") in ("true", True), (
        "without publish_results the scan runs and its score is thrown away: "
        "no badge, no API entry, nothing replacing the weekly public scan"
    )
    assert with_.get("results_format") == "sarif"
    assert with_.get("results_file"), (
        "results_file is what both consumers below read; unset, the upload "
        "steps have nothing to upload"
    )
    upload = _step_using("github/codeql-action/upload-sarif")
    assert (upload.get("with") or {}).get("sarif_file") == with_[
        "results_file"], (
        "the SARIF upload reads a different file than the analysis wrote"
    )


def test_the_analysis_runs_after_a_checkout():
    """Scorecard measures the repository, so it reads the tree the run
    checked out. Without the checkout step it scans whatever the runner
    image happens to carry."""
    order = [str((step or {}).get("uses") or "").split("@", 1)[0]
             for step in _steps()]
    assert order, "no steps"
    checkout = order.index("actions/checkout")
    analysis = order.index("ossf/scorecard-action")
    assert checkout < analysis, (
        "the analysis runs before the checkout — it would score the "
        "runner's own tree"
    )


def test_the_analysis_runs_on_a_hosted_ubuntu_runner():
    """Upstream refuses to publish from a job with a container, a service
    or a self-hosted runner; the SARIF upload silently never lands."""
    jobs = _load_workflow(SCORECARD).get("jobs") or {}
    assert len(jobs) == 1, (
        f"expected the one analysis job, found {sorted(jobs)} — each extra "
        "job needs its own publishing permissions re-derived"
    )
    job = next(iter(jobs.values()))
    assert job.get("runs-on") == "ubuntu-latest", job.get("runs-on")
    for shape in ("container", "services", "env", "defaults"):
        assert shape not in job, (
            f"the analysis job declares `{shape}`, which upstream refuses to "
            "publish with"
        )


# --- permissions ----------------------------------------------------------

def test_write_permissions_live_on_the_job_not_the_workflow():
    """Upstream's publishing rules reject a workflow-level write grant, and
    a top-level write grant would hand every future job in this file the
    ability to publish."""
    top = _load_workflow(SCORECARD).get("permissions")
    assert top == {"contents": "read"}, (
        f"workflow-level permissions are {top}; publishing requires "
        "read-only here and the two write grants on the job"
    )
    job = next(iter((_load_workflow(SCORECARD).get("jobs") or {}).values()))
    assert (job.get("permissions") or {}) == {
        "security-events": "write",
        "id-token": "write",
        "contents": "read",
    }, (
        "the job's permissions must be exactly the SARIF upload "
        "(security-events), the OIDC signing of the published result "
        "(id-token) and a read of the tree — anything more is a grant this "
        "scan does not need, and zizmor reads it as one"
    )


def test_a_fork_or_a_non_default_branch_never_publishes():
    """The published record is the repository's. A fork's run files into the
    fork's store and another branch's under this repository's name, so both
    would overwrite the score the badge shows.

    Compared to the WHOLE decoded expression, because a guard is a Boolean
    and every way of widening it keeps both context names on the line:
    `|| always()` and `|| github.event_name == 'workflow_dispatch'` each
    satisfy a substring check and each let the run they exclude publish.
    Equality also catches an inverted clause, which a name-only check reads
    as covered.
    """
    job = next(iter((_load_workflow(SCORECARD).get("jobs") or {}).values()))
    condition = str(job.get("if") or "")
    assert condition == FORK_GUARD, (
        "the job's `if:` must be exactly the fork-and-default-branch guard, "
        f"so no clause can be appended or inverted; found {condition!r}"
    )


def test_no_step_is_conditional():
    """Every step runs whenever the job does, or the workflow scans and
    publishes nothing while still reading green. The one guard that is
    wanted — fork and default branch — belongs on the job, so a step-level
    `if` here can only ever remove work."""
    job = next(iter((_load_workflow(SCORECARD).get("jobs") or {}).values()))
    skipped = [(step or {}).get("name") or (step or {}).get("uses")
               for step in job.get("steps") or []
               if (step or {}).get("if") not in (None, "")]
    assert not skipped, (
        "these steps can be skipped, so the run reports green without "
        f"producing or uploading what it exists to produce: {skipped}"
    )


def test_the_sarif_upload_shares_codeqls_pin():
    """One SARIF dialect across both producers. codeql-action records its
    version and refuses a later step at a different one (issue #335), and
    SARIF processing fails at the point of upload when they disagree."""
    def codeql_pins() -> set[str]:
        shas = set()
        for job in (_load_workflow(CODEQL).get("jobs") or {}).values():
            for step in (job or {}).get("steps") or []:
                ref = str((step or {}).get("uses") or "")
                action, _, sha = ref.partition("@")
                if action.rstrip("/").startswith("github/codeql-action/"):
                    shas.add(sha)
        return shas

    shas = codeql_pins()
    assert shas, "no codeql-action pin found in codeql.yml — extend this test"
    upload = _step_using("github/codeql-action/upload-sarif")
    assert str(upload["uses"]).partition("@")[2] in shas, (
        "scorecard's SARIF upload and codeql.yml's pins disagree: "
        f"{upload['uses']} against {sorted(shas)}"
    )


def test_the_checkout_does_not_leave_the_token_behind():
    """zizmor reads `persist-credentials: false` as the difference between
    a checkout and a credential hand-over to every later step."""
    checkout = _step_using("actions/checkout")
    assert (checkout.get("with") or {}).get("persist-credentials") in (
        "false", False), (
        "actions/checkout without persist-credentials: false leaves the job "
        "token in .git/config for the rest of the job to use"
    )


def test_the_workflow_is_a_tracked_file():
    """gitignore is deny-by-default here: an unlisted path is invisible to
    `git status`, and a workflow that is never committed never runs."""
    tracked = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files"],
        capture_output=True, text=True, check=True).stdout.split()
    assert ".github/workflows/scorecard.yml" in tracked
