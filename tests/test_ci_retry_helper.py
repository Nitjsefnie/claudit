"""scripts/ci/retry.sh against a stub command and a stub `sleep`: at most
three attempts, 5 then 10 s apart; only the succeeding attempt's stdout,
every attempt's stderr, and the last attempt's status on exhaustion. Its
temporary file is removed, and one it cannot create stops it before the
command runs. Every copy a workflow job writes is this file, before use."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.workflow_checks import (WORKFLOWS, load_workflow, retry_helper_writers,
                                   run_retry_helper_writer)

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "ci" / "retry.sh"
POSIX_ONLY = pytest.mark.skipif(
    os.name == "nt", reason="the helper runs under POSIX Bash")


def _run(tmp_path, fail_times, mktemp_fails=False):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    probe = bin_dir / "probe"
    # Attempt n exits 10 + n while n <= fail_times.
    probe.write_text(
        "#!/bin/sh\n"
        'printf "%s|" "$@" >> "$CALLS"\n'
        'echo >> "$CALLS"\n'
        'n=$(($(wc -l < "$CALLS")))\n'
        'ls "$TMPDIR" >> "$SEEN"\n'
        'echo "stdout of attempt $n"\n'
        'echo "stderr of attempt $n" >&2\n'
        f'if [ "$n" -le {fail_times} ]; then exit "$((10 + n))"; fi\n',
        encoding="utf-8", newline="\n")
    sleep = bin_dir / "sleep"
    sleep.write_text('#!/bin/sh\necho "$*" >> "$SLEEPS"\n',
                     encoding="utf-8", newline="\n")
    if mktemp_fails:
        (bin_dir / "mktemp").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8", newline="\n")
        (bin_dir / "mktemp").chmod(0o755)
    for stub in (probe, sleep):
        stub.chmod(0o755)
    calls, sleeps, scratch = tmp_path / "calls", tmp_path / "sleeps", tmp_path / "t"
    seen = tmp_path / "seen"
    scratch.mkdir()
    env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
               CALLS=str(calls), SLEEPS=str(sleeps), SEEN=str(seen), TMPDIR=str(scratch))
    proc = subprocess.run([str(HELPER), "probe", "--flag", "two words"],
                          cwd=tmp_path, env=env, check=False,
                          capture_output=True, text=True)
    assert not list(scratch.iterdir()), "the helper left its temporary file"
    attempts = calls.read_text(encoding="utf-8").splitlines() \
        if calls.exists() else []
    # While the command ran, its stdout buffer sat in $TMPDIR.
    assert len(seen.read_text(encoding="utf-8").split() if seen.exists() else []) == len(attempts)
    assert set(attempts) <= {"--flag|two words|"}, attempts
    waits = sleeps.read_text(encoding="utf-8").split() \
        if sleeps.exists() else []
    return proc, len(attempts), waits


@POSIX_ONLY
@pytest.mark.parametrize("fail_times, attempts, waits", [
    (0, 1, []), (1, 2, ["5"]), (2, 3, ["5", "10"]),
], ids=["first-try", "one-failure", "two-failures"])
def test_success_emits_only_the_succeeding_attempts_stdout(
        tmp_path, fail_times, attempts, waits):
    proc, made, slept = _run(tmp_path, fail_times)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == f"stdout of attempt {attempts}\n"
    assert made == attempts
    assert slept == waits
    for n in range(1, attempts + 1):
        assert f"stderr of attempt {n}" in proc.stderr


@POSIX_ONLY
def test_exhaustion_exits_with_the_last_attempts_status(tmp_path):
    proc, made, slept = _run(tmp_path, fail_times=99)
    assert proc.returncode == 13
    assert proc.stdout == ""
    assert made == 3
    assert slept == ["5", "10"]
    for n in (1, 2, 3):
        assert f"stderr of attempt {n}" in proc.stderr


@POSIX_ONLY
def test_an_uncreatable_temporary_file_stops_before_the_command(tmp_path):
    proc, made, slept = _run(tmp_path, fail_times=0, mktemp_fails=True)
    assert proc.returncode != 0
    assert made == 0
    assert slept == []


@POSIX_ONLY
def test_every_embedded_copy_is_the_helper_and_written_before_use(tmp_path):
    copies = callers = 0
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        for job_id, job in (load_workflow(path).get("jobs") or {}).items():
            steps = job.get("steps") or []
            writers = retry_helper_writers(job) if steps else []
            for number, step in enumerate(writers):
                runner = tmp_path / f"{path.stem}-{job_id}-{number}"
                runner.mkdir()
                run_retry_helper_writer(step, runner)
                assert (runner / "retry.sh").read_bytes() == HELPER.read_bytes(), (path.name, job_id)
                assert os.access(runner / "retry.sh", os.X_OK), (path.name, job_id)
                copies += 1
            uses = [index for index, step in enumerate(steps) if step not in writers
                    and re.search(r"\$\{?RUNNER_TEMP\}?/retry\.sh", step.get("run") or "")]
            if uses:
                callers += 1
                assert writers and steps.index(writers[0]) < uses[0], (path.name, job_id)
    assert copies and callers, "no embedded copy or caller was found; the walk is broken"
