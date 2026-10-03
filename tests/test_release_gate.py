"""Unit tests for the release waiter's gate selection.

`release.yml` used to project a commit's check runs through
`select(.name | startswith("release") | not)`. That dropped its own job and
two other things as well: any gate whose check name begins `release`
(issue #557 — the name is not the waiter, and dropping a red one releases
over it), and, by omission, every check a NON-gate workflow happened to
publish on the SHA — `claim`, whose `issue_comment` run is stamped with the
default branch's tip, so one `/claim` comment anywhere in the repository
puts a red `claim` row on master's head commit (issue #579).

The selection is now scripts/ci/release_gate.py, keyed on the workflow run
that PRODUCED each check run rather than on the check's name. These pin,
per assertion, that the old expression would have got it wrong:

- a push-run check named `release-anything` is JUDGED (#557);
- a check named exactly `release` from some OTHER run is JUDGED (#557's
  converse — identity is the run, not the name);
- the waiter's OWN job is still skipped, by exact name and run identity;
- the waiter's own WORKFLOW is skipped at any attempt — an earlier
  `release` run on the commit is a push run, so it lands in the gate set
  and would otherwise make every manual re-cut of a commit whose first
  release run failed refuse on its predecessor's answer;
- an `issue_comment` run's check is skipped (#579);
- a `workflow_dispatch` run of a workflow that does not gate the commit is
  skipped (the hourly rate-refresh bot publishes `refresh` / `verdict`
  rows on master's tip from an external hourly dispatch);
- a `workflow_dispatch` run of a workflow that DOES gate the commit is
  judged, so a manually dispatched gate still counts;
- a run stamped with another commit's SHA is skipped;
- and that an UNLISTED self run excludes nothing by path, so the path rule
  cannot quietly become a no-op when the listing lags — the state in which
  no path at all, derived or listed, can drop anything;

and the fail-closed side, without which the filter would be a way to lose a
gate rather than to shed noise:

- a check whose producing run cannot be resolved is JUDGED;
- while no push run is known for the commit, a dispatch row is JUDGED;
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "ci" / "release_gate.py"

SHA = "26081d5f19f53e05d7dce9b01e7b9820f101150f"
OTHER_SHA = "3b4e41b0000000000000000000000000000000000"
SELF_RUN = "90000000001"
SELF_JOB = "release"


def _load():
    spec = importlib.util.spec_from_file_location("release_gate", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["release_gate"] = module
    spec.loader.exec_module(module)
    return module


rg = _load()


def _check(name, run_id, status="completed", conclusion: str | None = "success",
           app="github-actions"):
    """One check run, as the commit check-runs endpoint returns it.

    ``conclusion`` is None on an unfinished check, which is what the
    endpoint sends and what the projection has to read as `-`.
    """
    return {
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "app": {"slug": app},
        "html_url": (f"https://github.com/Nitjsefnie/claudit/actions/runs/"
                     f"{run_id}/job/1"),
    }


def _foreign_check(name):
    """A check run posted by something that is not this repository's CI.

    Its html_url carries no run id, so nothing about which workflow run
    produced it can be read — the fail-closed shape.
    """
    return {
        "name": name,
        "status": "completed",
        "conclusion": "success",
        "app": {"slug": "acme-ci"},
        "html_url": "https://acme.example/build/42",
    }


def _run(run_id, event, head_sha=SHA, path=".github/workflows/ci-gate.yml",
         name="ci gate"):
    return {"id": int(run_id), "event": event, "head_sha": head_sha,
            "path": path, "name": name}


def _judged(checks, runs, sha=SHA, self_run_id: str | None = SELF_RUN,
            self_path=None):
    rows, _ = rg.select(checks, runs, sha, self_run_id, SELF_JOB, self_path)
    return [line.split("\t")[2] for line in rows]


def _dropped(checks, runs, sha=SHA, self_run_id: str | None = SELF_RUN,
             self_path=None):
    rows, notes = rg.select(checks, runs, sha, self_run_id, SELF_JOB,
                            self_path)
    return [line.split("\t")[2] for line in rows], len(notes)


PUSH_RUN = "37138486204"
FRESHNESS_RUN = "37138486053"
DISPATCH_RUN = "37138518367"
CLAIM_RUN = "37138981898"
OWN_RUN = SELF_RUN
RELEASE_PATH = ".github/workflows/release.yml"
EARLIER_RELEASE_RUN = "300"


# --- issue #557: the name is not the waiter -------------------------------

def test_a_push_run_check_named_release_anything_is_judged():
    # `startswith("release")` drops this row whatever it concluded, so a
    # gate that failed would still release.
    checks = [_check("release-notes", PUSH_RUN, conclusion="failure"),
              _check("aggregate", PUSH_RUN)]
    runs = [_run(PUSH_RUN, "push")]
    assert _judged(checks, runs) == ["release-notes", "aggregate"]


def test_a_check_named_exactly_release_from_another_run_is_judged():
    # Same name as the waiter's own job, different producing run: a real
    # gate, and dropping it on the name is the same defect one step later.
    checks = [_check(SELF_JOB, PUSH_RUN, conclusion="failure"),
              _check("aggregate", PUSH_RUN)]
    runs = [_run(PUSH_RUN, "push")]
    assert _judged(checks, runs) == [SELF_JOB, "aggregate"]


def test_the_waiters_own_job_is_skipped_by_name_and_run_identity():
    checks = [_check(SELF_JOB, OWN_RUN),
              _check("aggregate", PUSH_RUN)]
    runs = [_run(PUSH_RUN, "push"), _run(OWN_RUN, "push",
                                         path=".github/workflows/release.yml")]
    judged, dropped = _dropped(checks, runs)
    assert judged == ["aggregate"]
    assert dropped == 1


def test_the_own_job_is_skipped_even_its_run_id_cannot_be_read():
    # The html_url never fails to parse for Actions' own rows; when it
    # does, the exact name is still enough, and the wait must not deadlock
    # on the job that is waiting on it.
    check = _check(SELF_JOB, OWN_RUN)
    check["html_url"] = ""
    assert _judged([check], [_run(PUSH_RUN, "push")]) == []


# --- issue #579: a run stamped with another commit's claim ---------------

def test_an_issue_comment_runs_check_is_skipped():
    # The incident: `claim` / failure from run 37138981898, event
    # issue_comment, head_sha the master commit under release.
    checks = [_check("claim", CLAIM_RUN, conclusion="failure"),
              _check("aggregate", PUSH_RUN)]
    runs = [_run(PUSH_RUN, "push"),
            _run(CLAIM_RUN, "issue_comment",
                 path=".github/workflows/claim.yml", name="claim")]
    judged, dropped = _dropped(checks, runs)
    assert judged == ["aggregate"]
    assert dropped == 1


def test_a_schedule_and_a_workflow_run_triggered_check_is_skipped():
    # ratchet-push.yml is triggered BY ci-gate completing and pushes to
    # master; its row lands on master's tip for an unrelated event.
    checks = [_check("ratchet", "1"), _check("aggregate", PUSH_RUN)]
    runs = [_run(PUSH_RUN, "push"),
            _run("1", "workflow_run",
                 path=".github/workflows/ratchet-push.yml", name="ratchet")]
    assert _judged(checks, runs) == ["aggregate"]


# --- the hourly dispatch that is not a gate ------------------------------

def test_a_dispatch_of_a_workflow_that_never_gates_the_commit_is_skipped():
    # refresh-pricing.yml runs on no push trigger at all; an external
    # hourly dispatch runs it on master, so `refresh` / `push` / `verdict`
    # rows sit on master's tip on most hours.
    checks = [_check("refresh", DISPATCH_RUN, conclusion="failure"),
              _check("verdict", DISPATCH_RUN),
              _check("aggregate", PUSH_RUN)]
    runs = [_run(PUSH_RUN, "push"),
            _run(DISPATCH_RUN, "workflow_dispatch",
                 path=".github/workflows/refresh-pricing.yml",
                 name="refresh-pricing")]
    judged, _ = _dropped(checks, runs)
    assert judged == ["aggregate"]


def test_a_dispatch_of_a_workflow_that_also_gates_the_commit_is_judged():
    # A gate workflow dispatched by hand at the commit still vouches for
    # it, and dropping it would lose a gate rather than shed noise.
    checks = [_check("aggregate", PUSH_RUN),
              _check("ci-gate-manual", DISPATCH_RUN, conclusion="failure")]
    runs = [_run(PUSH_RUN, "push"), _run(DISPATCH_RUN, "workflow_dispatch")]
    assert _judged(checks, runs) == ["aggregate", "ci-gate-manual"]


def test_a_run_stamped_with_another_commit_is_skipped():
    checks = [_check("aggregate", PUSH_RUN),
              _check("stale", FRESHNESS_RUN)]
    runs = [_run(PUSH_RUN, "push"),
            _run(FRESHNESS_RUN, "push", head_sha=OTHER_SHA,
                 path=".github/workflows/gate-freshness.yml", name="freshness")]
    assert _judged(checks, runs) == ["aggregate"]


# --- fail-closed: what the filter must never drop ------------------------

def test_a_check_whose_producing_run_is_unresolvable_is_judged():
    # No run id in the html_url, so nothing proves the row is noise.
    checks = [_foreign_check("external-fancy"),
              _check("aggregate", PUSH_RUN)]
    runs = [_run(PUSH_RUN, "push")]
    assert _judged(checks, runs) == ["external-fancy", "aggregate"]


def test_a_check_run_id_absent_from_the_run_listing_is_judged():
    # The listing is a second API call and can lag the check-runs one.
    checks = [_check("aggregate", PUSH_RUN), _check("not-yet-listed", "42")]
    runs = [_run(PUSH_RUN, "push")]
    assert _judged(checks, runs) == ["aggregate", "not-yet-listed"]


# --- the waiter's own workflow, at any attempt --------------------------


def test_an_earlier_release_push_runs_failed_row_is_skipped():
    # Round 1's blocker. release.yml's own push run on this commit is a
    # PUSH run, so it is in gate_paths and would be judged: once it fails —
    # the 2700 s deadline, a transient `gh release create`, a cancel — a
    # later re-cut of that commit refuses on its predecessor's answer.
    # Two things drop the predecessor's row here, and neither is
    # --self-path: the re-cut is a dispatch of the TIP, so its own run IS
    # in the listing and resolves the path; and the earlier release run is
    # itself a PUSH run, which is what puts release.yml into gate_paths in
    # the first place.
    checks = [_check("aggregate", PUSH_RUN),
              _check(SELF_JOB, EARLIER_RELEASE_RUN, conclusion="failure")]
    runs = [_run(PUSH_RUN, "push"),
            _run(OWN_RUN, "workflow_dispatch", path=RELEASE_PATH),
            _run(EARLIER_RELEASE_RUN, "push", path=RELEASE_PATH)]
    judged, _ = _dropped(checks, runs)
    assert judged == ["aggregate"]


def test_an_earlier_dispatched_release_runs_row_is_skipped():
    # The same predecessor reached by the other trigger: a dispatch of
    # release.yml puts release.yml in gate_paths just as a push does.
    checks = [_check("aggregate", PUSH_RUN),
              _check(SELF_JOB, EARLIER_RELEASE_RUN, conclusion="failure")]
    runs = [_run(PUSH_RUN, "push"),
            _run(OWN_RUN, "push", path=RELEASE_PATH),
            _run(EARLIER_RELEASE_RUN, "workflow_dispatch",
                 path=RELEASE_PATH)]
    judged, _ = _dropped(checks, runs)
    assert judged == ["aggregate"]


TIP_SHA = "5e0c0b10000000000000000000000000000000000"


def test_a_re_cut_by_dispatch_of_a_non_tip_commit_proceeds():
    # Round 2's blocker, and the COMMON re-cut: master's commits are
    # 30-60 minutes apart, so the 2700 s deadline fires after the tip has
    # moved. A `workflow_dispatch` with `sha=X` records its OWN run
    # against the BRANCH TIP, so it is absent from a listing filtered by
    # X's head SHA — and with no path the selector judged the earlier
    # release run's row and refused. A regression against master, where
    # the old name-prefix filter dropped that row.
    checks = [_check("aggregate", PUSH_RUN),
              _check(SELF_JOB, EARLIER_RELEASE_RUN, conclusion="failure")]
    runs = [_run(PUSH_RUN, "push", head_sha=OTHER_SHA),
            _run(EARLIER_RELEASE_RUN, "push", head_sha=OTHER_SHA,
                 path=RELEASE_PATH)]
    judged, _ = _dropped(checks, runs, sha=OTHER_SHA,
                         self_path=RELEASE_PATH)
    assert judged == ["aggregate"]


def test_that_re_cut_is_refused_without_the_self_path():
    # The isolating control for --self-path: the listing carries no row for
    # the self run (it is stamped with the tip), so with no path supplied
    # the earlier release row is judged and the re-cut refuses. Everything
    # else about this fixture already holds.
    checks = [_check("aggregate", PUSH_RUN),
              _check(SELF_JOB, EARLIER_RELEASE_RUN, conclusion="failure")]
    runs = [_run(PUSH_RUN, "push", head_sha=OTHER_SHA),
            _run(EARLIER_RELEASE_RUN, "push", head_sha=OTHER_SHA,
                 path=RELEASE_PATH)]
    judged, _ = _dropped(checks, runs, sha=OTHER_SHA)
    assert judged == ["aggregate", SELF_JOB]


def test_a_gate_job_named_release_is_still_judged_with_the_self_path():
    # The path exclusion is this workflow's FILE, so a gate workflow with
    # a job of the same name keeps its row — unlike the name rule, which
    # would have dropped it.
    checks = [_check("aggregate", PUSH_RUN),
              _check(SELF_JOB, PUSH_RUN, conclusion="failure")]
    runs = [_run(PUSH_RUN, "push"),
            _run(OWN_RUN, "workflow_dispatch", path=RELEASE_PATH)]
    judged, _ = _dropped(checks, runs, self_path=RELEASE_PATH)
    assert judged == ["aggregate", SELF_JOB]


def test_an_unlisted_self_run_excludes_nothing_by_path():
    # The listing fallback: with no --self-path and no self run row, the
    # path is unknown, and an unknown path must not exclude a
    # predecessor's red row. This is what keeps the fallback fail-closed.
    checks = [_check("aggregate", PUSH_RUN),
              _check(SELF_JOB, EARLIER_RELEASE_RUN, conclusion="failure")]
    runs = [_run(PUSH_RUN, "push"),
            _run(EARLIER_RELEASE_RUN, "push", path=RELEASE_PATH)]
    judged, _ = _dropped(checks, runs)
    assert judged == ["aggregate", SELF_JOB]


def test_the_waiters_own_in_progress_row_never_blocks_it():
    # --self-run-id is what places this row, and the fixture is what makes
    # it the only thing that can: LISTING LAG. The self run has no row in
    # the listing, so the selector cannot resolve it and the path rule
    # cannot apply however well the path is known — only the exact name AND
    # run id together keep this row out. Judge it and its `in_progress`
    # status reads as pending, so the wait polls to the deadline.
    checks = [_check("aggregate", PUSH_RUN),
              _check(SELF_JOB, OWN_RUN, status="in_progress",
                     conclusion=None)]
    runs = [_run(PUSH_RUN, "push")]
    judged, _ = _dropped(checks, runs, self_path=RELEASE_PATH)
    assert judged == ["aggregate"]


def test_the_same_row_is_judged_without_the_self_run_id():
    # The isolating partner: same fixture, same known path, no run id. Only
    # the identity limb stood between this row and the judged set.
    checks = [_check("aggregate", PUSH_RUN),
              _check(SELF_JOB, OWN_RUN, status="in_progress",
                     conclusion=None)]
    runs = [_run(PUSH_RUN, "push")]
    judged, _ = _dropped(checks, runs, self_run_id=None,
                         self_path=RELEASE_PATH)
    assert judged == ["aggregate", SELF_JOB]


def test_a_dispatch_row_is_judged_until_a_push_run_proves_otherwise():
    # Nothing scheduled for this SHA yet: the module cannot tell a gate
    # dispatch from a stray one, so it judges both and lets the wait step
    # keep polling.
    checks = [_check("aggregate", PUSH_RUN), _check("refresh", DISPATCH_RUN)]
    runs = [_run(DISPATCH_RUN, "workflow_dispatch",
                 path=".github/workflows/refresh-pricing.yml",
                 name="refresh-pricing")]
    assert _judged(checks, runs) == ["aggregate", "refresh"]


def test_the_aggregate_verdict_survives_every_exclusion():
    # Issue #247's proof the gates ran is a push row, so no exclusion
    # above may be able to remove it.
    checks = [_check("aggregate", PUSH_RUN), _check(SELF_JOB, OWN_RUN),
              _check("claim", CLAIM_RUN, conclusion="failure")]
    runs = [_run(PUSH_RUN, "push"),
            _run(OWN_RUN, "push", path=RELEASE_PATH),
            _run(CLAIM_RUN, "issue_comment",
                 path=".github/workflows/claim.yml", name="claim")]
    rows, _ = rg.select(checks, runs, SHA, SELF_RUN, SELF_JOB)
    assert len(rows) == 1
    assert rows[0].split("\t")[1:] == ["success", "aggregate", "github-actions"]


# --- the projection and the two API shapes -------------------------------

def test_parse_pages_merges_the_pages_gh_printed():
    raw = (json.dumps({"total_count": 2,
                       "check_runs": [{"name": "a"}]}) + "\n"
           + json.dumps({"total_count": 2, "check_runs": [{"name": "b"}]}))
    assert [c["name"] for c in rg.parse_pages(raw, "check_runs")] == ["a", "b"]


def test_parse_pages_ignores_a_page_without_the_key():
    raw = json.dumps({"message": "Not Found"}) + "\n" + json.dumps(
        {"workflow_runs": [{"id": 1}]})
    assert rg.parse_pages(raw, "workflow_runs") == [{"id": 1}]


def test_run_id_is_read_from_the_actions_url_only():
    assert rg.run_id_of(_check("x", "12345")) == "12345"
    assert rg.run_id_of(_foreign_check("x")) is None
    assert rg.run_id_of({"html_url": ""}) is None


def test_the_row_projection_carries_status_conclusion_name_and_app():
    assert rg.row(_check("tests", "1", status="queued", conclusion=None)) \
        == "queued\t-\ttests\tgithub-actions"
    assert rg.row({"name": "n"}) == "\t-\tn\t-"


# --- the CLI the workflow step actually calls ---------------------------

def _cli(tmp_path, checks, runs, *extra):
    checks_file = tmp_path / "checks.json"
    runs_file = tmp_path / "runs.json"
    checks_file.write_text(json.dumps({"check_runs": checks}),
                           encoding="utf-8")
    runs_file.write_text(json.dumps({"workflow_runs": runs}), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--sha", SHA,
         "--self-run-id", SELF_RUN, "--self-job", SELF_JOB,
         "--checks", str(checks_file), "--runs", str(runs_file), *extra],
        capture_output=True, check=False, timeout=60, encoding="utf-8",
        errors="replace")


def test_the_cli_judges_the_gates_and_reports_what_it_dropped(tmp_path):
    checks = [_check("aggregate", PUSH_RUN), _check("claim", CLAIM_RUN),
              _check(SELF_JOB, OWN_RUN)]
    runs = [_run(PUSH_RUN, "push"),
            _run(OWN_RUN, "push", path=RELEASE_PATH),
            _run(CLAIM_RUN, "issue_comment",
                 path=".github/workflows/claim.yml", name="claim")]
    proc = _cli(tmp_path, checks, runs)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == [
        "completed\tsuccess\taggregate\tgithub-actions"]
    assert "skipping check 'claim'" in proc.stderr
    assert "issue_comment" in proc.stderr
    assert "skipping check 'release'" in proc.stderr


def _cli_stdin(payload, *flags):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--sha", SHA,
         "--self-run-id", SELF_RUN, *flags],
        input=json.dumps(payload), capture_output=True, check=False,
        timeout=60, encoding="utf-8", errors="replace")


def test_the_cli_reads_the_checks_from_stdin(tmp_path):
    # One half at a time: `sys.stdin` is consumed by the first read, so a
    # single stdin carrying both documents proves the SECOND read nothing.
    runs_file = tmp_path / "runs.json"
    runs_file.write_text(json.dumps({"workflow_runs": [_run(PUSH_RUN, "push")]}),
                         encoding="utf-8")
    proc = _cli_stdin({"check_runs": [_check("aggregate", PUSH_RUN)]},
                      "--checks", "-", "--runs", str(runs_file))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == [
        "completed\tsuccess\taggregate\tgithub-actions"]


def test_the_cli_reads_the_runs_from_stdin(tmp_path):
    checks_file = tmp_path / "checks.json"
    checks_file.write_text(
        json.dumps({"check_runs": [
            _check("aggregate", PUSH_RUN),
            _check("claim", CLAIM_RUN, conclusion="failure")]}),
        encoding="utf-8")
    proc = _cli_stdin({"workflow_runs": [
        _run(PUSH_RUN, "push"),
        _run(CLAIM_RUN, "issue_comment",
             path=".github/workflows/claim.yml", name="claim")]},
        "--checks", str(checks_file), "--runs", "-")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == [
        "completed\tsuccess\taggregate\tgithub-actions"]
    assert "skipping check 'claim'" in proc.stderr
