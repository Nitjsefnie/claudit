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
  hung step occupy a runner for six hours);
- the deploy key is loaded only by the two push jobs that take it from
  the master-push environment — refresh-pricing.yml's and the top-level
  ratchet-push.yml's — never by a forwarded secret, and tests.yml
  pushes nothing (issue #479: the secret resolved empty inside the
  workflow_call callee, and a GITHUB_TOKEN push is rejected by the
  ruleset's required aggregate check, so the ratchet push moved out of
  the callee into ratchet-push.yml, keyed on the ci-gate run's
  completion — the run that actually carries the ratchet artifacts).
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
TESTS_WORKFLOW = WORKFLOWS / "tests.yml"
REFRESH_WORKFLOW = WORKFLOWS / "refresh-pricing.yml"
RATCHET_PUSH_WORKFLOW = WORKFLOWS / "ratchet-push.yml"

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


def _aggregate_module():
    spec = importlib.util.spec_from_file_location(
        "aggregate_gate_shape",
        ROOT / "scripts" / "ci" / "aggregate_gate.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["aggregate_gate_shape"] = module
    spec.loader.exec_module(module)
    return module


def _tests_workflow():
    return _load(TESTS_WORKFLOW)


def _ratchet_push_job():
    jobs = _load(RATCHET_PUSH_WORKFLOW)["jobs"]
    assert set(jobs) == {"push"}
    return jobs["push"]


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


def test_legs_are_conditioned_on_the_narrowing_outputs():
    # Issue #455: a bot-data-only change (src/pricing.json alone) runs
    # only the cheap legs; every other leg needs BOTH narrowing outputs
    # false. The cheap set is pinned in lockstep with the aggregate
    # module's CHEAP_LEGS, so a leg cannot change class in one place.
    cheap = _aggregate_module().CHEAP_LEGS
    assert set(cheap) == {"lint", "smoke"}
    doc = _ci_gate()
    for leg in LEG_IDS:
        gate = doc["jobs"][leg].get("if") or ""
        if leg in cheap:
            assert gate == (
                "needs.classify.outputs.docs_only != 'true'"), leg
        else:
            assert gate == (
                "needs.classify.outputs.docs_only != 'true' "
                "&& needs.classify.outputs.data_only != 'true'"), leg


def test_aggregate_module_names_the_same_cheap_legs_the_workflow_runs():
    # The fold accepts a skipped leg under data_only only OUTSIDE the
    # cheap set, and the workflow runs the cheap set under data_only —
    # the same tuple, so the two files cannot disagree silently.
    module = _aggregate_module()
    assert module.CHEAP_LEGS == frozenset({"lint", "smoke"})
    doc = _ci_gate()
    for leg in LEG_IDS:
        gate = doc["jobs"][leg].get("if") or ""
        runs_under_data_only = "data_only" not in gate
        assert runs_under_data_only == (leg in module.CHEAP_LEGS), leg


def test_master_push_runs_get_a_per_sha_concurrency_group():
    # Issue #358: on a per-ref group with cancel-in-progress, a bot push
    # (the hourly pricing refresh, a dependabot merge) landing while a
    # master run was in flight cancelled it whole — but the aggregate
    # carries `if: always()`, so the cancelled run's aggregate still ran,
    # folded the cancelled legs, and recorded a red `aggregate` on the
    # superseded commit, which release.yml's waiter then refuses (a
    # VERSION commit raced by a bot push could not release). Scoping the
    # push side of the group to the commit SHA gives every master push
    # its own run the gate's group never cancels, and release.yml waits
    # on a run that completes. The two long legs are per-SHA on a push
    # too (issues #366, #409), pinned by tests/test_workflow_concurrency.py.
    # One residual remains one level down: the remaining per-ref leg
    # groups hold at most one pending run, so a third concurrent master
    # push can still cancel a merely-queued short leg (see the
    # workflow's SUPERSESSION block). Pull requests keep the
    # per-number group (a head push cancels the stale run), and a
    # deliberate cancel still reads never-green (the fold treats a
    # cancelled leg as a failure).
    group = _ci_gate()["concurrency"]["group"]
    assert group == (
        "ci-gate-${{ github.event.pull_request.number "
        "|| (github.event_name == 'push' && github.sha) || github.ref }}")
    assert _ci_gate()["concurrency"]["cancel-in-progress"] == "true"


def test_classify_job_outputs_the_narrowing_outputs():
    outputs = _ci_gate()["jobs"]["classify"].get("outputs") or {}
    assert "docs_only" in outputs
    assert "data_only" in outputs


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


def _ratchet_push_run() -> str:
    steps = _ratchet_push_job()["steps"]
    matches = [step for step in steps
               if step.get("name") == "Push the ratchet commit to master"]
    assert len(matches) == 1, "expected exactly one ratchet push step"
    step, = matches
    return step["run"]


# The pin is one function on purpose: the job's shape is one
# observation, and splitting it would let half the shape rot silently.
# pylint: disable-next=too-many-statements
def test_ratchet_push_workflow_pushes_with_the_deploy_key():
    raw = RATCHET_PUSH_WORKFLOW.read_text(encoding="utf-8")
    workflow = _load(RATCHET_PUSH_WORKFLOW)
    job = _ratchet_push_job()
    run = _ratchet_push_run()

    # The trigger is the ci-gate run's completion: tests.yml is a
    # workflow_call callee and produces no run of its own on a master
    # push — the run carrying the ratchet artifacts is ci-gate's.
    wr = (workflow.get("on") or {}).get("workflow_run") or {}
    assert wr.get("workflows") == ["ci gate"]
    assert wr.get("types") == ["completed"]

    # Only a completed green master push publishes: a PR run stages no
    # master data, and a failed or cancelled gate run's data is not the
    # tested state worth publishing.
    assert job["if"] == (
        "github.event.workflow_run.event == 'push' "
        "&& github.event.workflow_run.head_branch == 'master' "
        "&& github.event.workflow_run.conclusion == 'success'")
    assert job["environment"] == "master-push"
    assert job["timeout-minutes"] == "15"
    assert job["permissions"] == {"actions": "read"}
    assert workflow["permissions"] == {}
    assert workflow["concurrency"]["group"] == "ratchet-push"
    assert workflow["concurrency"]["cancel-in-progress"] == "false"

    steps = job["steps"]
    assert len(steps) == 4
    listing, download, suite_download, push = steps
    assert listing["name"] == "List the triggering run's artifacts"
    listing_run = listing.get("run") or ""
    assert listing["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert listing["env"]["RUN_ID"] == (
        "${{ github.event.workflow_run.id }}")
    assert "actions/runs/$RUN_ID/artifacts" in listing_run
    assert "grep -qx 'ratchet-push'" in listing_run
    assert "grep -qx 'suite-measurement'" in listing_run
    assert 'echo "ratchet=$ratchet"' in listing_run
    assert 'echo "suite=$suite"' in listing_run
    assert 'echo "any=true"' in listing_run

    # The artifacts belong to the TRIGGERING run: cross-run downloads
    # name its run id and carry a token, which the job's actions: read
    # caps.
    assert download["name"] == "Download the ratchet data"
    assert download["if"] == "steps.list.outputs.ratchet == 'true'"
    assert download["uses"] == (
        "actions/download-artifact@"
        "3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c")
    assert download["with"] == {
        "name": "ratchet-push",
        "path": "${{ runner.temp }}/ratchet-data",
        "run-id": "${{ github.event.workflow_run.id }}",
        "github-token": "${{ secrets.GITHUB_TOKEN }}",
    }
    assert suite_download["name"] == "Download the suite measurement"
    assert suite_download["if"] == "steps.list.outputs.suite == 'true'"
    assert suite_download["uses"] == (
        "actions/download-artifact@"
        "3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c")
    assert suite_download["with"] == {
        "name": "suite-measurement",
        "path": "${{ runner.temp }}/suite-data",
        "run-id": "${{ github.event.workflow_run.id }}",
        "github-token": "${{ secrets.GITHUB_TOKEN }}",
    }
    assert ("actions/download-artifact@"
            "3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c # v8.0.1" in raw)

    # The push step admits only when at least one artifact was staged.
    assert push["name"] == "Push the ratchet commit to master"
    assert push["if"] == "steps.list.outputs.any == 'true'"
    assert push["env"]["MASTER_PUSH_DEPLOY_KEY"] == (
        "${{ secrets.MASTER_PUSH_DEPLOY_KEY }}")
    assert push["env"]["REPO"] == "${{ github.repository }}"
    assert push["env"]["BASE_FALLBACK"] == (
        "${{ github.event.workflow_run.head_sha }}")
    assert "GH_TOKEN" not in (push.get("env") or {})
    assert "PUSH_REMOTE" not in push["env"]

    # The deploy key rides in an ssh agent, never a remote URL; the
    # empty-key refusal fails closed before anything runs.
    assert 'if [ -z "${MASTER_PUSH_DEPLOY_KEY:-}" ]' in run
    assert "the master-push environment is missing its secret" in run
    assert run.index("missing its secret") < run.index("ssh-agent")
    assert "trap 'kill \"$SSH_AGENT_PID\" 2>/dev/null || true' EXIT" in run
    assert "known_hosts" in run
    assert "StrictHostKeyChecking=yes" in run
    assert "x-access-token" not in run
    assert "GITHUB_TOKEN" not in run

    # The measured base: a raise staged base.txt; a suite-only run
    # measured the triggering run's own head (BASE_FALLBACK). Master
    # must still stand at it, or the data describes a tree master no
    # longer has.
    assert 'if [ -f "$RUNNER_TEMP/ratchet-data/base.txt" ]; then' in run
    assert 'base="$(cat "$RUNNER_TEMP/ratchet-data/base.txt")"' in run
    assert ('elif [ -f "$RUNNER_TEMP/suite-data/suite-measurement.json" ]'
            in run)
    assert 'base="$BASE_FALLBACK"' in run
    assert "no ratchet data and no suite measurement to push" in run
    assert run.index("remote=") < run.index("git ls-remote")

    # The push order and its refusals: the base staleness check, the
    # artifact-presence guards, the tighten, the no-change guard, the
    # commit, and the rejected-push fallback.
    assert run.index("git ls-remote") < run.index("git init")
    assert ('cp "$RUNNER_TEMP/ratchet-data/ci-thresholds.json" '
            '"$RUNNER_TEMP/ratchet-repo/.github/ci-thresholds.json"' in run)
    assert ('git -C "$RUNNER_TEMP/ratchet-repo" '
            'add .github/ci-thresholds.json' in run)
    assert ("git -C \"$RUNNER_TEMP/ratchet-repo\" "
            "config user.name 'github-actions[bot]'" in run)
    assert "41898282+github-actions[bot]@users.noreply.github.com" in run
    assert ("Ratchet ci-thresholds: raise coverage floor / tighten "
            "baselines (automated)" in run)
    assert "Co-Authored-By" not in run
    # The suite-cost tighten: a downward-only data operation, then the
    # no-change guard that lets a no-op tighten end the job quietly.
    assert "suite_ratchet.py" in run
    assert ('--tighten "$RUNNER_TEMP/suite-data/suite-measurement.json"'
            in run)
    assert run.index("suite_ratchet.py") < run.index(
        'add .github/ci-thresholds.json')
    assert "status --porcelain .github/ci-thresholds.json" in run
    assert run.index("status --porcelain") < run.index("commit -m")
    assert "nothing to commit" in run
    assert 'git -C "$RUNNER_TEMP/ratchet-repo" push origin HEAD:master' in run
    assert 'git -C "$RUNNER_TEMP/ratchet-repo" fetch --quiet origin master' in run
    assert "the ratchet push was rejected while master stood still" in run
    assert "rev-parse HEAD^" in run
    assert "rev-parse FETCH_HEAD" in run
    assert "--force" not in run

    # The key's shapes are gone from tests.yml ENTIRELY, and the callee
    # no longer carries the push job at all: a coordinated edit that
    # re-adds an env mapping, a key read, an agent, or the job fails
    # here, not just at the workflow pin.
    tests_raw = TESTS_WORKFLOW.read_text(encoding="utf-8")
    assert "MASTER_PUSH_DEPLOY_KEY" not in tests_raw
    assert "ssh-agent" not in tests_raw
    assert "x-access-token" not in tests_raw
    assert set(_tests_workflow()["jobs"]) == {"pytest", "pytest-portable"}


def test_the_tests_callee_is_capped_at_what_its_jobs_use():
    # A callee's token is capped by the caller's grant at the uses:
    # site. The callee pushes nothing since issue #479 (the ratchet
    # push lives in the top-level ratchet-push.yml workflow, where the
    # master-push environment's secret resolves), so contents: read
    # covers every job it declares; pull-requests: write stays for the
    # pytest job's informational patch-coverage comment. The pytest
    # job's narrower self-declared block still governs that job.
    doc = _ci_gate()
    calls = [job for job in (doc.get("jobs") or {}).values()
             if (job.get("uses") or "")
             == "./.github/workflows/tests.yml"]
    assert len(calls) == 1
    call = calls[0]
    assert call["permissions"] == {
        "contents": "read",
        "pull-requests": "write",
    }
    # No secret is forwarded to the callee: the call maps no secrets,
    # and the callee's workflow_call trigger declares none either.
    assert "secrets" not in call
    workflow_call = (_tests_workflow().get("on") or {}).get(
        "workflow_call") or {}
    assert "secrets" not in workflow_call


def test_tests_and_refresh_postgres_images_are_digest_pinned_in_lockstep():
    # Each side's digest is pinned separately elsewhere; the EQUALITY is
    # pinned here -- refresh-pricing could not move its postgres image
    # alone without this failing.
    tests_image = (
        yaml.safe_load(TESTS_WORKFLOW.read_text(encoding="utf-8"))
        ["jobs"]["pytest"]["services"]["postgres"]["image"])
    refresh_image = (
        yaml.safe_load(REFRESH_WORKFLOW.read_text(encoding="utf-8"))
        ["jobs"]["refresh"]["services"]["postgres"]["image"])
    assert tests_image == refresh_image
    assert tests_image.startswith("postgres:16@sha256:")


def test_the_pytest_job_pushes_nothing_and_the_deploy_key_has_two_wirings():
    raw = TESTS_WORKFLOW.read_text(encoding="utf-8")
    # The deploy key has ZERO wiring in tests.yml; its only load sites
    # in the repo are the two environment-backed push jobs. The pytest
    # job pushes nothing.
    assert "MASTER_PUSH_DEPLOY_KEY" not in raw
    assert "ssh-agent" not in raw
    for name, path in (("refresh-pricing.yml", REFRESH_WORKFLOW),
                       ("ratchet-push.yml", RATCHET_PUSH_WORKFLOW)):
        text = path.read_text(encoding="utf-8")
        assert text.count("secrets.MASTER_PUSH_DEPLOY_KEY") >= 1, name
    assert raw.count("secrets.MASTER_PUSH_DEPLOY_KEY") == 0
    pytest_job = _tests_workflow()["jobs"]["pytest"]
    for step in pytest_job.get("steps") or []:
        assert "git push" not in (step.get("run") or ""), step.get("name")


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
