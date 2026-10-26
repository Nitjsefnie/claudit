"""The size gate's wiring: the size-baseline job and the bot's size arm.
Assert decoded scalars, executed step drives and artifact names so decoy
script text cannot satisfy the contract."""
from __future__ import annotations

import os
import subprocess

import pytest
from tests.workflow_checks import (find_step as _find_step, run_lines as _run_lines, steps_of as _steps, workflow_by_name as _load, job_of as _job, TIGHTEN_CMD, WORKFLOWS)


def test_size_baseline_stages_only_tightened_records():
    # Stages records plus the measured base only when tighten moved the file; no change, no upload.
    doc = _load("tests.yml")
    job = _job(doc, "size-baseline")
    stage, = [step for step in _steps(job) if step.get("id") == "size_stage"]
    lines = _run_lines(stage)
    assert lines[0] == "changed=0"
    assert 'git diff --quiet -- .github/size-baseline.json || changed=$?' in lines
    quiet = lines.index('git diff --quiet -- .github/size-baseline.json || changed=$?')
    unchanged = lines.index("if [ \"$changed\" -eq 0 ]; then")
    assert quiet < unchanged
    assert lines[unchanged + 1:unchanged + 3] == [
        "echo 'changed=false' >> \"$GITHUB_OUTPUT\"", "exit 0"]
    assert 'cp .github/size-baseline.json "$RUNNER_TEMP/size-ratchet/size-baseline.json"' in lines
    assert 'git rev-parse HEAD > "$RUNNER_TEMP/size-ratchet/base.txt"' in lines
    upload, = [step for step in _steps(job)
               if step.get("with", {}).get("name") == "size-ratchet-push"]
    assert "if" not in stage
    assert upload["if"] == "steps.size_stage.outputs.changed == 'true'"


@pytest.mark.skipif(os.name == 'nt', reason='the step runs under POSIX Bash')
def test_every_cross_run_download_names_the_triggering_run():
    # Without run-id a download step reads THIS workflow's run, never the
    # triggering run's: every workflow_run download takes the event's id.
    swept = 0
    for path in sorted(WORKFLOWS.glob("*.yml")):
        doc = _load(path.name)
        if "workflow_run" not in (doc.get("on") or {}):
            continue
        for job_id, job in (doc.get("jobs") or {}).items():
            for step in (job or {}).get("steps") or []:
                if not (step.get("uses") or "").startswith(
                        "actions/download-artifact"):
                    continue
                swept += 1
                assert (step.get("with") or {}).get("run-id") == (
                    "${{ github.event.workflow_run.id }}"), (path.name, job_id)
    assert swept, "no download-artifact step under any workflow_run trigger"


def test_records_only_runs_select_their_measured_base_before_fetching():
    job = _job(_load("ratchet-push.yml"), "push")
    lines = _run_lines(_find_step(job, 'suite_ratchet.py" --tighten'))
    ratchet = lines.index('if [ -f "$RUNNER_TEMP/ratchet-data/base.txt" ]; then')
    suite = lines.index('elif [ -f "$RUNNER_TEMP/suite-data/suite-measurement.json" ]; then')
    records = lines.index('elif [ -f "$RUNNER_TEMP/comment-share-data/base.txt" ]; then')
    assert lines[records + 1] == 'base="$(cat "$RUNNER_TEMP/comment-share-data/base.txt")"'
    end = lines.index("else", records + 1)
    assert lines[end:end + 4] == [
        "else",
        'echo "no ratchet data, suite measurement, comment-share or size records to push" >&2',
        "exit 1", "fi"]
    fetch = lines.index('git -C "$RUNNER_TEMP/ratchet-repo" fetch --quiet origin master')
    size = lines.index('elif [ -f "$RUNNER_TEMP/size-data/base.txt" ]; then')
    assert lines[size + 1] == 'base="$(cat "$RUNNER_TEMP/size-data/base.txt")"'
    assert lines[size + 2:size + 5] == [
        'if [ -z "$base" ]; then',
        'echo "the size data carried no base; the measuring job is broken" >&2',
        "exit 1"]
    assert lines[size + 5] == "fi"
    assert ratchet < suite < records < size < fetch


def test_size_empty_base_refused_in_chain_and_landing(tmp_path):
    # Drive the real push step: the chain check covers a size-only run
    # and the landing guard a multi-artifact run whose earlier arm
    # resolved; an empty staged base dies with the message either way.
    step = _find_step(_job(_load("ratchet-push.yml"), "push"),
                      'suite_ratchet.py" --tighten')
    fakes = tmp_path / "bin"
    fakes.mkdir()
    for name, body in [
        ("ssh-agent", "#!/bin/sh\nexit 0\n"),
        ("ssh-add", "#!/bin/sh\nexit 0\n"),
        ("git", "#!/bin/sh\nexit 0\n"),
        ("python3", "#!/bin/sh\nexit 0\n"),
    ]:
        fake = fakes / name
        fake.write_text(body)
        fake.chmod(0o755)
    env = dict(os.environ, PATH=f"{fakes}{os.pathsep}{os.environ['PATH']}",
               HOME=str(tmp_path), CLAUDIT_MASTER_PUSH_DEPLOY_KEY="key",
               CLAUDIT_REPO="o/r", CLAUDIT_BASE_FALLBACK="f" * 40)
    runner = tmp_path / "runner"
    (runner / "size-data").mkdir(parents=True)
    # The faked git runs no checkout; stand in the scratch tree it would have had.
    (runner / "size-data" / "base.txt").write_text("")
    (runner / "ratchet-repo" / ".github").mkdir(parents=True)
    size_base_txt = runner / "size-data" / "base.txt"

    def _drive():
        return subprocess.run(["bash", "-e", "-c", step["run"]], env=env,
                              capture_output=True, text=True, check=False,
                              timeout=60)

    # Size-only run, empty base: the chain's size elif resolves and refuses here.
    env["RUNNER_TEMP"] = str(runner)
    result = _drive()
    assert result.returncode != 0
    assert ("the size data carried no base; the measuring job is broken"
            in result.stderr)
    assert "Master moved while this run measured" not in result.stderr
    # Multi-artifact run: an earlier arm resolved, so the landing guard
    # refuses the same empty file the chain never saw.
    (runner / "ratchet-data").mkdir()
    (runner / "ratchet-data" / "base.txt").write_text("b" * 40 + "\n")
    result = _drive()
    assert result.returncode != 0
    assert ("the size data carried no base; the measuring job is broken"
            in result.stderr)
    assert "Master moved while this run measured" not in result.stderr
    assert "nothing to commit" not in result.stdout
    # Healthy multi-artifact shape: with every tool faked the run reaches
    # the no-change ending, so neither check is trip-happy.
    size_base_txt.write_text("a" * 40 + "\n")
    result = _drive()
    assert result.returncode == 0, result.stderr
    assert ("No ratchet move is justified by this run: nothing to commit."
            in result.stdout)
    assert "carried no base" not in result.stderr


def test_size_records_landing_reads_the_measured_base():
    job = _job(_load("ratchet-push.yml"), "push")
    lines = _run_lines(_find_step(job, 'suite_ratchet.py" --tighten'))
    guard = lines.index('if [ -f "$RUNNER_TEMP/size-data/base.txt" ]; then')
    read = lines.index('size_base="$(cat "$RUNNER_TEMP/size-data/base.txt")"')
    # The landing guard admits an empty file on multi-artifact runs, where
    # the chain's size elif never evaluated: the arm refuses its own here.
    assert guard < read
    empty = lines.index('if [ -z "$size_base" ]; then')
    empty_end = lines.index("fi", empty + 1)
    assert lines[empty + 1:empty_end] == [
        'echo "the size data carried no base; the measuring job is broken" >&2',
        "exit 1"]
    assert read < empty < empty_end
    # Adoption commits the seed: a missing file on current master is a
    # deletion requiring review, never permission to auto-create one.
    current = lines.index(
        'if ! git -C "$RUNNER_TEMP/ratchet-repo" show '
        '"$fetched:.github/size-baseline.json" > "$current"; then')
    current_end = lines.index("fi", current + 1)
    assert lines[current + 1:current_end] == [
        'echo "Current master has no size-baseline records; deletion requires review" >&2',
        "exit 1"]
    assert lines.index(TIGHTEN_CMD) < guard < empty < current < current_end


def test_size_records_stale_measurement_drops_the_run():
    # A measurement whose base predates any non-records change on master
    # could record below the file's real count (tighten never raises):
    # the whole run drops for the next master push to measure again.
    job = _job(_load("ratchet-push.yml"), "push")
    lines = _run_lines(_find_step(job, 'suite_ratchet.py" --tighten'))
    stale = lines.index('if [ "$fetched" != "$size_base" ]; then')
    moved = lines.index(
        'if [ -n "$(git -C "$RUNNER_TEMP/ratchet-repo" diff --name-only '
        '"$size_base" "$fetched" '
        "-- ':!.github/size-baseline.json' ':!.github/comment-share.json' "
        "':!.github/ci-thresholds.json')\" ]; then")
    assert stale < moved
    dropped = lines.index("fi", moved + 1)
    branch = lines[moved + 1:dropped]
    assert ("echo '::notice::Master moved under the measurement; "
            "the next master push measures again'") in branch
    assert "echo '### Dropped: master moved under the measurement'" in branch
    assert '} >> "$GITHUB_STEP_SUMMARY"' in branch
    assert branch[-1] == "exit 0"


@pytest.mark.parametrize(("command", "notice", "heading"), [
    ('if ! git -C "$RUNNER_TEMP/ratchet-repo" show '
     '"$size_base:.github/size-baseline.json" > "$RUNNER_TEMP/base-size.json"; then',
     "Ancestor size records are unreadable", "ancestor size records could not be read"),
    ('elif ! git merge-file "$current" "$RUNNER_TEMP/base-size.json" '
     '"$RUNNER_TEMP/size-data/size-baseline.json"; then',
     "Size records changes conflict", "size records merge conflicted"),
    ('if ! python3 -I -m json.tool "$current" >/dev/null; then',
     "Merged size records are invalid", "size records failed JSON validation"),
])
def test_size_records_drop_branches_preserve_other_ratchets(command, notice, heading):
    job = _job(_load("ratchet-push.yml"), "push")
    lines = _run_lines(_find_step(job, 'suite_ratchet.py" --tighten'))
    # json.tool recurs in the comment-share arm; search from the size guard.
    size_guard = lines.index('if [ -f "$RUNNER_TEMP/size-data/base.txt" ]; then')
    start = lines.index(command, size_guard)
    end = next(index for index in range(start + 1, len(lines))
               if lines[index] in {"else", "fi"} or lines[index].startswith("elif "))
    branch = lines[start + 1:end]
    assert any(line.startswith(f'echo "::notice::{notice};') for line in branch)
    assert f'echo "### Dropped: {heading}"' in branch
    assert ('echo "This run\'s size tightening is dropped; '
            'the next master push measures again."' in branch)
    assert '} >> "$GITHUB_STEP_SUMMARY"' in branch
    assert not any(line.startswith("exit ") for line in branch)
    assert "size_selected=true" not in branch
    if "json.tool" not in command:
        assert branch[-1] == "size_merge_ok=false"
    commit = next(index for index, line in enumerate(lines) if " commit -m " in line)
    assert lines.index(TIGHTEN_CMD) < start < end < commit


def test_size_records_success_is_validated_before_selection_and_staging():
    job = _job(_load("ratchet-push.yml"), "push")
    lines = _run_lines(_find_step(job, 'suite_ratchet.py" --tighten'))
    initial = lines.index("size_selected=false")
    stale = lines.index('if [ "$fetched" != "$size_base" ]; then')
    ancestor = next(index for index, line in enumerate(lines)
                    if 'show "$size_base:.github/size-baseline.json"' in line)
    merge = next(index for index, line in enumerate(lines) if 'git merge-file "$current" "$RUNNER_TEMP/base-size.json"' in line)
    assert lines[stale - 1] == "size_merge_ok=true"
    copy = lines.index('cp "$RUNNER_TEMP/size-data/size-baseline.json" "$current"')
    assert lines[copy - 1] == "else"
    assert lines[copy - 2] == "fi"
    ready = lines.index('if [ "$size_merge_ok" = true ]; then')
    validate = lines.index('if ! python3 -I -m json.tool "$current" >/dev/null; then',
                           ready)
    selected = lines.index("size_selected=true")
    assert lines[selected - 2:selected] == [
        "else", 'cp "$current" "$RUNNER_TEMP/ratchet-repo/.github/size-baseline.json"']
    stage_thresholds = lines.index('git -C "$RUNNER_TEMP/ratchet-repo" add .github/ci-thresholds.json')
    stage_records = lines.index('git -C "$RUNNER_TEMP/ratchet-repo" add .github/comment-share.json')
    stage_size = lines.index('git -C "$RUNNER_TEMP/ratchet-repo" add .github/size-baseline.json')
    assert lines[stage_size - 1] == 'if [ "$size_selected" = true ]; then'
    assert lines[stage_size + 1] == "fi"
    assert (lines.index(TIGHTEN_CMD) < initial < stale < ancestor < merge
            < copy < ready < validate < selected
            < stage_thresholds < stage_records < stage_size)


def test_records_selection_controls_commit_message_and_all_files_are_checked():
    job = _job(_load("ratchet-push.yml"), "push")
    lines = _run_lines(_find_step(job, 'suite_ratchet.py" --tighten'))
    guard = lines.index(
        'if [ -z "$(git -C "$RUNNER_TEMP/ratchet-repo" status --porcelain '
        '.github/ci-thresholds.json .github/comment-share.json '
        '.github/size-baseline.json)" ]; then')
    # One message composed from what landed; the default names no records file.
    default = lines.index(
        "message='Ratchet ci-thresholds: raise coverage floor / tighten "
        "baselines (automated)'")
    assert lines[default - 2:default] == [
        'message="chore(ratchet): tighten $subjects records (automated)"',
        "else"]
    assert lines[default + 1:default + 3] == [
        "fi", 'git -C "$RUNNER_TEMP/ratchet-repo" commit -m "$message"']
    comment_share = lines.index('subjects="${subjects:+$subjects and }comment-share"')
    size = lines.index('subjects="${subjects:+$subjects and }size-baseline"')
    assert comment_share < size < default - 2
    assert lines.index("records_selected=true") < lines.index("size_selected=true") < guard
