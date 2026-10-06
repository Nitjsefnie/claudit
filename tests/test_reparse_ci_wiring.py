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
    assert step.get("uses") == "$/.github/actions/reparse-bench", (
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


def test_only_a_master_push_may_tighten_the_reparse_bytecodes(workflow):
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


def _action_steps() -> list[dict]:
    return yaml.safe_load(ACTION.read_text(encoding="utf-8"))["runs"]["steps"]


def test_the_action_measures_once_and_gates_that_same_file():
    """Measure and gate must name ONE file.

    Two names is the skew the corpus records: a measurement written to one
    path and checked against another gates whatever happened to be at the
    second path, which on a fresh runner is nothing at all.

    The shape is measure -> upload -> gate: two run steps invoking the
    bench exactly twice, with the upload between them (issue #710).
    """
    runs = [step for step in _action_steps() if "run" in step]
    assert len(runs) == 2, (
        "the action is two run steps, measure and gate; re-check the shape")
    measure, gate = runs
    for body in (measure["run"], gate["run"]):
        assert "set -euo pipefail" in body, (
            "a step's shell does not stop on the first failure, so a failed "
            "measure can be followed by a gate that passes on a stale file")
    assert measure["run"].count("scripts/ci/reparse_bench.py") == 1
    assert gate["run"].count("scripts/ci/reparse_bench.py") == 1, (
        "the action must invoke the bench exactly twice: once to measure and "
        "write, once to gate what it wrote")
    assert "--write" in measure["run"] and "--report" in measure["run"], (
        "the measuring invocation no longer both writes and reports the "
        "measurement")
    assert "--check" in gate["run"], (
        "the gating invocation no longer checks the measurement")
    # All three references must read the SAME variable, so a hard-coded
    # path in any of them is the skew this test exists to catch.
    assert measure["run"].count('"$MEASUREMENT"') == 2, (
        "the measuring invocation does not name the measurement path twice, "
        "via the shared variable")
    assert gate["run"].count('"$MEASUREMENT"') == 1, (
        "the gating invocation does not name the measurement path via the "
        "shared variable")


def test_every_run_step_binds_MEASUREMENT_to_the_measurement_input():
    """That the steps share the NAME is pinned above; what it is BOUND to
    is not (issue #712): a step can keep reading `"$MEASUREMENT"` while an
    edit points the variable at a file the measuring step never wrote --
    the gate then passes on whatever is at the second path, which on a
    fresh runner is nothing at all.
    """
    misbound = [
        step.get("name", "<unnamed>")
        for step in _action_steps()
        if "run" in step
        and step.get("env", {}).get("MEASUREMENT") != "${{ inputs.measurement }}"
    ]
    assert not misbound, (
        "these run steps bind MEASUREMENT to anything other than the "
        f"action's measurement input: {misbound}")


def test_the_measurement_default_is_the_file_the_ratchet_reads(workflow):
    """The caller passes no `measurement` input, so the action input's
    default is the operative path -- and tests.yml's ratchet step reads
    the measurement back through a plain literal (`--measured-file`,
    issue #712). Two independently editable literals: when they stop
    agreeing, the ratchet tightens on a file the bench never wrote.
    """
    gate = _step(workflow, GATE_STEP)
    assert "measurement" not in (gate.get("with") or {}), (
        "the caller overrides the measurement input, so the default is no "
        "longer the operative path this test pins")
    default = yaml.safe_load(ACTION.read_text(encoding="utf-8"))[
        "inputs"]["measurement"]["default"]
    found = re.search(
        r"reparse_ratchet\.py\s+--measured-file\s+(\S+)",
        _step(workflow, RATCHET_STEP).get("run", ""))
    assert found, "the ratchet step no longer names its measurement file"
    assert default == found.group(1), (
        f"the measurement input's default {default!r} is not the path the "
        f"ratchet step reads back ({found.group(1)!r})")


def test_the_action_uploads_the_measurement_as_an_artifact():
    """The reparse seed's runner-measurement source is this artifact
    (issue #710): without it, a seed can only be transcribed by hand or
    measured off-runner, both forbidden by the re-seed rules.

    The whole shape is pinned by position, so removing the upload, moving
    it after the gate, or slipping in a step fails here.
    """
    steps = _action_steps()
    assert [step.get("uses", "") for step in steps] == [
        "",
        "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
        "",
    ], ("the action's shape is measure -> upload -> gate; the upload step "
        "with the repo's hash-pinned action is missing or displaced")
    upload = steps[1]
    assert upload["if"] == "${{ !cancelled() }}", (
        "the upload is not always-on: a breached gate is exactly when the "
        "runner number is needed")
    with_ = upload["with"]
    assert with_["name"] == "reparse-measurement"
    assert with_["path"] == "${{ inputs.measurement }}"
    assert with_["if-no-files-found"] == "error"
    assert with_["retention-days"] == 1, (
        "short retention, matching the suite-measurement artifact")


def test_the_action_fails_the_job_when_a_phase_is_over_budget():
    """The gate's whole job is a nonzero exit, so nothing may absorb it."""
    gate = [step for step in _action_steps() if "run" in step][-1]
    body = gate["run"]
    check = body[body.index("--check"):]
    assert "||" not in check and "|| true" not in check, (
        "the gating invocation absorbs its own failure")
    assert check.rstrip().endswith('"$MEASUREMENT"'), (
        "the gating invocation does not end on the measurement it checked")
