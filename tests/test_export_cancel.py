"""Real-child cancellation tests for export cleanup (issue #259)."""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi import HTTPException

from backend import api_export

_CHILD_SLEEP_S = 0.8
_POST_CANCEL_OBSERVATION_S = 2.0
_TIMEOUT_S = 0.2
_TIMEOUT_CHILD_SLEEP_S = 1.0
_POST_TIMEOUT_OBSERVATION_S = 3.5


def _renderer_argv(marker: Path, out_path: Path, delay: float = _CHILD_SLEEP_S) -> list[str]:
    script = (
        "import os, pathlib, sys, time\n"
        "marker, output, delay = sys.argv[1], sys.argv[2], float(sys.argv[3])\n"
        "pathlib.Path(marker).write_text(str(os.getpid()), encoding='ascii')\n"
        "time.sleep(delay)\n"
        "pathlib.Path(output).write_bytes(b'png')\n"
    )
    return [sys.executable, "-c", script, str(marker), str(out_path), str(delay)]


def _record_render_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[asyncio.subprocess.Process], list[int | None]]:
    processes: list[asyncio.subprocess.Process] = []
    returncodes: list[int | None] = []
    original_create = api_export.asyncio.create_subprocess_exec
    original_render = api_export._render_export  # pylint: disable=protected-access

    async def capture_process(*args: Any, **kwargs: Any) -> Any:
        proc = await original_create(*args, **kwargs)
        processes.append(proc)
        return proc

    async def render_and_record(argv: list[str], out_path: str) -> None:
        try:
            await original_render(argv, out_path)
        finally:
            if processes:
                returncodes.append(processes[-1].returncode)

    monkeypatch.setattr(api_export.asyncio, "create_subprocess_exec", capture_process)
    monkeypatch.setattr(api_export, "_render_export", render_and_record)
    return processes, returncodes


async def _wait_for_started(
    marker: Path, task: asyncio.Task[None] | None = None
) -> int:
    deadline = asyncio.get_running_loop().time() + 10.0
    while True:
        if marker.exists():
            try:
                return int(marker.read_text(encoding="ascii"))
            except ValueError:
                pass
        if task is not None and task.done():
            task.result()
            raise AssertionError("renderer finished before creating its started marker")
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise AssertionError("renderer did not create its started marker")
        await asyncio.sleep(min(0.01, remaining))


async def _wait_for_process(
    processes: list[asyncio.subprocess.Process],
) -> asyncio.subprocess.Process:
    deadline = asyncio.get_running_loop().time() + 10.0
    while not processes:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise AssertionError("renderer process was not captured")
        await asyncio.sleep(min(0.01, remaining))
    return processes[0]


async def _file_appeared_during(path: Path, duration: float) -> bool:
    deadline = asyncio.get_running_loop().time() + duration
    while not path.exists():
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return path.exists()
        await asyncio.sleep(min(0.05, remaining))
    return True


def _process_is_alive(pid: int) -> bool | None:
    if sys.platform == "win32":
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_cancelled_render_reaps_child_and_prevents_output(tmp_path, monkeypatch):
    marker = tmp_path / "started"
    out_path = tmp_path / "out.png"
    _, returncodes = _record_render_exit(monkeypatch)

    async def run() -> None:
        task = asyncio.create_task(api_export._render_export(  # pylint: disable=protected-access
            _renderer_argv(marker, out_path), str(out_path)))
        pid = await _wait_for_started(marker)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert returncodes and returncodes[-1] is not None
        child_alive = _process_is_alive(pid)
        output_appeared = await _file_appeared_during(
            out_path, _POST_CANCEL_OBSERVATION_S)
        assert child_alive is not True and not output_appeared, (
            f"child_alive_immediately_after_cancel={child_alive}; "
            f"output_appeared_after_cancel={output_appeared}")

    asyncio.run(run())


def test_cancelled_handler_unlinks_temp_png_after_reaping_child(tmp_path, monkeypatch):
    marker = tmp_path / "started"
    captured_paths: list[str] = []

    def build_argv(_rng: str, _project: str | None, out_path: str) -> list[str]:
        captured_paths.append(out_path)
        return _renderer_argv(marker, Path(out_path))

    monkeypatch.setattr(api_export, "build_export_argv", build_argv)
    _, returncodes = _record_render_exit(monkeypatch)

    async def run() -> None:
        task = asyncio.create_task(api_export.export_png(rng="7d", project=None))
        pid = await _wait_for_started(marker)
        assert captured_paths
        out_path = Path(captured_paths[0])
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert returncodes and returncodes[-1] is not None
        child_alive = _process_is_alive(pid)
        output_appeared = await _file_appeared_during(
            out_path, _POST_CANCEL_OBSERVATION_S)
        assert not api_export._export_lock.locked()  # pylint: disable=protected-access
        assert child_alive is not True and not output_appeared, (
            f"child_alive_immediately_after_cancel={child_alive}; "
            f"temp_png_reappeared_after_handler_cleanup={output_appeared}; "
            f"path={out_path}")

    asyncio.run(run())


def test_export_timeout_still_returns_503_and_reaps_child(tmp_path, monkeypatch):
    marker = tmp_path / "started"
    out_path = tmp_path / "out.png"
    monkeypatch.setattr(api_export, "_EXPORT_TIMEOUT_S", _TIMEOUT_S)
    processes, returncodes = _record_render_exit(monkeypatch)

    async def run() -> None:
        task = asyncio.create_task(api_export._render_export(  # pylint: disable=protected-access
            _renderer_argv(marker, out_path, delay=_TIMEOUT_CHILD_SLEEP_S), str(out_path)))
        with pytest.raises(HTTPException) as excinfo:
            await task
        assert excinfo.value.status_code == 503
        assert excinfo.value.detail == "export render timed out"
        assert processes
        pid = processes[0].pid
        assert returncodes and returncodes[-1] is not None

        child_alive = _process_is_alive(pid)
        output_appeared = await _file_appeared_during(
            out_path, _POST_TIMEOUT_OBSERVATION_S)
        assert child_alive is not True and not output_appeared, (
            f"child_alive_after_timeout={child_alive}; "
            f"output_appeared_after_timeout={output_appeared}")

    asyncio.run(run())


def test_cancel_during_timeout_reap_waits_and_propagates_cancel(tmp_path, monkeypatch):
    out_path = tmp_path / "out.png"
    monkeypatch.setattr(api_export, "_EXPORT_TIMEOUT_S", _TIMEOUT_S)
    processes, returncodes = _record_render_exit(monkeypatch)

    async def run() -> None:
        task = asyncio.create_task(api_export._render_export(  # pylint: disable=protected-access
            _renderer_argv(tmp_path / "unused", out_path,
                           delay=_TIMEOUT_CHILD_SLEEP_S), str(out_path)))
        proc = await _wait_for_process(processes)
        pid = proc.pid

        reap_started = asyncio.Event()
        release_reap = asyncio.Event()
        original_wait = proc.wait

        async def blocked_reap_wait() -> int:
            reap_started.set()
            await release_reap.wait()
            return await original_wait()

        monkeypatch.setattr(proc, "wait", blocked_reap_wait)
        await asyncio.wait_for(reap_started.wait(), timeout=10.0)
        task.cancel()
        release_reap.set()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert returncodes and returncodes[-1] is not None
        child_alive = _process_is_alive(pid)
        output_appeared = await _file_appeared_during(
            out_path, _POST_CANCEL_OBSERVATION_S)
        assert child_alive is not True and not output_appeared, (
            f"child_alive_after_timeout_cancel={child_alive}; "
            f"output_appeared_after_timeout_cancel={output_appeared}")

    asyncio.run(run())


def test_second_cancel_during_reap_still_waits_for_child(tmp_path, monkeypatch):
    marker = tmp_path / "started"
    out_path = tmp_path / "out.png"
    processes, returncodes = _record_render_exit(monkeypatch)

    async def run() -> None:
        task = asyncio.create_task(api_export._render_export(  # pylint: disable=protected-access
            _renderer_argv(marker, out_path), str(out_path)))
        pid = await _wait_for_started(marker)
        assert processes

        reap_started = asyncio.Event()
        release_reap = asyncio.Event()
        original_wait = processes[0].wait

        async def blocked_reap_wait() -> int:
            reap_started.set()
            await release_reap.wait()
            return await original_wait()

        monkeypatch.setattr(processes[0], "wait", blocked_reap_wait)
        task.cancel()
        await asyncio.wait_for(reap_started.wait(), timeout=10.0)
        task.cancel()
        release_reap.set()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert returncodes and returncodes[-1] is not None
        child_alive = _process_is_alive(pid)
        output_appeared = await _file_appeared_during(
            out_path, _POST_CANCEL_OBSERVATION_S)
        assert child_alive is not True and not output_appeared, (
            f"child_alive_after_second_cancel={child_alive}; "
            f"output_appeared_after_second_cancel={output_appeared}")

    asyncio.run(run())


def test_reap_does_not_kill_an_already_finished_child(monkeypatch):
    async def run() -> None:
        proc = await asyncio.create_subprocess_exec(sys.executable, "-c", "pass")
        await proc.wait()
        assert proc.returncode == 0
        kill_calls: list[None] = []

        def record_kill() -> None:
            kill_calls.append(None)
        monkeypatch.setattr(proc, "kill", record_kill)

        await api_export._reap(proc)  # pylint: disable=protected-access
        assert proc.returncode == 0
        assert not kill_calls

    asyncio.run(run())
