"""The tests canary measures and gates the suite bench once per ci-gate run.
Assert decoded scalars, expressions, commands and artifact names so decoy
script text cannot satisfy the workflow contract."""
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
    # The canary must always gate after measuring; no alternate mode may skip it.
    assert "if" not in gate
    assert "mode" not in (action.get("inputs") or {})
    lines = _run_lines(gate)
    # The check's exit code must be the LAST command of its step: the
    # step fails when the bench's gate fails, whatever else the block
    # prints around it.
    assert lines[-1] == CHECK_CMD


def test_every_run_step_binds_MEASUREMENT_to_the_measurement_input():
    """The command strings above quote `"$MEASUREMENT"`, so both steps
    read one variable -- but nothing pinned what that variable is BOUND
    to (the twin): an edit pointing a step's env at a
    file the measuring step never wrote keeps this suite green while
    the gate passes on whatever is at the second path, which on a
    fresh runner is nothing at all.
    """
    action = yaml.load(
        (ROOT / ".github" / "actions" / "suite-bench" / "action.yml")
        .read_text(encoding="utf-8"), Loader=yaml.BaseLoader) or {}
    misbound = [
        step.get("name", "<unnamed>")
        for step in action["runs"]["steps"]
        if "run" in step
        and step.get("env", {}).get("MEASUREMENT") != "${{ inputs.measurement }}"
    ]
    assert not misbound, (
        "these run steps bind MEASUREMENT to anything other than the "
        f"action's measurement input: {misbound}")


def test_speed_yml_is_gone():
    # The fold: one bench pass per ci-gate run, in the
    # tests canary; a second workflow to run it again is a regression.
    assert not (WORKFLOWS / "speed.yml").exists()


def test_tests_job_measures_and_gates_on_every_event():
    doc = _load("tests.yml")
    job = _job(doc, "pytest")
    steps = _steps(job)
    bench = [step for step in steps
             if step.get("id") == "suite_bench"]
    assert len(bench) == 1
    bench = bench[0]
    guard = next(step for step in steps
                 if step.get("name") == "Threshold direction guard")
    assert steps.index(guard) + 1 == steps.index(bench)
    # The gate runs wherever the canary runs -- every non-docs,
    # non-data event. Only a green suite and a green JavaScript
    # coverage pass are worth measuring. The bench action's own input
    # (measurement) is contract and deliberately unread — fleet-rules,
    # "Merging and CI" with:-inputs ruling.
    assert bench["if"] == (
        "${{ !cancelled() && steps.pytest.outcome == 'success' "
        "&& (steps.jscov.outcome == 'success' "
        "|| inputs.thresholds_only == 'true') }}")

    # One upload, located by its condition (the workflow's behaviour,
    # never a with: input — fleet-rules, "Merging and CI" with:-inputs
    # ruling). A red gate's numbers stay inspectable: the upload runs
    # whenever the bench ran at all.
    uploads = [step for step in _steps(job)
               if (step.get("uses") or "").startswith(
                   "actions/upload-artifact")
               and "steps.suite_bench" in (step.get("if") or "")]
    assert len(uploads) == 1
    upload = uploads[0]
    assert upload["if"] == (
        "${{ !cancelled() && steps.suite_bench.outcome != 'skipped' }}")

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
    # The bot (ratchet-push.yml) reads artifact names out of the
    # triggering run. Two uploads of one name in the same ci-gate run
    # would make the download ambiguous: require one publisher per name. Names
    # are DERIVED from the download side — workflow against workflow,
    # no literal (fleet-rules, "Merging and CI"): rename both together
    # and this follows; rename one and the disagreement reds.
    bot_doc = _load("ratchet-push.yml")
    download_names = sorted({
        name
        for job in (bot_doc.get("jobs") or {}).values()
        for step in (job or {}).get("steps") or []
        if (step.get("uses") or "").startswith("actions/download-artifact")
        if isinstance(name := (step.get("with") or {}).get("name"), str)})
    assert download_names, "the bot downloads nothing — the sweep is vacuous"
    uploaders = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        doc = _load(path.name)
        for job_id, job in (doc.get("jobs") or {}).items():
            for step in (job or {}).get("steps") or []:
                name = (step.get("with") or {}).get("name") or ""
                if (step.get("uses") or "").startswith(
                        "actions/upload-artifact") and name in download_names:
                    uploaders.append(f"{path.name}:{job_id}:{name}")
    assert sorted(uploaders) == sorted(
        f"tests.yml:pytest:{name}" for name in download_names), sorted(
            uploaders)


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


def test_ratchet_push_classifies_the_fetched_tip_before_merging():
    job = _job(_load("ratchet-push.yml"), "push")
    lines = _run_lines(_find_step(job, 'suite_ratchet.py" --tighten'))
    checkout = lines.index(
        'git -C "$RUNNER_TEMP/ratchet-repo" checkout -B push FETCH_HEAD')
    fetched = lines.index(
        'fetched="$(git -C "$RUNNER_TEMP/ratchet-repo" rev-parse HEAD)"')
    stale = lines.index('if [ "$fetched" != "$base" ]; then')
    classify = lines.index(
        'if ! (cd "$RUNNER_TEMP/ratchet-repo" && python3 scripts/ci/'
        'ratchet_staleness.py --base "$base" --tip "$fetched"); then')
    merge = lines.index(
        'if ! git -C "$RUNNER_TEMP/ratchet-repo" merge-file '
        '.github/ci-thresholds.json "$RUNNER_TEMP/ratchet-base.json" '
        '"$RUNNER_TEMP/ratchet-data/ci-thresholds.json"; then')
    ancestor = lines.index(
        'if ! git -C "$RUNNER_TEMP/ratchet-repo" show '
        '"$base:.github/ci-thresholds.json" > "$RUNNER_TEMP/ratchet-base.json"; then')
    validate = lines.index(
        'if ! python3 "$RUNNER_TEMP/ratchet-repo/scripts/ci/thresholds.py" '
        '--check; then')
    assert checkout < fetched < stale < classify < ancestor < merge < validate
    assert validate < lines.index(TIGHTEN_CMD)
    assert not any("ls-remote" in line for line in lines[:stale])
    for start, stop in ((classify, ancestor), (merge, validate),
                        (validate, lines.index(TIGHTEN_CMD))):
        branch = lines[start:stop]
        assert any("::notice::" in line for line in branch)
        assert any('>> "$GITHUB_STEP_SUMMARY"' in line for line in branch)
        assert "exit 0" in branch


def test_ratchet_push_drops_an_unreadable_ancestor_before_commit_or_push():
    job = _job(_load("ratchet-push.yml"), "push")
    lines = _run_lines(_find_step(job, 'suite_ratchet.py" --tighten'))
    ancestor = lines.index(
        'if ! git -C "$RUNNER_TEMP/ratchet-repo" show '
        '"$base:.github/ci-thresholds.json" > "$RUNNER_TEMP/ratchet-base.json"; then')
    end = lines.index("fi", ancestor + 1)
    branch = lines[ancestor + 1:end]
    assert any("::notice::" in line for line in branch)
    assert any('>> "$GITHUB_STEP_SUMMARY"' in line for line in branch)
    assert branch[-1] == "exit 0"
    merge = next(index for index, line in enumerate(lines) if "merge-file" in line)
    commit = next(index for index, line in enumerate(lines)
                  if line.startswith('git -C "$RUNNER_TEMP/ratchet-repo" commit'))
    push = next(index for index, line in enumerate(lines)
                if 'push origin HEAD:master' in line)
    assert ancestor < end < merge < lines.index(TIGHTEN_CMD) < commit < push


def test_ratchet_push_rejected_push_handler_stays_guarded():
    job = _job(_load("ratchet-push.yml"), "push")
    lines = _run_lines(_find_step(job, 'suite_ratchet.py" --tighten'))
    rejected = lines.index(
        'if ! git -C "$RUNNER_TEMP/ratchet-repo" push origin HEAD:master; then')
    assert lines[rejected:] == [
        'if ! git -C "$RUNNER_TEMP/ratchet-repo" push origin HEAD:master; then',
        'git -C "$RUNNER_TEMP/ratchet-repo" fetch --quiet origin master',
        'if [ "$(git -C "$RUNNER_TEMP/ratchet-repo" rev-parse HEAD^)" = '
        '"$(git -C "$RUNNER_TEMP/ratchet-repo" rev-parse FETCH_HEAD)" ]; then',
        'echo "the ratchet push was rejected while master stood still" >&2',
        'exit 1',
        'fi',
        'echo "::notice::Master moved while this run measured; '
        'the next push raises instead"',
        '{',
        'echo "### Dropped: master moved while this run measured"',
        'echo "This run\'s raise is dropped; the next master push raises instead."',
        '} >> "$GITHUB_STEP_SUMMARY"',
        'fi',
    ]


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
    # Budgets count instructions for one interpreter; gate and recording
    # must use the same exact patch version.
    doc = _load("tests.yml")
    job = _job(doc, "pytest")
    setups = [step for step in _steps(job)
              if (step.get("uses") or "").startswith("actions/setup-python")]
    assert len(setups) == 1
    assert setups[0]["with"]["python-version"] == "3.13.14"
