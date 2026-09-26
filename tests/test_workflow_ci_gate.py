"""Workflow shape: one aggregate gate owns the push/PR trigger surface.

ci-gate.yml folds every gate workflow's result into one verdict
(`ci gate / aggregate`, the name a future ruleset requires), and the
docs-only classifier narrows the expensive legs while every required
check still reports. The invariants pinned here:

- every reusable call points at an existing workflow that declares
  `workflow_call`;
- the aggregate job runs with `if: always()` and needs every leg, so it
  reports even when legs fail, skip or never start;
- trigger ownership really moved: the ten callees no longer carry
  `push`/`pull_request`, and ci-gate's push trigger keeps the ratchet
  bot's paths-ignore so its commit never starts the suite;
- the aggregate's expected-leg list and the workflow's needs list cannot
  drift apart;
- no `cache:` on any setup-python step (mirrored here so this file's
  contract is self-contained; `tests/test_workflow_pip_cache.py` owns
  the pip-cache shape repo-wide);
- every step-running job in every workflow declares `timeout-minutes`
  (issue #98: GitHub's default is 360, so an undeclared bound lets a
  hung step occupy a runner for six hours).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
CI_GATE = WORKFLOWS / "ci-gate.yml"
GATE_FRESHNESS = WORKFLOWS / "gate-freshness.yml"

# The ten gate workflows ci-gate calls, plus the classifier job.
LEG_WORKFLOWS = (
    "tests.yml", "test-data.yml", "lint.yml", "types.yml", "eslint.yml",
    "smoke.yml", "audit.yml", "actionlint.yml", "speed.yml", "codeql.yml",
)
LEG_IDS = tuple(Path(name).stem for name in LEG_WORKFLOWS)


def _load(path):
    # BaseLoader, not safe_load: YAML 1.1 parses the bare key `on` as the
    # boolean True, and these tests read the trigger map by name.
    return yaml.load(path.read_text(encoding="utf-8"),
                     Loader=yaml.BaseLoader) or {}


def _ci_gate():
    return _load(CI_GATE)


def test_every_reusable_call_points_at_an_existing_callable_workflow():
    doc = _ci_gate()
    for job_id, job in doc["jobs"].items():
        uses = (job or {}).get("uses") or ""
        if not uses.startswith("./.github/workflows/"):
            continue
        name = uses.removeprefix("./.github/workflows/")
        target = WORKFLOWS / name
        assert target.exists(), (job_id, uses)
        assert "workflow_call" in (_load(target).get("on") or {}), (
            job_id, uses)


def test_aggregate_always_runs_and_needs_every_leg():
    doc = _ci_gate()
    aggregate = doc["jobs"]["aggregate"]
    assert aggregate.get("if") == "${{ always() }}"
    needs = aggregate["needs"]
    for leg in ("classify", *LEG_IDS):
        assert leg in needs, leg


def test_aggregate_legs_match_the_modules_expected_legs():
    # Lockstep pin: the workflow's needs list and the aggregate module's
    # expected-leg tuple fail together, so adding or removing a leg
    # without the other half goes red here.
    spec = importlib.util.spec_from_file_location(
        "aggregate_gate_shape",
        ROOT / "scripts" / "ci" / "aggregate_gate.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["aggregate_gate_shape"] = module
    spec.loader.exec_module(module)
    assert module.EXPECTED_LEGS == ("classify", *LEG_IDS)


def test_ci_gate_jobs_are_exactly_the_non_leg_jobs_plus_the_legs():
    # Lockstep pin for the verified-base walk (issue #208): the
    # classifier's NON_LEG_JOBS and this file's LEG_IDS must together
    # name every ci-gate job, so a leg added to the workflow moves all
    # three lists in one commit or this test goes red — a job the walk
    # mistakes for the classifier or the aggregate would otherwise read
    # as "no leg ran".
    spec = importlib.util.spec_from_file_location(
        "classify_changes_shape",
        ROOT / "scripts" / "ci" / "classify_changes.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["classify_changes_shape"] = module
    spec.loader.exec_module(module)
    assert (set(_ci_gate()["jobs"])
            == set(module.NON_LEG_JOBS) | set(LEG_IDS))


def test_every_leg_is_conditioned_on_the_docs_only_output():
    doc = _ci_gate()
    for leg in LEG_IDS:
        gate = doc["jobs"][leg].get("if") or ""
        assert "needs.classify.outputs.docs_only != 'true'" in gate, leg


def test_classify_job_outputs_docs_only():
    outputs = _ci_gate()["jobs"]["classify"].get("outputs") or {}
    assert "docs_only" in outputs


def test_classify_job_reads_actions_for_the_verified_base_walk():
    # Issue #208: on a push the classifier walks the ci-gate workflow
    # runs and run-jobs endpoints for the newest master commit whose
    # legs executed. Without `actions: read` every such read 403s and
    # every push over-runs to full legs, silently disabling docs-only
    # narrowing on master.
    permissions = _ci_gate()["jobs"]["classify"].get("permissions") or {}
    assert permissions.get("actions") == "read"


def test_ci_gate_push_trigger_keeps_the_ratchet_paths_ignore():
    # The ratchet bot's master commit (only .github/ci-thresholds.json)
    # must not start the suite — the guarantee the gate workflows used to
    # carry individually, hosted by ci-gate now.
    push = (_ci_gate().get("on") or {}).get("push") or {}
    ignored = push.get("paths-ignore") or []
    assert ".github/ci-thresholds.json" in ignored, ignored


def test_ci_gate_triggers():
    on = _ci_gate().get("on") or {}
    assert "pull_request" in on
    assert "push" in on
    assert "workflow_dispatch" in on
    assert "workflow_call" not in on  # ci-gate is a root, not a callee


def test_gate_legs_own_no_push_or_pr_trigger_any_more():
    for name in LEG_WORKFLOWS:
        on = _load(WORKFLOWS / name).get("on") or {}
        assert "push" not in on, (name, "push")
        assert "pull_request" not in on, (name, "pull_request")
        assert "workflow_call" in on, (name, "workflow_call")
        assert "workflow_dispatch" in on, (name, "workflow_dispatch")


def test_audit_and_codeql_keep_their_crons():
    assert "schedule" in (_load(WORKFLOWS / "audit.yml").get("on") or {})
    assert "schedule" in (_load(WORKFLOWS / "codeql.yml").get("on") or {})


def test_untouched_workflows_keep_their_triggers():
    # Out of scope for this change; their trigger shape is pinned here so
    # a later edit cannot silently move it.
    on = _load(WORKFLOWS / "secrets.yml").get("on") or {}
    assert "push" in on and "pull_request" in on and "schedule" in on
    on = _load(WORKFLOWS / "release.yml").get("on") or {}
    assert "push" in on
    assert "schedule" not in (_load(WORKFLOWS / "release.yml").get("on") or {})


def test_no_setup_python_step_owns_the_cache_anywhere():
    for path in sorted(WORKFLOWS.glob("*.yml")):
        doc = _load(path)
        for job_id, job in (doc.get("jobs") or {}).items():
            for step in (job or {}).get("steps") or []:
                uses = (step.get("uses") or "").split("@", 1)[0]
                if uses != "actions/setup-python":
                    continue
                assert "cache" not in (step.get("with") or {}), (
                    path.name, job_id)


def test_every_step_running_job_declares_timeout_minutes():
    # Issue #98. GitHub's default is 360 minutes per job, so an
    # undeclared bound lets a hung step occupy a runner for six hours.
    # A reusable-call job cannot carry the key at all — the schema allows
    # only name/uses/with/secrets/needs/if/permissions there — so the
    # bound belongs to the callee's own jobs, which every workflow here
    # declares.
    offenders = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        doc = _load(path)
        for job_id, job in (doc.get("jobs") or {}).items():
            job = job or {}
            if "uses" in job:
                continue
            if not job.get("timeout-minutes"):
                offenders.append(f"{path.name}:{job_id}")
    assert not offenders, offenders


def test_gate_freshness_is_dispatch_only_and_runs_the_script():
    doc = _load(GATE_FRESHNESS)
    on = doc.get("on") or {}
    assert list(on) == ["workflow_dispatch"], on
    steps = [step for job in doc["jobs"].values()
             for step in (job.get("steps") or [])]
    runs = [step.get("run") or "" for step in steps]
    assert any("scripts/ci/gate_freshness.py" in run for run in runs), runs


def test_ci_gate_aggregate_step_runs_the_module():
    doc = _ci_gate()
    steps = doc["jobs"]["aggregate"].get("steps") or []
    runs = [step.get("run") or "" for step in steps]
    assert any("scripts/ci/aggregate_gate.py" in run for run in runs), runs


def test_classify_step_runs_the_module():
    doc = _ci_gate()
    steps = doc["jobs"]["classify"].get("steps") or []
    runs = [step.get("run") or "" for step in steps]
    assert any("scripts/ci/classify_changes.py" in run for run in runs), runs
