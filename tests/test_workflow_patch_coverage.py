"""Workflow shape: the informational patch-coverage readout (issues #129, #560).

tests.yml's `pytest` job already produces `coverage.xml`; on a pull
request it also computes the patch-coverage readout against the merge
base. Until #560 it posted the result itself, under a
`pull-requests: write` token that was live from `pip install` through a
run of the PROPOSED tree — and read-only on a fork anyway, which is why no
fork pull request ever saw the readout. The job now uploads the rendered
body and a NEW top-level workflow, coverage-comment.yml, posts it: that
workflow checks out nothing and runs no pull-request code, so it is the
only side of the boundary holding a token that can write.

The readout is INFORMATIONAL either way: no threshold, no gate, no
required check anywhere — the module's own exit contract is pinned in
tests/test_diff_coverage.py. The invariants here:

COMPUTE SIDE (tests.yml, the `pytest` job)
- the compute step exists, runs the module, and reads the merge base;
- it is pull-request-only, and the readout also lands on
  `$GITHUB_STEP_SUMMARY`;
- coverage.xml exists before the readout runs;
- the workflow carries exactly one wiring of the module.

TOKEN GRANTS (#560)
- the `pytest` job and ci-gate's `tests` leg hold `contents: read` and
  NOTHING else, and neither file grants pull-request write anywhere;
- the body crosses as an artifact named `diff-coverage-comment`, staged
  with the pull-request number it claims.

POSTING SIDE (coverage-comment.yml) — the artifact-poisoning surface
- it is triggered by `workflow_run` on the workflow that OWNS the run
  (`ci gate`, because `tests` is its reusable callee), and only acts on a
  pull-request run;
- it holds exactly `pull-requests: write` + `actions: read`, has no
  `actions/checkout`, and runs nothing the artifact contains;
- the DESTINATION is resolved from the event's own `pull_requests`, and
  the artifact's claim is COMPARED with it — a mismatch fails the step;
- the body is size-capped and refused rather than truncated, must be an
  ordinary file, and reaches GitHub as a file argument, never shell
  interpolation;
- one marker comment, posted or updated.

NOT A GATE (#560)
- it is not a ci-gate leg, publishes no check run, and holds no
  `checks:` permission;
- release_gate.py never judges a `workflow_run`-triggered run's check
  runs, which is why publishing nothing cannot become a hidden gate.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
TESTS_WF = WORKFLOWS / "tests.yml"
CI_GATE = WORKFLOWS / "ci-gate.yml"
COVERAGE_WF = WORKFLOWS / "coverage-comment.yml"
MODULE_REF = "scripts/ci/diff_coverage.py"
MARKER = "<!-- claudit-diff-coverage -->"
ARTIFACT = "diff-coverage-comment"
RELEASE_GATE = ROOT / "scripts" / "ci" / "release_gate.py"

# The steps the readout hangs off, in the `pytest` job.
COMPUTE_STEP = "Patch coverage against the merge base"
STAGE_STEP = "Stage the patch-coverage comment"
UPLOAD_STEP = "Upload the patch-coverage comment"
POST_STEP = "Post or update the pull request comment"
MARK_STEP = "Mark missing patch coverage"
RESOLVE_STEP = "Resolve the target pull request from the event"


def _load(path):
    # BaseLoader, not safe_load: YAML 1.1 parses the bare key `on` as the
    # boolean True, and these tests read the trigger map by name.
    return yaml.load(Path(path).read_text(encoding="utf-8"),
                     Loader=yaml.BaseLoader) or {}


def _pytest_job():
    return _load(TESTS_WF)["jobs"]["pytest"]


def _comment_job():
    return _load(COVERAGE_WF)["jobs"]["comment"]


def _step(job, name):
    for step in job.get("steps") or []:
        if step.get("name") == name:
            return step
    raise AssertionError(f"no step named {name!r}")


def _step_names(job):
    return [step.get("name") for step in job.get("steps") or []]


def _uses(job):
    return [step.get("uses") for step in job.get("steps") or []]


def _runs(job):
    return [step.get("run") or "" for step in job.get("steps") or []]


class TestComputeSide:
    def test_compute_step_exists_and_runs_the_module(self):
        step = _step(_pytest_job(), COMPUTE_STEP)
        assert MODULE_REF in (step.get("run") or "")
        assert "--coverage coverage.xml" in (step.get("run") or "")

    def test_compute_step_reads_the_merge_base(self):
        run = _step(_pytest_job(), COMPUTE_STEP).get("run") or ""
        assert "git merge-base" in run
        assert "git diff" in run

    def test_compute_and_publish_steps_are_pull_request_only(self):
        job = _pytest_job()
        for name in (COMPUTE_STEP, STAGE_STEP, UPLOAD_STEP):
            cond = _step(job, name).get("if") or ""
            assert "github.event_name == 'pull_request'" in cond, name
            assert "steps.pytest.conclusion != 'skipped'" in cond, name

    def test_the_readout_also_lands_on_the_step_summary(self):
        run = _step(_pytest_job(), COMPUTE_STEP).get("run") or ""
        assert "GITHUB_STEP_SUMMARY" in run
        assert "diff-coverage.md" in run

    def test_coverage_xml_exists_before_the_readout_runs(self):
        names = _step_names(_pytest_job())
        assert names.index("Coverage summary") < names.index(COMPUTE_STEP)

    def test_the_body_crosses_as_the_named_artifact(self):
        step = _step(_pytest_job(), UPLOAD_STEP)
        assert (step.get("uses") or "").startswith("actions/upload-artifact")
        assert (step.get("with") or {}).get("name") == ARTIFACT

    def test_the_artifact_carries_the_body_and_the_claimed_pr_number(self):
        run = _step(_pytest_job(), STAGE_STEP).get("run") or ""
        assert "body.md" in run
        assert "pr-number.txt" in run
        # Written as data, never spliced into a command that later runs.
        assert 'cp "$RUNNER_TEMP/diff-coverage.md"' in run

    def test_the_readout_is_wired_exactly_once(self):
        """One wiring site: no other workflow or job runs the module."""
        wired = []
        for path in sorted(WORKFLOWS.glob("*.yml")):
            doc = _load(path)
            for job_id, job in (doc.get("jobs") or {}).items():
                for step in (job or {}).get("steps") or []:
                    if MODULE_REF in (step.get("run") or ""):
                        wired.append(f"{path.name}:{job_id}")
        assert wired == ["tests.yml:pytest"]


class TestTokenGrants:
    """#560: the job that RUNS the proposed tree holds no write token."""

    def test_the_callee_job_is_contents_read_only(self):
        perms = _pytest_job().get("permissions") or {}
        assert perms == {"contents": "read"}

    def test_the_caller_leg_is_contents_read_only(self):
        perms = _load(CI_GATE)["jobs"]["tests"].get("permissions") or {}
        assert perms == {"contents": "read"}

    def test_no_pull_request_write_grant_anywhere_in_the_gate_tree(self):
        offenders = []
        for path in (TESTS_WF, CI_GATE):
            doc = _load(path)
            for job_id, job in (doc.get("jobs") or {}).items():
                perms = (job or {}).get("permissions") or {}
                for scope, level in perms.items():
                    if level == "write" and scope.startswith("pull-"):
                        offenders.append(f"{path.name}:{job_id}:{scope}")
                for scope, level in (doc.get("permissions") or {}).items():
                    if level == "write" and scope.startswith("pull-"):
                        offenders.append(f"{path.name}:{scope}")
        assert not offenders, offenders


class TestPostingSide:
    def test_it_is_triggered_by_the_run_that_owns_the_tests_leg(self):
        # `tests` is a REUSABLE callee of ci-gate.yml, so the run that
        # completes — and the run whose artifacts carry the body — is
        # `ci gate`. Naming `tests` here would never fire.
        on = _load(COVERAGE_WF).get("on") or {}
        assert sorted(on) == ["workflow_run"], on
        run = on["workflow_run"]
        assert run.get("workflows") == ["ci gate"], run
        assert run.get("types") == ["completed"], run

    def test_it_only_acts_on_a_pull_request_run(self):
        job = _comment_job()
        assert "github.event.workflow_run.event == 'pull_request'" \
            in (job.get("if") or "")

    def test_its_permissions_are_exactly_what_posting_needs(self):
        assert _load(COVERAGE_WF).get("permissions") == {
            "pull-requests": "write", "actions": "read"}

    def test_it_checks_out_nothing(self):
        # No checkout is the structural half of the safety argument: there
        # is no working tree for pull-request content to enter.
        assert not [u for u in _uses(_comment_job())
                    if (u or "").startswith("actions/checkout")]

    def test_it_downloads_the_artifact_of_the_triggering_run_only(self):
        downloads = [s for s in _comment_job()["steps"]
                     if (s.get("uses") or "").startswith(
                         "actions/download-artifact")]
        assert len(downloads) == 1, downloads
        with_ = downloads[0].get("with") or {}
        assert with_.get("name") == ARTIFACT
        # Bound to the EVENT's run id, never to something the artifact says.
        assert "github.event.workflow_run.id" in str(with_.get("run-id"))

    def test_the_destination_is_resolved_from_the_event(self):
        env = _step(_comment_job(), RESOLVE_STEP).get("env") or {}
        assert "github.event.workflow_run.pull_requests" in str(
            env.get("EVENT_PRS"))
        # Resolved BEFORE the download: the destination is settled while
        # nothing the pull request produced is on disk.
        names = _step_names(_comment_job())
        assert names.index(RESOLVE_STEP) < names.index(POST_STEP)

    def test_the_artifact_claim_is_compared_not_obeyed(self):
        run = _step(_comment_job(), POST_STEP).get("run") or ""
        assert 'claimed="$(cat pr-number.txt)"' in run
        assert '[ "$claimed" != "$PR_NUMBER" ]' in run
        # Digits only: the claim is never used as anything but a number.
        assert "*[!0-9]*)" in run
        # And the mismatch is loud — refusing, not quietly correcting.
        assert "refusing to post" in run

    def test_the_body_must_be_an_ordinary_file_and_is_size_capped(self):
        run = _step(_comment_job(), POST_STEP).get("run") or ""
        assert '[ ! -f "$f" ] || [ -L "$f" ]' in run
        # Refused, never trimmed: a truncated coverage report is a wrong
        # one, and the comments API caps a body at 65536 characters.
        assert "wc -c < body.md" in run
        assert 'if [ "$size" -gt 60000 ]; then' in run

    def test_the_body_reaches_github_as_data_never_as_shell(self):
        job = _comment_job()
        step = _step(job, POST_STEP)
        run = step.get("run") or ""
        assert "-F body=@" in run
        # The body is concatenated into a file and uploaded as a file.
        assert run.count("cat body.md") == 1
        # Every address the API is given comes from the resolved EVENT,
        # never from a value the artifact supplied.
        env = step.get("env") or {}
        assert env.get("PR_NUMBER") == "${{ steps.pr.outputs.number }}"
        assert env.get("GH_TOKEN") == "${{ github.token }}"
        # Nothing the artifact holds is ever re-interpreted by the shell.
        # Comments are excluded: prose quotes backticks.
        for body in _runs(job):
            code = "\n".join(line for line in body.splitlines()
                             if not line.lstrip().startswith("#"))
            assert "eval" not in code
            assert "`" not in code
        for line in run.splitlines():
            if "$claimed" in line:
                assert "gh api" not in line, line

    def test_it_updates_one_marker_comment(self):
        run = _step(_comment_job(), POST_STEP).get("run") or ""
        assert MARKER in run
        # find-or-create-then-update: a PATCH of the existing comment's id,
        # never a second POST when one already exists.
        assert "-X POST" in run and "-X PATCH" in run
        assert ".[0].id // empty" in run

    def test_a_run_without_the_artifact_refreshes_the_marker(self):
        # Otherwise a red or narrowed run leaves the previous commit's
        # percentage looking current.
        step = _step(_comment_job(), MARK_STEP)
        cond = step.get("if") or ""
        assert "steps.artifact.outputs.present != 'true'" in cond
        run = step.get("run") or ""
        assert MARKER in run
        assert "-X PATCH" in run


class TestNotAGate:
    """The comment is information. Nothing waits on this workflow."""

    def test_it_is_not_a_ci_gate_leg(self):
        for path in sorted(WORKFLOWS.glob("*.yml")):
            for job in (_load(path).get("jobs") or {}).values():
                uses = (job or {}).get("uses") or ""
                assert "coverage-comment.yml" not in uses, path.name

    def test_it_publishes_no_check_run(self):
        # No `checks:` permission is the structural proof; the absence of
        # a check-runs write is the consequence.
        perms = _load(COVERAGE_WF).get("permissions") or {}
        assert "checks" not in perms
        for run in _runs(_comment_job()):
            assert "/check-runs" not in run

    def test_release_gate_never_judges_a_workflow_run_triggered_run(self):
        spec = importlib.util.spec_from_file_location(
            "release_gate_560", RELEASE_GATE)
        assert spec and spec.loader
        rg = importlib.util.module_from_spec(spec)
        sys.modules["release_gate_560"] = rg
        spec.loader.exec_module(rg)

        assert "workflow_run" not in rg.GATE_EVENTS
        sha = "9d1c0b2a3f4e5d6c7b8a9f0e1d2c3b4a5f6e7d8c"
        run_id = "77000000001"
        check = {"name": "comment", "status": "completed",
                 "conclusion": "failure", "app": {"slug": "github-actions"},
                 "html_url": f"https://github.com/o/r/actions/runs/{run_id}"
                             "/job/1"}

        def judged(event, path):
            runs = [{"id": int(run_id), "event": event, "head_sha": sha,
                     "path": path, "name": path.rsplit("/", 1)[-1][:-4]}]
            rows, _notes = rg.select([check], runs, sha, 1, "release", None)
            return [line.split("\t")[2] for line in rows]

        assert judged("workflow_run",
                      ".github/workflows/coverage-comment.yml") == []
        # …and the same check from a real gate run IS judged, so the skip
        # is the event and not a filter that drops everything.
        assert judged("push", ".github/workflows/ci-gate.yml") == ["comment"]
