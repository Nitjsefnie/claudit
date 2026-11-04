"""The node runner budgets CPU with a wall ceiling, or wall time without /proc."""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, call

import pytest

from tests import node_run
from tests.node_run import _cpu_seconds

needs_proc = pytest.mark.skipif(not os.path.exists("/proc/self/stat"), reason="needs /proc/<pid>/stat")
KILL_NOTE = re.compile(r"killed on the (.+) after (\S+) s wall and (\S+) s CPU")


@pytest.fixture(name="without_proc")
def _without_proc(monkeypatch):
    monkeypatch.setattr(node_run, "PROC_STAT_READABLE", False)


def _kill_note(error: BaseException) -> tuple[str, float, float]:
    matches = [KILL_NOTE.fullmatch(note) for note in getattr(error, "__notes__", [])]
    [match] = [match for match in matches if match]
    return match[1], float(match[2]), float(match[3])


def test_one_timeout_retries_with_the_same_argv_input_bound_and_kwargs(monkeypatch, without_proc):
    argv = ("node", "-e", "process.stdout.write('ok')")
    result = subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")
    run = Mock(side_effect=[subprocess.TimeoutExpired(argv, 120), result])
    monkeypatch.setattr(node_run.subprocess, "run", run)
    kwargs = {
        "capture_output": True, "check": True, "text": True,
        "encoding": "utf-8", "errors": "strict", "cwd": "work",
        "env": {"NODE_ENV": "test"},
    }

    assert node_run.run_node(argv, input="source", timeout=120, **kwargs) is result

    assert run.call_args_list == [
        call(argv, input="source", timeout=120, **kwargs),
        call(argv, input="source", timeout=120, **kwargs),
    ]


def test_two_timeouts_propagate_after_two_calls_at_the_default_bound(monkeypatch, without_proc):
    argv = ("node",)
    first = subprocess.TimeoutExpired(argv, 60)
    second = subprocess.TimeoutExpired(argv, 60, output=b"partial", stderr=b"HEAD" + b"x" * 5000 + b"TAIL")
    run = Mock(side_effect=[first, second])
    monkeypatch.setattr(node_run.subprocess, "run", run)

    with pytest.raises(subprocess.TimeoutExpired) as raised:
        node_run.run_node(argv)

    assert raised.value is second
    assert run.call_args_list == [
        call(argv, input=None, timeout=60.0),
        call(argv, input=None, timeout=60.0),
    ]
    assert "killed on the wall timeout of 60.0 s after" in raised.value.__notes__[-3]
    stdout_note, stderr_note = raised.value.__notes__[-2:]
    assert stdout_note == "node stdout tail:\npartial"
    assert stderr_note.startswith("node stderr tail:\nxx") and stderr_note.endswith("xTAIL") and len(stderr_note) < 5000


@pytest.mark.parametrize("failure", ["returncode", "called-process-error", "os-error"])
def test_non_timeout_failures_are_not_retried(monkeypatch, without_proc, failure):
    argv = ("node", "--check", "-")
    result = subprocess.CompletedProcess(argv, 7, stdout=b"", stderr=b"bad script")
    error = (subprocess.CalledProcessError(7, argv)
             if failure == "called-process-error" else FileNotFoundError("node"))
    run = Mock(return_value=result, side_effect=None if failure == "returncode" else error)
    monkeypatch.setattr(node_run.subprocess, "run", run)
    check = failure == "called-process-error"

    if failure == "returncode":
        assert node_run.run_node(argv, input=b"source", check=check) is result
    else:
        with pytest.raises(type(error)) as raised:
            node_run.run_node(argv, input=b"source", check=check)
        assert raised.value is error

    run.assert_called_once_with(argv, input=b"source", timeout=60.0, check=check)


@needs_proc
def test_a_blocked_child_outlives_its_cpu_budget_in_wall_time():
    """Sleeping costs no CPU, so three seconds of wall under a one-second budget is not a hang."""
    done = node_run.run_node([sys.executable, "-c", "import time; time.sleep(3)"], timeout=1, capture_output=True)
    assert done.returncode == 0


@needs_proc
@pytest.mark.parametrize("spin", ["while True: pass", "zero = open('/dev/zero', 'rb')\nwhile True: zero.read(1 << 20)"],
                         ids=["user", "system"])
def test_a_spinning_child_is_killed_on_its_cpu_budget_with_evidence(monkeypatch, request, spin):
    monkeypatch.setattr(node_run, "POLL_SECONDS", 0.05)
    script = "import sys; print('started', flush=True); sys.stderr.write('warming\\n'); sys.stderr.flush()\n" + spin
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        node_run.run_node([sys.executable, "-c", script], timeout=0.5, capture_output=True)
    bound, _, cpu = _kill_note(raised.value)
    assert bound == "CPU budget of 0.5 s" and 0.5 <= cpu < 0.75
    assert f"stalled test: {request.node.nodeid} (call)" in raised.value.__notes__
    assert b"started" in raised.value.output and b"warming" in (raised.value.stderr or b"")
    assert "node stdout tail:\nstarted\n" in raised.value.__notes__
    assert "node stderr tail:\nwarming\n" in raised.value.__notes__


@needs_proc
def test_a_child_asleep_past_the_wall_ceiling_is_killed(monkeypatch):
    monkeypatch.setattr(node_run, "WALL_CEILING_MULTIPLE", 2)
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        node_run.run_node([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5)
    bound, _, cpu = _kill_note(raised.value)
    assert bound == "wall ceiling of 1.0 s" and cpu < 0.5


@needs_proc
def test_wall_ceiling_in_note_is_the_ceiling_that_fires(monkeypatch):
    monkeypatch.setattr(node_run, "WALL_CEILING_MULTIPLE", 2)
    monkeypatch.setattr(node_run, "time", Mock(monotonic=Mock(side_effect=[0.0, 0.75, 1.5, 2.5])))
    monkeypatch.setattr(node_run, "_cpu_seconds", Mock(side_effect=[0.1, 0.2, 1.0]))
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        node_run.run_node([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5)
    bound, _, cpu = _kill_note(raised.value)
    assert bound == "wall ceiling of 1.0 s"
    assert cpu == 0.2


@needs_proc
def test_the_shipped_wall_ceiling_kills_and_a_held_pipe_keeps_the_partial_output(tmp_path):
    """At the shipped multiple a 0.2 s budget's ceiling is 1.0 s; the grandchild keeps the pipes open past the kill."""
    marker = tmp_path / "finished"
    grandchild = "import pathlib, sys, time; time.sleep(5); pathlib.Path(sys.argv[1]).touch()"
    script = ("import subprocess, sys, time\n"
              f"held = subprocess.Popen([sys.executable, '-S', '-c', {grandchild!r}, sys.argv[1]])\n"
              "print(held.pid, flush=True); time.sleep(3)")
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        node_run.run_node([sys.executable, "-S", "-c", script, str(marker)], timeout=0.2, capture_output=True)
    try:
        assert not marker.exists(), "drain waited for the grandchild to finish"
    finally:
        if not marker.exists():
            try:
                os.kill(int(raised.value.output), signal.SIGKILL)
            except ProcessLookupError:
                pass
    bound, _, _ = _kill_note(raised.value)
    assert bound == "wall ceiling of 1.0 s"


@needs_proc
def test_cpu_stat_command_name_can_contain_a_closing_parenthesis(monkeypatch):
    stat = b"123 (worker) name) R 1 2 3 4 5 6 7 8 9 10 100 200"
    monkeypatch.setattr(Path, "read_bytes", lambda _path: stat)
    assert _cpu_seconds(123) == 300 / os.sysconf("SC_CLK_TCK")


@needs_proc
def test_an_error_while_polling_kills_the_child(monkeypatch, tmp_path):
    def unreadable_stat(pid):
        raise OSError(f"stat of {pid} unreadable")
    monkeypatch.setattr(node_run, "_cpu_seconds", unreadable_stat)
    marker = tmp_path / "survived"
    script = "import pathlib, sys, time; time.sleep(3); pathlib.Path(sys.argv[1]).write_text('')"
    with pytest.raises(OSError, match="unreadable"):
        node_run.run_node([sys.executable, "-S", "-c", script, str(marker)], timeout=5)
    assert not marker.exists()


@needs_proc
def test_an_interrupt_while_polling_kills_and_reaps_the_child(monkeypatch):
    communicate = subprocess.Popen.communicate
    processes = []

    def interrupted_communicate(process, *args, **kwargs):
        processes.append(process)
        signal.setitimer(signal.ITIMER_REAL, 0.05)
        try:
            return communicate(process, *args, **kwargs)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)

    def interrupt(_signum, _frame):
        raise KeyboardInterrupt("poll interrupted")

    monkeypatch.setattr(subprocess.Popen, "communicate", interrupted_communicate)
    saved_timer = signal.setitimer(signal.ITIMER_REAL, 0)
    saved_handler = signal.signal(signal.SIGALRM, interrupt)
    try:
        with pytest.raises(KeyboardInterrupt, match="poll interrupted"):
            node_run.run_node([sys.executable, "-c", "import time; time.sleep(30)"], timeout=5)
        process = processes[0]
        assert process.returncode == -signal.SIGKILL
        with pytest.raises(ChildProcessError):
            os.waitpid(process.pid, os.WNOHANG)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait()
        signal.signal(signal.SIGALRM, saved_handler)
        signal.setitimer(signal.ITIMER_REAL, *saved_timer)


@needs_proc
@pytest.mark.parametrize(("check", "exit_code"), [(False, 3), (True, 3), (True, 0)])
def test_input_capture_text_and_check_behave_as_subprocess_run(monkeypatch, check, exit_code):
    """The child reads stdin only after several poll slices, so the input outlives the first slice."""
    monkeypatch.setattr(node_run, "POLL_SECONDS", 0.05)
    script = ("import sys, time; time.sleep(0.3); data = sys.stdin.read()\n"
              f"print(len(data)); sys.stderr.write(data[:3]); sys.exit({exit_code})")
    argv = [sys.executable, "-c", script]
    kwargs = {"input": "abc" * 100_000, "capture_output": True, "text": True, "check": check}
    outcomes = []
    for runner in (subprocess.run, node_run.run_node):
        try:
            done = runner(argv, **kwargs)  # pylint: disable=W1510
            outcomes.append((type(done), done.args, done.returncode, done.stdout, done.stderr))
        except subprocess.CalledProcessError as error:
            outcomes.append((type(error), error.cmd, error.returncode, error.output, error.stderr))
    assert outcomes[0] == outcomes[1]
    assert outcomes[1][2:] == (exit_code, "300000\n", "abc")


@needs_proc
def test_the_stall_debug_flag_alone_reads_wchan_stack_and_pressure(monkeypatch):
    monkeypatch.setattr(node_run, "WALL_CEILING_MULTIPLE", 2)
    monkeypatch.setattr(node_run, "STALL_DEBUG_FILES", (*node_run.STALL_DEBUG_FILES, "/proc/{pid}/absent"))
    read_text = vars(Path)["read_text"]
    seen: list[str] = []

    def recording_read_text(path, *args, **kwargs):
        seen.append(str(path))
        return read_text(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", recording_read_text)
    debug_file = re.compile(r"/proc/(\d+/(wchan|stack|absent)|pressure/(io|cpu|memory))")
    for flag in (None, "", "1"):
        if flag is None:
            monkeypatch.delenv("CLAUDIT_DEBUG_NODE_STALL", raising=False)
        else:
            monkeypatch.setenv("CLAUDIT_DEBUG_NODE_STALL", flag)
        seen.clear()
        with pytest.raises(subprocess.TimeoutExpired) as raised:
            node_run.run_node([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.25)
        noted = [note.partition(":")[0] for note in raised.value.__notes__ if debug_file.match(note)]
        read = [path for path in seen if debug_file.fullmatch(path)]
        if flag:
            assert [re.sub(r"\d+", "N", path) for path in noted] == [
                "/proc/N/wchan", "/proc/N/stack", "/proc/pressure/io", "/proc/pressure/cpu", "/proc/pressure/memory",
                "/proc/N/absent"]
            assert read == noted
            assert "unreadable" not in next(note for note in raised.value.__notes__ if "/wchan" in note)
            assert ": unreadable: " in next(note for note in raised.value.__notes__ if "/absent" in note)
        else:
            assert not noted and not read
