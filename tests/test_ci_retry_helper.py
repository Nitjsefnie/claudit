"""scripts/ci/retry.sh against a stub command and a stub `sleep`: at most
three attempts, 5 then 10 s apart; only the succeeding attempt's stdout,
every attempt's stderr, and the last attempt's status on exhaustion."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "ci" / "retry.sh"
POSIX_ONLY = pytest.mark.skipif(
    os.name == "nt", reason="the helper runs under POSIX Bash")


def _run(tmp_path, fail_times):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    probe = bin_dir / "probe"
    # Attempt n exits 10 + n while n <= fail_times.
    probe.write_text(
        "#!/bin/sh\n"
        'printf "%s|" "$@" >> "$CALLS"\n'
        'echo >> "$CALLS"\n'
        'n="$(wc -l < "$CALLS")"\n'
        'echo "stdout of attempt $n"\n'
        'echo "stderr of attempt $n" >&2\n'
        f'if [ "$n" -le {fail_times} ]; then exit "$((10 + n))"; fi\n',
        encoding="utf-8", newline="\n")
    sleep = bin_dir / "sleep"
    sleep.write_text('#!/bin/sh\necho "$*" >> "$SLEEPS"\n',
                     encoding="utf-8", newline="\n")
    for stub in (probe, sleep):
        stub.chmod(0o755)
    calls, sleeps = tmp_path / "calls", tmp_path / "sleeps"
    env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
               CALLS=str(calls), SLEEPS=str(sleeps))
    proc = subprocess.run([str(HELPER), "probe", "--flag", "two words"],
                          cwd=tmp_path, env=env, check=False,
                          capture_output=True, text=True)
    attempts = calls.read_text(encoding="utf-8").splitlines()
    assert attempts and set(attempts) == {"--flag|two words|"}, attempts
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
