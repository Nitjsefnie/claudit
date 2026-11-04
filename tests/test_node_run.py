"""The node runner kills a child on its own CPU time, never on a slow runner's wall time."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, call

import pytest

from tests import node_run

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
    second = subprocess.TimeoutExpired(argv, 60, output=b"partial", stderr=b"warn")
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
    assert raised.value.__notes__[-2:] == ["node stdout tail:\npartial", "node stderr tail:\nwarn"]


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
def test_a_spinning_child_is_killed_on_its_cpu_budget_with_evidence(request):
    script = ("import sys; print('started', flush=True); sys.stderr.write('warming\\n'); sys.stderr.flush()\n"
              "while True: pass")
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        node_run.run_node([sys.executable, "-c", script], timeout=0.3, capture_output=True)
    bound, _, cpu = _kill_note(raised.value)
    assert bound == "CPU budget of 0.3 s" and cpu >= 0.3
    assert f"stalled test: {request.node.nodeid} (call)" in raised.value.__notes__
    assert b"started" in raised.value.output and b"warming" in (raised.value.stderr or b"")
    assert "node stdout tail:\nstarted\n" in raised.value.__notes__
    assert "node stderr tail:\nwarming\n" in raised.value.__notes__


@needs_proc
def test_a_child_asleep_past_the_wall_ceiling_is_killed(monkeypatch):
    monkeypatch.setattr(node_run, "WALL_CEILING_MULTIPLE", 2)
    with pytest.raises(subprocess.TimeoutExpired) as raised:
        node_run.run_node([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.5)
    bound, wall, cpu = _kill_note(raised.value)
    assert bound == "wall ceiling of 1.0 s" and wall >= 1.0 and cpu < 0.5


@needs_proc
@pytest.mark.parametrize("check", [False, True])
def test_input_capture_text_and_check_behave_as_subprocess_run(monkeypatch, check):
    """The child reads stdin only after several poll slices, so the input outlives the first slice."""
    monkeypatch.setattr(node_run, "POLL_SECONDS", 0.05)
    script = ("import sys, time; time.sleep(0.3); data = sys.stdin.read()\n"
              "print(len(data)); sys.stderr.write(data[:3]); sys.exit(3)")
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
    assert outcomes[1][2:] == (3, "300000\n", "abc")


@needs_proc
def test_the_stall_debug_flag_alone_reads_wchan_stack_and_pressure(monkeypatch):
    monkeypatch.setattr(node_run, "WALL_CEILING_MULTIPLE", 2)
    read_text = vars(Path)["read_text"]
    seen: list[str] = []

    def recording_read_text(path, *args, **kwargs):
        seen.append(str(path))
        return read_text(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", recording_read_text)
    debug_file = re.compile(r"/proc/(\d+/(wchan|stack)|pressure/(io|cpu|memory))")
    for flag in ("", "1"):
        monkeypatch.setenv("CLAUDIT_DEBUG_NODE_STALL", flag)
        seen.clear()
        with pytest.raises(subprocess.TimeoutExpired) as raised:
            node_run.run_node([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.25)
        noted = [note.partition(":")[0] for note in raised.value.__notes__ if debug_file.match(note)]
        read = [path for path in seen if debug_file.fullmatch(path)]
        if flag:
            assert [re.sub(r"\d+", "N", path) for path in noted] == [
                "/proc/N/wchan", "/proc/N/stack", "/proc/pressure/io", "/proc/pressure/cpu", "/proc/pressure/memory"]
            assert read == noted
            assert "unreadable" not in next(note for note in raised.value.__notes__ if "/wchan" in note)
        else:
            assert not noted and not read
