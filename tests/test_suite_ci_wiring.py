"""Workflow wiring: the speed leg runs the bench and gates on it.

The old speed.yml compared wall-clock durations between two checkouts
(A/B/A/B against a pinned baseline release); the new one measures one
run's instruction counts and gates them against the committed
suite_cost budgets. Every case here pins the NEW shape by its DECODED
scalars -- exact expressions, exact commands, exact artifact names --
not the presence of a script text, and the accompanying mutation table
(in the introducing change's report) proves each pin dies on its
mutant.
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

MASTER_PUSH = "github.event_name == 'push' && github.ref == 'refs/heads/master'"


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


# --- speed.yml ---------------------------------------------------------------

def test_speed_job_keeps_its_name_gate_and_fork_environment():
    doc = _load("speed.yml")
    assert doc["name"] == "speed"
    job = _job(doc, "speed")
    # The bench still EXECUTES pull-request code, so the fork
    # admission gate is load-bearing; the decoded expression, exact.
    assert job["environment"] == (
        "${{ github.event.pull_request.head.repo.fork "
        "&& 'fork-speed-benchmark' || 'speed-benchmark' }}")
    # Least privilege at the workflow level, which the leg inherits.
    assert doc["permissions"] == {"contents": "read"}


def test_the_bench_action_measures_and_gates():
    """The composite action's own shape: measure, then gate on demand."""
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
    assert gate["if"] == "inputs.mode == 'gate'"
    lines = _run_lines(gate)
    # The check's exit code must be the LAST command of its step: the
    # step fails when the bench's gate fails, whatever else the block
    # prints around it.
    assert lines[-1] == CHECK_CMD


def test_speed_job_runs_the_bench_action_in_gate_mode():
    doc = _load("speed.yml")
    job = _job(doc, "speed")
    bench = [step for step in _steps(job)
             if step.get("uses") == BENCH_ACTION]
    assert len(bench) == 1
    assert bench[0]["with"]["mode"] == "gate"
    assert bench[0]["with"]["measurement"] == (
        "${{ runner.temp }}/suite-measurement.json")


def test_speed_job_uploads_the_measurement():
    doc = _load("speed.yml")
    job = _job(doc, "speed")
    uploads = [step for step in _steps(job)
               if (step.get("uses") or "").startswith(
                   "actions/upload-artifact")]
    assert len(uploads) == 1
    upload = uploads[0]
    # NOT tests.yml's `suite-measurement`: both legs run in the same
    # ci-gate run, and one name shared by two uploads made the tighten
    # bot's download-by-name a coin toss between them (issue #496).
    assert upload["with"]["name"] == "suite-speed-measurement"
    assert upload["with"]["path"] == (
        "${{ runner.temp }}/suite-measurement.json")
    assert upload["with"]["if-no-files-found"] == "error"


def test_the_two_suite_measurement_uploads_cannot_collide():
    # The bot (ratchet-push.yml) reads one artifact by name out of the
    # triggering run: `suite-measurement`, which tests.yml publishes.
    # speed.yml publishes a second measurement of the same pass, so the
    # two names must differ or the bot cannot say which one it read.
    speed = _load("speed.yml")
    tests = _load("tests.yml")

    def _upload_names(doc, job):
        return [((step.get("with") or {}).get("name"))
                for step in _steps(_job(doc, job))
                if (step.get("uses") or "").startswith(
                    "actions/upload-artifact")
                and "suite" in str((step.get("with") or {}).get("name"))]

    speed_names = _upload_names(speed, "speed")
    tests_names = _upload_names(tests, "pytest")
    assert speed_names == ["suite-speed-measurement"]
    assert tests_names == ["suite-measurement"]
    assert not set(speed_names) & set(tests_names)


def test_speed_job_measures_on_postgres():
    doc = _load("speed.yml")
    job = _job(doc, "speed")
    # The fixture runs the scratch-DB machinery, so the service is
    # load-bearing: the image is pinned by digest, exactly the one the
    # tests leg measures against.
    image = job["services"]["postgres"]["image"]
    assert image == (
        "postgres:16@sha256:1a6ab3f5345eb6dbe04a1349529caabdb0ab09293a0"
        "9590fad07b2246bfa4b54")
    wait = _find_step(job, "pg_isready")
    assert wait["run"]
    env = job["env"]
    assert env["PGHOST"] == "localhost"
    assert env["PGPORT"] == "5432"
    assert env["PGUSER"] == "postgres"


def test_speed_job_never_compares_two_checkouts():
    # The A/B design is GONE, and the things that existed only to serve
    # it are gone with it: no baseline-release lookup, no merge-base,
    # no dual checkout, no interleaved rounds, no comparator, no JUnit.
    text = (WORKFLOWS / "speed.yml").read_text(encoding="utf-8")
    for gone in ("compare_durations", "merge_base", "MAX_REGRESSION",
                 "ROUNDS", "junitxml", "venv-base", "venv-head",
                 "releases/latest"):
        assert gone not in text, gone


def test_speed_job_pip_cache_shape():
    # Restore runs on every event (no `if`); save runs only from a
    # master push, directly after the dependency install, under a key
    # that hashes the SINGLE tree's requirement files (the A/B design's
    # base+head key is gone).
    doc = _load("speed.yml")
    job = _job(doc, "speed")
    steps = _steps(job)
    restores = [step for step in steps
                if (step.get("uses") or "").startswith(
                    "actions/cache/restore")]
    saves = [step for step in steps
             if (step.get("uses") or "").startswith("actions/cache/save")]
    assert len(restores) == 1 and len(saves) == 1
    assert "if" not in restores[0]
    assert saves[0]["if"] == (
        "github.event_name == 'push' "
        "&& github.ref == 'refs/heads/master'")
    install_index = steps.index(_find_step(job, "pip install -r"))
    assert steps.index(saves[0]) == install_index + 1
    expected_key = (
        "pip-${{ runner.os }}-3.13-"
        "${{ hashFiles('backend/requirements.txt', "
        "'requirements-test.txt') }}")
    assert restores[0]["with"]["key"] == expected_key
    assert saves[0]["with"]["key"] == expected_key


def test_speed_job_pins_the_seed_interpreter():
    # The budgets are exact instruction counts: a runner-side Python
    # drift would move them out from under the committed ceilings, so
    # the exact micro the seed was measured with is pinned (the node
    # coverage pin's reason, issue #266).
    doc = _load("speed.yml")
    job = _job(doc, "speed")
    setups = [step for step in _steps(job)
              if (step.get("uses") or "").startswith("actions/setup-python")]
    assert len(setups) == 1
    assert setups[0]["with"]["python-version"] == "3.13.14"


def test_speed_job_timeout_is_bound_and_margin_sized():
    doc = _load("speed.yml")
    job = _job(doc, "speed")
    # Measured: one bench pass ~115 s on the seeding box under
    # instrumentation; 15 minutes is a >6x margin, and the bound exists
    # so a hung run fails loud instead of billing the hour.
    assert job["timeout-minutes"] == "15"


# --- tests.yml (the measure-and-stage side) ----------------------------------

def test_tests_job_measures_on_master_pushes_only():
    doc = _load("tests.yml")
    job = _job(doc, "pytest")
    bench = [step for step in _steps(job)
             if step.get("uses") == BENCH_ACTION]
    assert len(bench) == 1
    assert bench[0]["id"] == "suite_bench"
    # Record mode: the tests leg only measures -- the gate lives in
    # speed.yml, and the push lives in ratchet-push.yml.
    assert bench[0]["with"]["mode"] == "record"
    assert bench[0]["if"] == (
        "${{ !cancelled() && steps.pytest.outcome == 'success' "
        "&& steps.jscov.outcome == 'success' && "
        "github.event_name == 'push' "
        "&& github.ref == 'refs/heads/master' }}")
    uploads = [step for step in _steps(job)
               if (step.get("uses") or "").startswith(
                   "actions/upload-artifact")
               and (step.get("with") or {}).get("name")
               == "suite-measurement"]
    assert len(uploads) == 1


def test_tests_job_publishes_no_outputs_any_more():
    # Dead wiring since issue #479: the push job that consumed
    # ratchet_changed/suite_measured moved to ratchet-push.yml, and the
    # artifact presence IS the signal there (its list step reads the
    # triggering run's artifacts). The job-level outputs mapping must
    # not silently return.
    doc = _load("tests.yml")
    assert not (_job(doc, "pytest").get("outputs")), (
        "the pytest job's outputs relay died with the in-callee push job")


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


def test_both_bench_paths_pin_the_seed_interpreter():
    # The budgets are exact instruction counts for ONE interpreter. The
    # gate (speed.yml) and the record path (tests.yml's pytest job,
    # whose measurement feeds the tighten) must run the same exact
    # micro: a floating record-side alias that drifts to a cheaper
    # 3.13.x lets the bot tighten ceilings below what the pinned gate
    # interpreter measures -- a red gate with no legal raise.
    for name, job_id in (("speed.yml", "speed"), ("tests.yml", "pytest")):
        doc = _load(name)
        job = _job(doc, job_id)
        setups = [step for step in _steps(job)
                  if (step.get("uses") or "").startswith(
                      "actions/setup-python")]
        assert len(setups) == 1, name
        assert setups[0]["with"]["python-version"] == "3.13.14", name
