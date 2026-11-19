"""scripts/ci/progress_watchdog.py against real children, each ending on its own within 10 s:
one that keeps writing is never stopped; one that stops is, even if it ignores SIGTERM."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

WATCHDOG = Path(__file__).resolve().parents[1] / "scripts" / "ci" / "progress_watchdog.py"
pytestmark = pytest.mark.skipif(os.name == "nt", reason="process groups are POSIX")
# argv: the watched directory, a pid file, flags, then the seconds before each write two levels below the directory.
CHILD = r"""
import os, signal, subprocess, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN) if "ignore" in sys.argv else None
open(sys.argv[2], "w").write(str(os.getpid()))
if "group" in sys.argv:  # a grandchild deaf to SIGTERM: only the group's SIGKILL ends it
    code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(10)"
    open(sys.argv[2] + ".group", "w").write(str(subprocess.Popen([sys.executable, "-c", code]).pid))
for at, gap in enumerate(float(arg) for arg in sys.argv[3:] if arg not in ("ignore", "group")):
    time.sleep(gap)
    open(os.path.join(sys.argv[1], "archives", "partial", "f"), "w").write("x" * (2 - at % 2))  # grows, then shrinks
    open(sys.argv[2] + ".at", "w").write(repr(time.monotonic()))
time.sleep(10)
"""


def _run(tmp_path: Path, stall: str, *child: str, code: str = CHILD) -> tuple[subprocess.CompletedProcess, int, float]:
    """(run, child pid, seconds from the child's last write to the watchdog's exit)."""
    watched, pid, last = tmp_path / "watched", tmp_path / "pid", tmp_path / "pid.at"
    (watched / "archives" / "partial").mkdir(parents=True)
    proc = subprocess.run([str(WATCHDOG), "--stall", stall, "--sample", "0.1", "--grace", "0.3", "--watch", str(watched),
                           "--", sys.executable, "-c", code, str(watched), str(pid), *child],
                          capture_output=True, text=True, check=False, timeout=30)
    after = time.monotonic() - float(last.read_text(encoding="utf-8")) if last.exists() else 0.0
    return proc, int(pid.read_text(encoding="utf-8")), after


def _gone(pid: int) -> bool:
    for _ in range(40):  # a killed orphan lingers until its reaper waits on it
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def test_a_child_that_keeps_writing_is_never_stopped(tmp_path):
    # Each 0.9 s gap stays under the 1.5 s window by more than a sample, and the four outlast it.
    proc, _, _ = _run(tmp_path, "1.5", "0.9", "0.9", "0.9", "0.9", code=CHILD.replace("time.sleep(10)", "sys.exit(7)"))
    assert (proc.returncode, proc.stderr, (tmp_path / "watched/archives/partial/f").read_text("utf-8")) == (7, "", "x")


@pytest.mark.parametrize("ignore", [False, True], ids=["obeys-term", "ignores-term"])
def test_a_child_that_stops_writing_is_stopped_with_its_group_and_reported(tmp_path, ignore):
    proc, pid, after = _run(tmp_path, "1", "group", "0", *["ignore"] * ignore)
    assert proc.returncode == 124
    assert proc.stderr.endswith(f"wrote nothing to {tmp_path / 'watched'} for 1 s; stopped it\n"), proc.stderr
    assert 1 <= after <= 1 + 0.1 + 0.3 * ignore + 1, after
    assert _gone(pid) and _gone(int((tmp_path / "pid.group").read_text(encoding="utf-8")))


def test_a_child_that_fails_on_its_own_reports_its_status(tmp_path):
    proc, _, _ = _run(tmp_path, "5", code=CHILD.replace("time.sleep(10)", "os.kill(os.getpid(), 9)"))
    assert (proc.returncode, proc.stderr) == (137, "")


def test_a_signal_to_the_watchdog_stops_the_child(tmp_path):
    pid = tmp_path / "pid"
    with subprocess.Popen([str(WATCHDOG), "--stall", "30", "--sample", "0.1", "--grace", "0.3", "--",
                           sys.executable, "-c", CHILD, str(tmp_path / "watched"), str(pid)]) as watchdog:
        for _ in range(200):  # the child writes its pid after the watchdog installed its handlers
            if pid.exists() and pid.read_text(encoding="utf-8"):
                break
            time.sleep(0.05)
        watchdog.send_signal(signal.SIGTERM)
        assert watchdog.wait(10) == 128 + signal.SIGTERM
    assert _gone(int(pid.read_text(encoding="utf-8")))
