"""The tests canary measures and gates the suite bench once per ci-gate run.
Assert decoded scalars, expressions, commands and artifact names so decoy
script text cannot satisfy the workflow contract."""
from __future__ import annotations

import pytest
import yaml
from tests.workflow_checks import (ROOT, job_of as _job, workflow_by_name as _load, find_step as _find_step, run_lines as _run_lines, steps_of as _steps, TIGHTEN_CMD, WORKFLOWS)

# The exact commands the shared composite action's steps must run, as
# the runner's shell sees them: the measurement (its --summary lands in
# the step summary) and the gate (its exit code must be the step's own,
# so a failed check fails the caller's job). The measurement env var is
# the action's input; the callers all point it at the same file.
MEASURE_CMD = (
    'python scripts/ci/suite_bench.py --write "$CLAUDIT_MEASUREMENT" '
    '--summary "$GITHUB_STEP_SUMMARY"')
CHECK_CMD = 'python scripts/ci/suite_bench.py --check "$CLAUDIT_MEASUREMENT"'


def test_version_guard_binds_the_event_for_push_metadata():
    step = _find_step(_job(_load("version-guard.yml"), "version-guard"), "version_guard.py")
    assert step["env"]["CLAUDIT_EVENT"] == "${{ github.event_name }}"
    assert 'if [ "$CLAUDIT_EVENT" = "push" ]; then' in _run_lines(step)


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
    """The commands above quote `"$CLAUDIT_MEASUREMENT"`, so steps
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
        and step.get("env", {}).get("CLAUDIT_MEASUREMENT") != "${{ inputs.measurement }}"
    ]
    assert not misbound, (
        "these run steps bind the measurement to anything other than the "
        f"action's measurement input: {misbound}")


def test_speed_yml_is_gone():
    # The fold: one bench pass per ci-gate run, in the
    # tests canary; a second workflow to run it again is a regression.
    assert not (WORKFLOWS / "speed.yml").exists()


def test_tests_job_measures_and_gates_on_every_event():
    doc = _load("tests.yml")
    job = _job(doc, "suite-bench")
    bench, = [step for step in _steps(job) if step.get("id") == "suite_bench"]
    assert "if" not in bench
    assert job["needs"] == ["pytest-shards", "pytest-thresholds", "js-coverage"]
    assert job["if"] == (
        "${{ !cancelled() && (needs.pytest-shards.result == 'success' "
        "|| needs.pytest-thresholds.result == 'success') "
        "&& (needs.js-coverage.result == 'success' "
        "|| inputs.thresholds_only == 'true') }}")
    # A fresh job is part of the workload: no earlier pytest session may
    # warm imports or collection before the bench action measures it.
    pytest_steps = [step for step in _steps(job)
                    if "pytest" in step.get("run", "")
                    or step.get("uses") in {"$/.github/actions/suite-bench",
                                            "$/.github/actions/coverage-gate"}]
    assert pytest_steps == [bench]
    upload, = [step for step in _steps(job)
               if step.get("with", {}).get("name") == "suite-measurement"]
    assert upload["if"] == (
        "${{ !cancelled() && steps.suite_bench.outcome != 'skipped' }}")
    assert "needs.suite-bench.result == 'success'" in _job(doc, "ratchet")["if"]


def test_ratchet_reads_job_outputs_and_stages_the_tested_tip():
    job = _job(_load("tests.yml"), "ratchet")
    assert job["needs"] == ["pytest-shards", "coverage-combine", "js-coverage",
                            "reparse-gate", "suite-bench"]
    assert job["if"] == (
        "${{ !cancelled() && github.event_name == 'push' && github.ref == 'refs/heads/master' "
        "&& needs.pytest-shards.result == 'success' && needs.coverage-combine.result == 'success' "
        "&& needs.js-coverage.result == 'success' && needs.reparse-gate.result == 'success' "
        "&& needs.suite-bench.result == 'success' }}")
    ratchet, = [step for step in _steps(job) if step.get("id") == "ratchet"]
    assert ratchet["env"] == {
        "CLAUDIT_JS_MEASURED": "${{ needs.js-coverage.outputs.js_measured }}",
        "CLAUDIT_PY_MEASURED": "${{ needs.coverage-combine.outputs.python_measured }}"}
    assert 'measured="$CLAUDIT_PY_MEASURED"' in _run_lines(ratchet)
    assert "coverage report" not in ratchet["run"]
    steps = _steps(job)
    node, = [step for step in steps if step.get("uses", "").startswith("actions/setup-node@")]
    npm, = [step for step in steps if step.get("run") == "npm ci --no-audit --no-fund"]
    pip = next(step for step in steps if step.get("name") == "Install dependencies")
    assert steps.index(pip) < steps.index(node) < steps.index(npm) < steps.index(ratchet)
    download, = [step for step in _steps(job)
                 if step.get("with", {}).get("name") == "suite-measurement"]
    assert "run-id" not in download["with"]
    stage, = [step for step in _steps(job) if step.get("name") == "Stage the ratchet data"]
    assert 'git rev-parse HEAD > "$RUNNER_TEMP/ratchet-data/base.txt"' in _run_lines(stage)
    assert ('cp .github/ci-thresholds.json "$RUNNER_TEMP/ratchet-data/ci-thresholds.json"'
            in _run_lines(stage))
    upload, = [step for step in _steps(job)
               if step.get("with", {}).get("name") == "ratchet-push"]
    assert stage["if"] == upload["if"] == "steps.ratchet.outputs.changed == 'true'"


def test_the_suite_measurement_upload_is_unique_repo_wide():
    # The bot's download side (ratchet-push.yml) names the artifacts.
    # Every downloaded name must have exactly one uploader across the
    # repository's workflows: two uploads of one name would make the
    # download ambiguous. The map pins the publishing workflow and job
    # for each name; a coordinated rename must also update the map.
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
    expected = {
        "comment-share-ratchet": "comment-share.yml:comment-share:comment-share-ratchet",
        "ratchet-push": "tests.yml:ratchet:ratchet-push",
        "size-ratchet-push": "tests.yml:size-baseline:size-ratchet-push",
        "suite-measurement": "tests.yml:suite-bench:suite-measurement"}
    assert download_names == sorted(expected)
    assert sorted(uploaders) == sorted(expected.values()), sorted(uploaders)


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
    # test_workflow_ci_gate).
    # The tighten is a DATA operation in the bot job: no checkout, no
    # pip, no test code anywhere in the job's own text.
    text = (WORKFLOWS / "ratchet-push.yml").read_text(encoding="utf-8")
    job_text = text[text.index("  push:"):]
    for banned in ("actions/checkout", "pip install", "pytest "):
        assert banned not in job_text, banned
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


def test_ratchet_push_skips_suite_measurements_when_thresholds_moved():
    job = _job(_load("ratchet-push.yml"), "push")
    lines = _run_lines(_find_step(job, 'suite_ratchet.py" --tighten'))
    initial = lines.index("suite_measurement_current=true")
    ancestor = lines.index(
        'if ! git -C "$RUNNER_TEMP/ratchet-repo" show '
        '"$base:.github/ci-thresholds.json" > "$RUNNER_TEMP/ratchet-base.json"; then')
    compare = lines.index(
        'elif ! cmp -s "$RUNNER_TEMP/ratchet-base.json" '
        '"$RUNNER_TEMP/ratchet-repo/.github/ci-thresholds.json"; then')
    merge = next(index for index, line in enumerate(lines) if "merge-file" in line)
    check = next(index for index, line in enumerate(lines)
                 if 'thresholds.py" --check' in line)
    guard = lines.index('if [ "$suite_measurement_current" = true ]; then')
    tighten = lines.index(TIGHTEN_CMD)
    assert initial < ancestor < compare < merge < check < guard < tighten
    unreadable = lines[ancestor + 1:compare]
    assert 'if [ -f "$RUNNER_TEMP/ratchet-data/ci-thresholds.json" ]; then' in unreadable
    assert "exit 0" in unreadable
    assert unreadable[-1] == "suite_measurement_current=false"
    assert lines[compare + 1] == "suite_measurement_current=false"
    skip = lines.index("else", tighten + 1)
    end = lines.index("fi", skip + 1)
    branch = lines[skip + 1:end]
    assert any("::notice::" in line for line in branch)
    assert any('>> "$GITHUB_STEP_SUMMARY"' in line for line in branch)
    assert any("master's thresholds moved while this run measured" in line
               for line in branch)
    assert not any(line.startswith("exit ") for line in branch)


def test_ratchet_push_validates_every_stale_path_before_tightening():
    job = _job(_load("ratchet-push.yml"), "push")
    lines = _run_lines(_find_step(job, 'suite_ratchet.py" --tighten'))
    check = lines.index(
        'if ! python3 "$RUNNER_TEMP/ratchet-repo/scripts/ci/thresholds.py" '
        '--check; then')
    copy = next(index for index, line in enumerate(lines) if line.startswith("cp "))
    assert copy < check < lines.index(TIGHTEN_CMD)
    assert lines[check - 3:check] == [
        "fi", "fi", 'if [ "$fetched" != "$base" ]; then']
    branch = lines[check + 1:lines.index("fi", check + 1)]
    assert any("::notice::" in line for line in branch)
    assert any('>> "$GITHUB_STEP_SUMMARY"' in line for line in branch)
    assert branch[-1] == "exit 0"


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


def test_the_bench_runner_sets_up_one_interpreter():
    doc = _load("tests.yml")
    for job_id, job in doc["jobs"].items():
        if job_id == "pytest-portable":
            continue
        setups = [step for step in _steps(job)
                  if (step.get("uses") or "").startswith("actions/setup-python")]
        # The size job runs no Python of its own: the action sets its
        # interpreter up itself.
        assert len(setups) == (0 if job_id == "size-baseline" else 1)


def test_records_base_reads_are_guarded_on_artifact_presence():
    job = _job(_load("ratchet-push.yml"), "push")
    lines = _run_lines(_find_step(job, 'suite_ratchet.py" --tighten'))
    guards = []
    reads = 0
    for line in lines:
        if line.startswith("if ") and line.endswith("; then"):
            guards.append(line)
        elif line.startswith("elif ") or line == "else":
            guards[-1] = line
        elif line == "fi":
            guards.pop()
        if 'cat "$RUNNER_TEMP/comment-share-data/base.txt"' in line:
            reads += 1
            assert any(guard in {
                'if [ -f "$RUNNER_TEMP/comment-share-data/base.txt" ]; then',
                'elif [ -f "$RUNNER_TEMP/comment-share-data/base.txt" ]; then'}
                for guard in guards)
    assert reads == 2, "base selection and records merge each read their guarded base"


@pytest.mark.parametrize("base_var", ["base", "records_base"])
def test_records_empty_base_is_a_broken_handoff(base_var):
    job = _job(_load("ratchet-push.yml"), "push")
    lines = _run_lines(_find_step(job, 'suite_ratchet.py" --tighten'))
    guard = lines.index(
        f'{"elif" if base_var == "base" else "if"} '
        '[ -f "$RUNNER_TEMP/comment-share-data/base.txt" ]; then')
    read = lines.index(f'{base_var}="$(cat "$RUNNER_TEMP/comment-share-data/base.txt")"')
    empty = lines.index(f'if [ -z "${base_var}" ]; then')
    end = lines.index("fi", empty + 1)
    assert lines[empty + 1:end] == [
        'echo "the comment-share data carried no base; the measuring job is broken" >&2',
        "exit 1"]
    assert guard < read < empty < end
    if base_var == "base":
        assert end < next(index for index, line in enumerate(lines)
                          if "ratchet_staleness.py" in line)


def test_current_master_records_deletion_requires_review():
    job = _job(_load("ratchet-push.yml"), "push")
    lines = _run_lines(_find_step(job, 'suite_ratchet.py" --tighten'))
    current = lines.index(
        'if ! git -C "$RUNNER_TEMP/ratchet-repo" show '
        '"$fetched:.github/comment-share.json" > "$current"; then')
    end = lines.index("fi", current + 1)
    assert lines[current + 1:end] == [
        'echo "Current master has no comment-share records; deletion requires review" >&2',
        "exit 1"]
    ancestor = lines.index(
        'if ! git -C "$RUNNER_TEMP/ratchet-repo" show '
        '"$records_base:.github/comment-share.json" > "$RUNNER_TEMP/base-records.json"; then')
    assert lines.index(TIGHTEN_CMD) < current < end < ancestor


@pytest.mark.parametrize(("command", "notice", "heading"), [
    ('if ! git -C "$RUNNER_TEMP/ratchet-repo" show '
     '"$records_base:.github/comment-share.json" > "$RUNNER_TEMP/base-records.json"; then',
     "Ancestor records are unreadable", "ancestor records could not be read"),
    ('elif ! git merge-file "$current" "$RUNNER_TEMP/base-records.json" '
     '"$RUNNER_TEMP/comment-share-data/comment-share.json"; then',
     "Records changes conflict", "records merge conflicted"),
    ('if ! python3 -I -m json.tool "$current" >/dev/null; then',
     "Merged records are invalid", "records failed JSON validation"),
])
def test_records_drop_branches_preserve_other_ratchets(command, notice, heading):
    job = _job(_load("ratchet-push.yml"), "push")
    lines = _run_lines(_find_step(job, 'suite_ratchet.py" --tighten'))
    start = lines.index(command)
    end = next(index for index in range(start + 1, len(lines))
               if lines[index] in {"else", "fi"} or lines[index].startswith("elif "))
    branch = lines[start + 1:end]
    assert any(line.startswith(f'echo "::notice::{notice};') for line in branch)
    assert f'echo "### Dropped: {heading}"' in branch
    assert ('echo "This run\'s records tightening is dropped; '
            'the next master push measures again."' in branch)
    assert '} >> "$GITHUB_STEP_SUMMARY"' in branch
    assert not any(line.startswith("exit ") for line in branch)
    assert "records_selected=true" not in branch
    if "json.tool" not in command:
        assert branch[-1] == "records_merge_ok=false"
    commit = next(index for index, line in enumerate(lines) if " commit -m " in line)
    assert lines.index(TIGHTEN_CMD) < start < end < commit


def test_successful_records_merge_is_validated_before_selection_and_staging():
    job = _job(_load("ratchet-push.yml"), "push")
    lines = _run_lines(_find_step(job, 'suite_ratchet.py" --tighten'))
    initial = lines.index("records_selected=false")
    stale = lines.index('if [ "$fetched" != "$records_base" ]; then')
    ancestor = next(index for index, line in enumerate(lines)
                    if 'show "$records_base:.github/comment-share.json"' in line)
    merge = next(index for index, line in enumerate(lines) if 'git merge-file "$current"' in line)
    assert lines[stale - 1] == "records_merge_ok=true"
    copy = lines.index('cp "$RUNNER_TEMP/comment-share-data/comment-share.json" "$current"')
    assert lines[copy - 1] == "else"
    assert lines[copy - 2] == "fi"
    ready = lines.index('if [ "$records_merge_ok" = true ]; then')
    validate = lines.index('if ! python3 -I -m json.tool "$current" >/dev/null; then')
    selected = lines.index("records_selected=true")
    assert lines[selected - 2:selected] == [
        "else", 'cp "$current" "$RUNNER_TEMP/ratchet-repo/.github/comment-share.json"']
    stage_thresholds = lines.index('git -C "$RUNNER_TEMP/ratchet-repo" add .github/ci-thresholds.json')
    stage_records = lines.index('git -C "$RUNNER_TEMP/ratchet-repo" add .github/comment-share.json')
    assert lines[stage_records - 1] == 'if [ "$records_selected" = true ]; then'
    assert lines[stage_records + 1] == "fi"
    assert (lines.index(TIGHTEN_CMD) < initial < stale < ancestor < merge
            < copy < ready < validate < selected < stage_thresholds < stage_records)
