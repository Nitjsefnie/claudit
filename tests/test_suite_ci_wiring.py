"""Workflow wiring: the canary measures and gates the suite bench.

The old speed.yml measured one pass in a leg of its own; since issue
#515 the tests canary owns the measurement AND the gate (one bench pass
per ci-gate run), and speed.yml is gone. Every case here pins the shape
by its DECODED scalars -- exact expressions, exact commands, exact
artifact names -- not the presence of a script text.
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

# The exact commands the shared composite action's steps must run, as
# the runner's shell sees them: the measurement (its --summary lands in
# the step summary) and the gate (its exit code must be the step's own,
# so a failed check fails the caller's job). The MEASUREMENT env var is
# the action's input; the callers all point it at the same file.
MEASURE_CMD = (
    'python scripts/ci/suite_bench.py --write "$MEASUREMENT" '
    '--summary "$GITHUB_STEP_SUMMARY"')
CHECK_CMD = 'python scripts/ci/suite_bench.py --check "$MEASUREMENT"'
TIGHTEN_CMD = (
    'python3 "$RUNNER_TEMP/ratchet-repo/scripts/ci/suite_ratchet.py" '
    '--tighten "$RUNNER_TEMP/suite-data/suite-measurement.json" '
    '--thresholds '
    '"$RUNNER_TEMP/ratchet-repo/.github/ci-thresholds.json"')
BENCH_ACTION = "./.github/actions/suite-bench"


def _load(name):
    # BaseLoader, not safe_load: YAML 1.1 parses the bare key `on` as
    # the boolean True, and these tests read decoded scalars.
    return yaml.load(
        (WORKFLOWS / name).read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader) or {}


def _job(doc, job_id):
    return doc["jobs"][job_id]


def _steps(job):
    return job["steps"]


def _run_lines(step):
    """The step's run block as a list of stripped non-empty lines.

    Block indentation is YAML shape, not command shape: the pins below
    compare and order the COMMANDS the shell sees.
    """
    return [line.strip() for line in (step.get("run") or "").splitlines()
            if line.strip()]


def _find_step(job, needle):
    """The one step whose run names `needle`; fails otherwise."""
    matches = [step for step in _steps(job) if needle in (step.get("run")
                                                          or "")]
    assert len(matches) == 1, (needle, [s.get("name") for s in matches])
    return matches[0]


# --- the bench lives in the tests canary -------------------------------------

def test_the_bench_action_measures_and_gates():
    """The composite action's own shape: measure, then gate."""
    action = yaml.load(
        (ROOT / ".github" / "actions" / "suite-bench" / "action.yml")
        .read_text(encoding="utf-8"), Loader=yaml.BaseLoader) or {}
    assert action["name"] == "Suite cost bench"
    steps = action["runs"]["steps"]
    measure = steps[0]
    assert measure["id"] == "measure"
    assert MEASURE_CMD in _run_lines(measure)
    # The action's output is declared, so the callers' steps.*.outputs
    # plumbing reads a real value.
    assert action["outputs"]["measured"]["value"] == (
        "${{ steps.measure.outputs.measured }}")
    gate = steps[1]
    # One mode only: the canary always gates after measuring (issue
    # #515). The record-vs-gate split died with the second leg.
    assert "if" not in gate
    assert "mode" not in (action.get("inputs") or {})
    lines = _run_lines(gate)
    # The check's exit code must be the LAST command of its step: the
    # step fails when the bench's gate fails, whatever else the block
    # prints around it.
    assert lines[-1] == CHECK_CMD


def test_speed_yml_is_gone():
    # The fold (issue #515): one bench pass per ci-gate run, in the
    # tests canary; a second workflow to run it again is a regression.
    assert not (WORKFLOWS / "speed.yml").exists()


def test_tests_job_measures_and_gates_on_every_event():
    doc = _load("tests.yml")
    job = _job(doc, "pytest")
    bench = [step for step in _steps(job)
             if step.get("uses") == BENCH_ACTION]
    assert len(bench) == 1
    bench = bench[0]
    assert bench["id"] == "suite_bench"
    # The gate runs wherever the canary runs -- every non-docs,
    # non-data event. Only a green suite and a green JavaScript
    # coverage pass are worth measuring.
    assert bench["with"] == {
        "measurement": "${{ runner.temp }}/suite-measurement.json"}
    assert bench["if"] == (
        "${{ !cancelled() && steps.pytest.outcome == 'success' "
        "&& steps.jscov.outcome == 'success' }}")

    # One artifact name, and it is the bot's feed. A red gate's numbers
    # stay inspectable: the upload runs whenever the bench ran at all.
    uploads = [step for step in _steps(job)
               if (step.get("uses") or "").startswith(
                   "actions/upload-artifact")
               and (step.get("with") or {}).get("name")
               == "suite-measurement"]
    assert len(uploads) == 1
    upload = uploads[0]
    assert upload["if"] == (
        "${{ !cancelled() && steps.suite_bench.outcome != 'skipped' }}")
    assert upload["with"]["path"] == (
        "${{ runner.temp }}/suite-measurement.json")
    assert upload["with"]["if-no-files-found"] == "error"
    assert upload["with"]["retention-days"] == "1"

    # A red bench gate must not stage ratchet data inside the failed
    # job, whatever the run-level fold does with it.
    ratchet = [step for step in _steps(job)
               if step.get("name") == "Ratchet the thresholds"]
    assert len(ratchet) == 1
    ratchet_if = ratchet[0].get("if") or ""
    assert "steps.suite_bench.outcome == 'success'" in ratchet_if

    # The push lives in ratchet-push.yml, not here.
    assert not _job(doc, "pytest").get("outputs")


def test_the_suite_measurement_upload_is_unique_repo_wide():
    # The bot (ratchet-push.yml) reads one artifact name out of the
    # triggering run. Two uploads of one name in the same ci-gate run
    # made that download a coin toss (issue #496); the fold (issue
    # #515) leaves exactly ONE publisher of the measurement.
    uploaders = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        doc = _load(path.name)
        for job_id, job in (doc.get("jobs") or {}).items():
            for step in (job or {}).get("steps") or []:
                name = (step.get("with") or {}).get("name") or ""
                if (step.get("uses") or "").startswith(
                        "actions/upload-artifact") and "suite" in name:
                    uploaders.append(f"{path.name}:{job_id}:{name}")
    assert uploaders == ["tests.yml:pytest:suite-measurement"]


def test_the_canary_never_compares_two_checkouts():
    # The A/B design is GONE, and the things that existed only to serve
    # it are gone with it: no baseline-release lookup, no merge-base,
    # no dual checkout, no interleaved rounds, no comparator, no JUnit.
    for name in (WORKFLOWS / "tests.yml",
                 ROOT / ".github" / "actions" / "suite-bench" / "action.yml"):
        text = (ROOT / name).read_text(encoding="utf-8")
        for gone in ("compare_durations", "merge_base", "MAX_REGRESSION",
                     "ROUNDS", "junitxml", "venv-base", "venv-head",
                     "releases/latest"):
            assert gone not in text, (name, gone)


# --- ratchet-push.yml (the bot side) -----------------------------------------


# --- ratchet-push.yml (the bot side) -----------------------------------------

def test_ratchet_push_job_tightens_from_the_artifact():
    doc = _load("ratchet-push.yml")
    job = _job(doc, "push")
    # The bot job's admission lives in the job's if (pinned in
    # test_workflow_ci_gate); the artifacts come from the TRIGGERING
    # run, downloaded cross-run.
    downloads = [step for step in _steps(job)
                 if (step.get("uses") or "").startswith(
                     "actions/download-artifact")]
    names = sorted((step.get("with") or {}).get("name", "")
                   for step in downloads)
    assert names == ["ratchet-push", "suite-measurement"]
    for step in downloads:
        assert step["with"]["run-id"] == (
            "${{ github.event.workflow_run.id }}")
    # The tighten is a DATA operation in the bot job: no checkout, no
    # pip, no test code anywhere in the job's own text.
    text = (WORKFLOWS / "ratchet-push.yml").read_text(encoding="utf-8")
    job_text = text[text.index("  push:"):]
    for banned in ("actions/checkout", "pip install", "pytest "):
        assert banned not in job_text, banned
    # The runner sees one line: the quoted script path and its flag.
    tighten = _find_step(job, 'suite_ratchet.py" --tighten')
    assert TIGHTEN_CMD in _run_lines(tighten)


def test_ratchet_push_commits_only_when_something_changed():
    doc = _load("ratchet-push.yml")
    job = _job(doc, "push")
    push = _find_step(job, 'suite_ratchet.py" --tighten')
    lines = _run_lines(push)
    # Order is load-bearing: the tighten, then the porcelain check,
    # then the guarded commit -- and a no-op run is a NORMAL ending
    # (exit 0), not a failure, because the job now runs for the
    # tighten alone.
    tighten_index = lines.index(TIGHTEN_CMD)
    guard_index = next(index for index, line in enumerate(lines)
                       if "status --porcelain" in line)
    commit_index = next(index for index, line in enumerate(lines)
                        if line.startswith("git -C \"$RUNNER_TEMP/ratchet-repo\" commit"))
    assert tighten_index < guard_index < commit_index
    nothing_index = next(index for index, line in enumerate(lines)
                         if "nothing to commit" in line)
    assert lines[nothing_index + 1] == "exit 0"


# --- the deleted A/B machinery stays deleted ---------------------------------

def test_compare_durations_is_gone():
    assert not (ROOT / "scripts" / "ci" / "compare_durations.py").exists()
    assert not (ROOT / "tests" / "test_compare_durations.py").exists()


def test_ratchet_data_reads_are_guarded_on_artifact_presence():
    # A suite-only run (no ratchet-data artifact, the suite measurement
    # present) is the COMMON admission: the ratchet-data files were
    # never downloaded, and the push step's default shell is `bash -e`
    # — an unguarded `cat base.txt` aborts the step and the tighten
    # never runs. Every read of the ratchet-data artifact must sit
    # inside a presence guard, ordered before the read.
    doc = _load("ratchet-push.yml")
    job = _job(doc, "push")
    push = _find_step(job, 'suite_ratchet.py" --tighten')
    lines = _run_lines(push)
    guard = 'if [ -f "$RUNNER_TEMP/ratchet-data/base.txt" ]; then'
    guard_index = lines.index(guard)
    cat_index = next(index for index, line in enumerate(lines)
                     if 'cat "$RUNNER_TEMP/ratchet-data/base.txt"' in line)
    assert guard_index < cat_index, (
        "the ratchet-data read is not behind its presence guard")
    # The guarded block is closed, and the tighten comes after it: the
    # absence path degrades to the BASE_FALLBACK base (the triggering
    # run's head) rather than dropping the run.
    close_index = next(index for index, line in enumerate(lines)
                       if index > cat_index and line == "fi")
    tighten_index = lines.index(TIGHTEN_CMD)
    assert close_index < tighten_index


def test_the_bench_runner_pins_the_seed_interpreter():
    # The budgets are exact instruction counts for ONE interpreter. The
    # canary is both the gate and the record path now (issue #515), so
    # its exact micro the seed was measured with is all that is left to
    # pin (the node coverage pin's reason, issue #266).
    doc = _load("tests.yml")
    job = _job(doc, "pytest")
    setups = [step for step in _steps(job)
              if (step.get("uses") or "").startswith("actions/setup-python")]
    assert len(setups) == 1
    assert setups[0]["with"]["python-version"] == "3.13.14"


def _suite_jobs():
    """Every (workflow, job) that runs the suite or the bench, found by
    READING the workflows rather than by being named here.

    The point of deriving the list is that a new leg running the suite
    inherits the pin without anyone remembering to add it: naming the
    legs in this test is what let a third and fourth shallow checkout
    ship a red gate (#502).
    """
    for path in sorted(WORKFLOWS.glob("*.yml")):
        doc = yaml.load(
            path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader) or {}
        for job_id, job in (doc.get("jobs") or {}).items():
            steps = job.get("steps") or []
            runs_suite = any(
                "pytest" in (step.get("run") or "")
                or BENCH_ACTION in (step.get("uses") or "")
                for step in steps)
            if runs_suite:
                yield path.name, job_id, job


def _reads_the_marker(checkout):
    """Whether a checkout can resolve the merge commit's pull-request
    head one level down.

    An ABSENT ``fetch-depth`` IS ``actions/checkout``'s default 1, so
    the key's absence is the failure rather than an exemption from the
    check. 0 is the full history the default steps away from.
    """
    depth = (checkout.get("with") or {}).get("fetch-depth")
    return depth is not None and int(depth) != 1


def test_the_depth_predicate_reads_an_absent_key_as_the_default():
    # The predicate's other claim: without this, the `depth is None`
    # branch is never exercised by the derived scan below (every leg
    # spells fetch-depth today), so a mutant inverting it survives.
    assert not _reads_the_marker({})
    assert not _reads_the_marker({"with": {"fetch-depth": "1"}})
    assert not _reads_the_marker({"with": {}})
    assert _reads_the_marker({"with": {"fetch-depth": "2"}})
    assert _reads_the_marker({"with": {"fetch-depth": "0"}})


def test_every_leg_running_the_suite_can_read_the_reseed_marker():
    # The suite_cost re-seed marker is read from HEAD and, on a
    # pull-request run, from the pull request's head one level under
    # the merge commit (scripts/ci/reseed.py). The default depth-1
    # checkout has no parent at all, so a leg that shallow-checked out
    # cannot see the marker, the loader fails closed, and every
    # committed-document test in that leg goes red on the very commit
    # that declares the re-seed (issue #502).
    checked = 0
    for name, job_id, job in _suite_jobs():
        checkouts = [step for step in _steps(job)
                     if (step.get("uses") or "").startswith(
                         "actions/checkout")]
        if not checkouts:
            # A job with no checkout of its own has no tree of its own
            # to read; today none of the derived jobs takes this path,
            # and `checked` below is what would notice a new one.
            continue
        assert len(checkouts) == 1, f"{name}:{job_id}"
        assert _reads_the_marker(checkouts[0]), (
            f"{name}:{job_id} checks out at depth 1 and cannot read the "
            "suite-cost re-seed marker")
        checked += 1
    assert checked >= 4, f"only {checked} suite legs found: the scan is broken"
