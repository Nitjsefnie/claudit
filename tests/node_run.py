"""Run node under a hang guard on the child's own CPU time, so a child blocked on I/O is no hang.

Without /proc the guard is a wall timeout with one retry.
"""
from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

PROC_STAT_READABLE = os.path.exists("/proc/self/stat")
# A blocked child gets 5x its CPU budget in wall time: 5 min at 60 s, well inside a CI job's limit.
WALL_CEILING_MULTIPLE = 5
POLL_SECONDS = 0.25
TAIL_CHARS = 2000
STALL_DEBUG_FILES = ("/proc/{pid}/wchan", "/proc/{pid}/stack", "/proc/pressure/io", "/proc/pressure/cpu", "/proc/pressure/memory")


def run_node(
    node_argv: Sequence[str], *, input: str | bytes | None = None,  # pylint: disable=W0622
    timeout: float = 60.0, **run_kwargs: Any,
) -> subprocess.CompletedProcess[Any]:
    if run_kwargs.get("text") or run_kwargs.get("universal_newlines"):
        run_kwargs.setdefault("encoding", "utf-8")  # node speaks UTF-8; without UTF-8 mode (on by default from 3.15) the default is the locale's encoding
    if PROC_STAT_READABLE:
        return _run_on_cpu_budget(node_argv, input, timeout, run_kwargs)
    retried = False
    while True:
        started = time.monotonic()
        try:
            return subprocess.run(node_argv, input=input, timeout=timeout, **run_kwargs)  # pylint: disable=W1510
        except subprocess.TimeoutExpired as error:
            if retried:
                _add_kill_notes(error, f"wall timeout of {timeout} s", time.monotonic() - started, None)
                raise
            retried = True


def _run_on_cpu_budget(
    node_argv: Sequence[str], stdin_data: Any, timeout: float, run_kwargs: dict[str, Any],
) -> subprocess.CompletedProcess[Any]:
    check = run_kwargs.pop("check", False)
    # shortcut: skips subprocess.run's stdin/stdout/stderr conflict check; add it once a caller passes those.
    if stdin_data is not None:
        run_kwargs["stdin"] = subprocess.PIPE
    if run_kwargs.pop("capture_output", False):
        run_kwargs["stdout"] = run_kwargs["stderr"] = subprocess.PIPE
    with subprocess.Popen(node_argv, **run_kwargs) as process:
        started = time.monotonic()
        try:
            ceiling = timeout * WALL_CEILING_MULTIPLE
            while True:
                try:
                    stdout, stderr = process.communicate(stdin_data, timeout=POLL_SECONDS)
                    break
                except subprocess.TimeoutExpired:
                    stdin_data = None  # communicate() keeps unsent input; resending raises
                cpu, wall = _cpu_seconds(process.pid), time.monotonic() - started
                if cpu > timeout:
                    raise _kill(process, timeout, f"CPU budget of {timeout} s", wall, cpu)
                if wall > ceiling:
                    raise _kill(process, timeout, f"wall ceiling of {ceiling} s", wall, cpu)
        except BaseException:
            process.kill()
            process.wait()
            raise
        retcode = process.wait()
    if check and retcode:
        raise subprocess.CalledProcessError(retcode, process.args, output=stdout, stderr=stderr)
    return subprocess.CompletedProcess(process.args, retcode, stdout, stderr)


def _cpu_seconds(pid: int) -> float:
    # utime and stime are fields 14-15; the command name before them may hold ")".
    fields = Path(f"/proc/{pid}/stat").read_bytes().rpartition(b")")[2].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


def _kill(process: subprocess.Popen[Any], timeout: float, bound: str, wall: float, cpu: float) -> subprocess.TimeoutExpired:
    debug = _stall_debug(process.pid) if os.environ.get("CLAUDIT_DEBUG_NODE_STALL") else []
    process.kill()
    try:
        stdout, stderr = process.communicate(timeout=POLL_SECONDS)
    except subprocess.TimeoutExpired as drain:  # a grandchild still holds the pipes
        stdout, stderr = drain.output, drain.stderr
    error = subprocess.TimeoutExpired(process.args, timeout, output=stdout, stderr=stderr)
    _add_kill_notes(error, bound, wall, cpu)
    for note in debug:
        error.add_note(note)
    return error


def _stall_debug(pid: int) -> list[str]:
    notes = []
    for path in (template.format(pid=pid) for template in STALL_DEBUG_FILES):
        try:
            notes.append(f"{path}:\n{Path(path).read_text(encoding='utf-8', errors='replace')}")
        except OSError as error:
            notes.append(f"{path}: unreadable: {error}")
    return notes


def _add_kill_notes(error: subprocess.TimeoutExpired, bound: str, wall: float, cpu: float | None) -> None:
    if test := os.environ.get("PYTEST_CURRENT_TEST"):
        error.add_note(f"stalled test: {test}")
    cpu_text = "CPU time unreadable without /proc" if cpu is None else f"{cpu:.2f} s CPU"
    error.add_note(f"killed on the {bound} after {wall:.2f} s wall and {cpu_text}")
    for name, value in (("stdout", error.output), ("stderr", error.stderr)):
        text = value.decode("utf-8", "replace") if isinstance(value, bytes) else value or ""
        error.add_note(f"node {name} tail:\n{text[-TAIL_CHARS:]}")
