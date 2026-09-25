"""Workflow shape: the informational patch-coverage readout (issue #129).

tests.yml's `pytest` job already produces `coverage.xml`; on a pull
request it now also computes the patch-coverage readout against the
merge base and posts it as ONE comment that is UPDATED on later pushes
rather than duplicated. The readout is INFORMATIONAL: no threshold, no
gate, no required check anywhere — the module's own exit contract is
pinned in tests/test_diff_coverage.py. The invariants here:

- the compute and comment steps exist in the `pytest` job, run only on
  pull requests, and run the module;
- the readout lands on both surfaces — `$GITHUB_STEP_SUMMARY` and the
  pull-request comment — from the same rendered body;
- the comment step carries `continue-on-error` (a failed comment must
  never redden the run) and the fixed marker, and updates rather than
  duplicates the marked comment;
- the token grants sit on BOTH sides of the reusable-call boundary;
- coverage.xml exists before the readout steps run;
- the workflow carries exactly one wiring of the readout.
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
TESTS_WF = WORKFLOWS / "tests.yml"
CI_GATE = WORKFLOWS / "ci-gate.yml"
MODULE_REF = "scripts/ci/diff_coverage.py"
MARKER = "<!-- claudit-diff-coverage -->"

# The steps the readout hangs off, in the `pytest` job.
COMPUTE_STEP = "Patch coverage against the merge base"
COMMENT_STEP = "Post the patch-coverage comment"


def _load(path):
    # BaseLoader, not safe_load: YAML 1.1 parses the bare key `on` as the
    # boolean True, and these tests read the trigger map by name.
    return yaml.load(path.read_text(encoding="utf-8"),
                     Loader=yaml.BaseLoader) or {}


def _pytest_job():
    return _load(TESTS_WF)["jobs"]["pytest"]


def _step(job, name):
    for step in job.get("steps") or []:
        if step.get("name") == name:
            return step
    raise AssertionError(f"no step named {name!r} in the pytest job")


def _step_names(job):
    return [step.get("name") for step in job.get("steps") or []]


class TestPatchCoverageWiring:
    def test_compute_step_exists_and_runs_the_module(self):
        job = _pytest_job()
        step = _step(job, COMPUTE_STEP)
        assert MODULE_REF in (step.get("run") or "")
        assert "--coverage coverage.xml" in (step.get("run") or "")

    def test_compute_step_reads_the_merge_base(self):
        run = _step(_pytest_job(), COMPUTE_STEP).get("run") or ""
        assert "git merge-base" in run
        assert "git diff" in run

    def test_both_steps_are_pull_request_only(self):
        job = _pytest_job()
        for name in (COMPUTE_STEP, COMMENT_STEP):
            cond = _step(job, name).get("if") or ""
            assert "github.event_name == 'pull_request'" in cond, name
            assert "steps.pytest.conclusion != 'skipped'" in cond, name

    def test_both_surfaces_receive_the_readout(self):
        run = _step(_pytest_job(), COMPUTE_STEP).get("run") or ""
        assert "GITHUB_STEP_SUMMARY" in run
        assert "diff-coverage.md" in run

    def test_comment_step_carries_continue_on_error(self):
        step = _step(_pytest_job(), COMMENT_STEP)
        assert step.get("continue-on-error") is True

    def test_comment_step_updates_one_marker_comment(self):
        run = _step(_pytest_job(), COMMENT_STEP).get("run") or ""
        assert MARKER in run
        # find-or-create-then-update: a PATCH of the existing comment's id,
        # never a second POST when one already exists.
        assert "-X PATCH" in run
        assert ".[0].id // empty" in run

    def test_comment_body_is_passed_as_data_not_shell(self):
        run = _step(_pytest_job(), COMMENT_STEP).get("run") or ""
        assert "-F body=@" in run

    def test_token_grant_on_the_callee_job(self):
        perms = _pytest_job().get("permissions") or {}
        assert perms.get("pull-requests") == "write"

    def test_token_grant_on_the_caller_job(self):
        tests_leg = _load(CI_GATE)["jobs"]["tests"]
        perms = tests_leg.get("permissions") or {}
        assert perms.get("pull-requests") == "write"

    def test_coverage_xml_exists_before_the_readout_runs(self):
        names = _step_names(_pytest_job())
        assert names.index("Coverage summary") < names.index(COMPUTE_STEP)
        assert names.index("Upload coverage.xml") < names.index(COMMENT_STEP)

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
