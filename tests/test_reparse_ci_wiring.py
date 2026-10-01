"""The reparse bench's CI wiring, pinned (issue #436).

The bench is a gate, and a gate nothing pins is a gate a later PR can
delete without a red build. Every assertion here is about the SHAPE --
which event runs it, what it measures, what it gates, and who may write the
recorded numbers -- because those are the four things a refactor moves and
none of them is the bench's arithmetic, which `test_reparse_bench.py` owns.

The review corpus is blunt about this line: `ci/2026-08-28-derive-the-gate-
list-from-the-workflow.md` records a planted deletion of a gate step leaving
the whole suite green, seven recurrences deep, and its companion entries pin
the operator's semantics rather than the presence of the text. So each case
below asserts a decoded value, and the load-bearing ones are proven by
mutating the workflow in a disposable copy.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
ACTION = ROOT / ".github" / "actions" / "reparse-bench" / "action.yml"

GATE_STEP = "Reparse CPU gate"
RATCHET_STEP = "Ratchet the thresholds"


@pytest.fixture(name="workflow")
def _workflow_fixture() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


JOB = "pytest"   # the leg the gate and the ratchet both live in


def _steps(workflow: dict, job: str = JOB) -> list[dict]:
    return workflow["jobs"][job]["steps"]


def _step(workflow: dict, name: str, job: str = JOB) -> dict:
    for step in _steps(workflow, job):
        if step.get("name") == name:
            return step
    raise AssertionError(f"no step named {name!r} in job {job!r}")


def test_the_gate_step_exists_and_calls_the_bundled_action(workflow):
    """A gate that is a `run:` line can be retyped into a no-op; a
    composite action's shape is checked in `action.yml` instead."""
    step = _step(workflow, GATE_STEP)
    assert step.get("uses") == "./.github/actions/reparse-bench", (
        "the gate step no longer calls the bundled composite action")
    assert ACTION.is_file(), (
        f"{ACTION} is gone, so the gate step cannot run at all")


def test_the_gate_runs_on_every_event_not_only_on_master(workflow):
    """A gate that only runs on master gates nothing on a pull request."""
    step = _step(workflow, GATE_STEP)
    condition = step.get("if", "")
    assert "github.event_name" not in condition, (
        "the gate is conditioned on the event, so a pull request skips it")
    assert "github.ref" not in condition, (
        "the gate is conditioned on the ref, so a branch push skips it")
    # It is allowed to defer to a skipped pytest leg (a docs-only change
    # classifies the expensive legs out), and to a cancelled run. Anything
    # else in the condition narrows when it runs.
    assert set(re.findall(r"steps\.\w+\.\w+", condition)) <= {
        "steps.pytest.conclusion"}


def test_the_gate_is_not_continue_on_error(workflow):
    """`continue-on-error: true` is a gate that reports success while failing."""
    step = _step(workflow, GATE_STEP)
    assert not step.get("continue-on-error"), (
        "the gate step swallows its own failure")


def test_the_gate_step_is_identified_so_the_ratchet_can_require_it(workflow):
    """The ratchet conditions on `steps.reparse.outcome`, so the id is load-bearing.

    Delete or rename the `id:` and the ratchet's condition silently
    evaluates false -- the ratchet stops running, and nothing says so.
    """
    assert _step(workflow, GATE_STEP).get("id") == "reparse"


def test_only_a_master_push_may_tighten_the_reparse_shares(workflow):
    """A pull request runs untrusted code and must not write the recorded data.

    All four conditions are checked by name, because dropping ANY of them
    opens the same hole differently: no `steps.reparse.outcome` tightens on a
    failed measurement, no `steps.pytest.outcome` tightens on a failed suite,
    and no master-push condition lets a fork's push write the document.
    """
    condition = _step(workflow, RATCHET_STEP).get("if", "")
    for required in ("steps.pytest.outcome == 'success'",
                     "steps.jscov.outcome == 'success'",
                     "steps.reparse.outcome == 'success'",
                     "github.event_name == 'push'",
                     "github.ref == 'refs/heads/master'"):
        assert required in condition, f"the ratchet no longer requires {required!r}"


def test_the_ratchet_actually_runs_the_reparse_ratchet(workflow):
    """The condition can be perfect while the command is gone."""
    body = _step(workflow, RATCHET_STEP).get("run", "")
    assert "scripts/ci/reparse_ratchet.py" in body, (
        "the ratchet step no longer runs the reparse ratchet")
    assert "--measured-file" in body, (
        "the reparse ratchet is called with no measurement file, so it has "
        "nothing to read and cannot tighten anything")


def test_the_action_measures_once_and_gates_that_same_file():
    """Measure and gate must name ONE file.

    Two names is the skew the corpus records: a measurement written to one
    path and checked against another gates whatever happened to be at the
    second path, which on a fresh runner is nothing at all.
    """
    steps = yaml.safe_load(ACTION.read_text(encoding="utf-8"))["runs"]["steps"]
    assert len(steps) == 1, "the action grew a second step; re-check the shape"
    body = steps[0]["run"]
    assert body.count("scripts/ci/reparse_bench.py") == 2, (
        "the action must invoke the bench exactly twice: once to measure and "
        "write, once to gate what it wrote")
    assert "--write" in body and "--check" in body, (
        "the action no longer both writes and checks a measurement")
    # Both invocations must read the same path, taken from the input rather
    # than written out twice.
    # Three: `--write` and `--report` on the measuring invocation, `--check`
    # on the gating one. All three must read the SAME variable, so a
    # hard-coded path in any of them is the skew this test exists to catch.
    assert body.count('"$MEASUREMENT"') == 3, (
        "the invocations do not all name the measurement path they share")
    assert "set -euo pipefail" in body, (
        "the action's shell does not stop on the first failure, so a failed "
        "measure can be followed by a gate that passes on a stale file")


def test_the_action_fails_the_job_when_a_phase_is_over_budget():
    """The gate's whole job is a nonzero exit, so nothing may absorb it."""
    steps = yaml.safe_load(ACTION.read_text(encoding="utf-8"))["runs"]["steps"]
    body = steps[0]["run"]
    check = body[body.index("--check"):]
    assert "||" not in check and "|| true" not in check, (
        "the gating invocation absorbs its own failure")
    assert check.rstrip().endswith('"$MEASUREMENT"'), (
        "the gating invocation does not end on the measurement it checked")
