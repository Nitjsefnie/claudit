"""Real-child cancellation tests for export cleanup (issue #259)."""
from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Awaitable
from pathlib import Path
from typing import Any, TypeVar

import pytest
from fastapi import HTTPException

from backend import api_export

_TEST_WAIT_S = 15.0
_CHILD_RELEASE_TIMEOUT_S = 30.0
_POST_EXCEPTION_OBSERVATION_S = 1.0
_TIMEOUT_S = 0.2
_FLOOD_BYTES = 4 * 1024 * 1024
_T = TypeVar("_T")


def _renderer_argv(
    marker: Path,
    out_path: Path,
    release: Path,
    flood_bytes: int = 0,
) -> list[str]:
    script = (
        "import os, pathlib, sys, time\n"
        "marker, output, release = map(pathlib.Path, sys.argv[1:4])\n"
        "flood_bytes = int(sys.argv[4])\n"
        "marker.write_text(str(os.getpid()), encoding='ascii')\n"
        "if flood_bytes:\n"
        "    sys.stdout.buffer.write(b'x' * flood_bytes)\n"
        "    sys.stdout.buffer.flush()\n"
        f"deadline = time.monotonic() + {_CHILD_RELEASE_TIMEOUT_S}\n"
        "while not release.exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.01)\n"
        "if release.exists():\n"
        "    output.write_bytes(b'png')\n"
    )
    return [
        sys.executable,
        "-c",
        script,
        str(marker),
        str(out_path),
        str(release),
        str(flood_bytes),
    ]


async def _await_bounded(awaitable: Awaitable[_T], description: str) -> _T:
    task = asyncio.ensure_future(awaitable)
    done, _ = await asyncio.wait({task}, timeout=_TEST_WAIT_S)
    if not done:
        task.cancel()
        raise AssertionError(
            f"{description} did not finish within {_TEST_WAIT_S:g} seconds")
    return task.result()


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


def _assert_render_reaped(returncodes: list[int | None]) -> None:
    assert returncodes, "render exited before the renderer process was captured"
    assert returncodes[-1] is not None, "renderer was not reaped when the render exited"


def _hold_initial_communicate(
    monkeypatch: pytest.MonkeyPatch,
    communicate_started: asyncio.Event,
    release_communicate: asyncio.Event,
    raise_on_release: type[BaseException] | None = None,
) -> None:
    """Fill the pipe before reaping and register wait before the child exits."""
    original_create = api_export.asyncio.create_subprocess_exec

    async def hold_first_communicate(*args: Any, **kwargs: Any) -> Any:
        proc = await original_create(*args, **kwargs)
        original_communicate = proc.communicate
        original_kill = proc.kill
        first_call = True

        def defer_kill_one_turn() -> None:
            asyncio.get_running_loop().call_soon(original_kill)

        async def gated_communicate() -> tuple[bytes | None, bytes | None]:
            nonlocal first_call
            if first_call:
                first_call = False
                communicate_started.set()
                await release_communicate.wait()
                if raise_on_release is not None:
                    raise raise_on_release()
            return await original_communicate()

        monkeypatch.setattr(proc, "communicate", gated_communicate)
        monkeypatch.setattr(proc, "kill", defer_kill_one_turn)
        return proc

    monkeypatch.setattr(api_export.asyncio, "create_subprocess_exec", hold_first_communicate)


def _gate_reap_wait(
    monkeypatch: pytest.MonkeyPatch,
    reap_started: asyncio.Event,
    release_reap: asyncio.Event,
) -> list[asyncio.subprocess.Process]:
    """Gate proc.wait reached by _reap's shielded communicate after pipe drain."""
    reap_calls: list[asyncio.subprocess.Process] = []
    original_reap = api_export._reap  # pylint: disable=protected-access

    async def gated_reap(proc: asyncio.subprocess.Process) -> bool:
        reap_calls.append(proc)
        original_wait = proc.wait

        async def blocked_reap_wait() -> int:
            reap_started.set()
            await _await_bounded(release_reap.wait(), "reap wait gate")
            return await _await_bounded(original_wait(), "renderer process wait")

        monkeypatch.setattr(proc, "wait", blocked_reap_wait)
        return await original_reap(proc)

    monkeypatch.setattr(api_export, "_reap", gated_reap)
    return reap_calls


async def _wait_for_started(
    marker: Path, task: asyncio.Task[Any] | None = None
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


async def _wait_for_stdout_pause(proc: asyncio.subprocess.Process) -> None:
    process_transport = getattr(proc, "_transport")  # pylint: disable=protected-access
    stdout_transport = process_transport.get_pipe_transport(1)
    assert stdout_transport is not None
    deadline = asyncio.get_running_loop().time() + _TEST_WAIT_S
    while stdout_transport.is_reading():
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise AssertionError("renderer stdout pipe did not pause reading")
        await asyncio.sleep(min(0.01, remaining))


def _prepare_flooded_render(
    monkeypatch: pytest.MonkeyPatch,
    raise_on_release: type[BaseException] | None = None,
) -> tuple[
    list[asyncio.subprocess.Process],
    list[int | None],
    asyncio.Event,
    asyncio.Event,
]:
    communicate_started = asyncio.Event()
    release_communicate = asyncio.Event()
    _hold_initial_communicate(
        monkeypatch,
        communicate_started,
        release_communicate,
        raise_on_release,
    )
    processes, returncodes = _record_render_exit(monkeypatch)
    return processes, returncodes, communicate_started, release_communicate


async def _wait_for_flooded_render(
    marker: Path,
    task: asyncio.Task[Any],
    processes: list[asyncio.subprocess.Process],
    communicate_started: asyncio.Event,
) -> int:
    pid = await _wait_for_started(marker, task)
    proc = await _wait_for_process(processes)
    await _await_bounded(communicate_started.wait(), "initial communicate start")
    await _await_bounded(_wait_for_stdout_pause(proc), "stdout pipe pause")
    return pid


async def _file_appeared_during(path: Path, duration: float) -> bool:
    deadline = asyncio.get_running_loop().time() + duration
    while not path.exists():
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return path.exists()
        await asyncio.sleep(min(0.05, remaining))
    return True


async def _release_and_observe(release: Path, out_path: Path) -> bool:
    release.touch(exist_ok=True)
    return await _file_appeared_during(out_path, _POST_EXCEPTION_OBSERVATION_S)


async def _cleanup_test(
    task: asyncio.Task[Any] | None,
    processes: list[asyncio.subprocess.Process],
    release: Path,
    gates: tuple[asyncio.Event, ...] = (),
) -> None:
    for gate in gates:
        gate.set()
    if task is not None and not task.done():
        task.cancel()
    for proc in processes:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
    release.touch(exist_ok=True)
    for proc in processes:
        try:
            await _await_bounded(proc.communicate(), "renderer pipe cleanup")
        except (asyncio.CancelledError, Exception):
            pass
    if task is not None:
        if task.done():
            if not task.cancelled():
                task.exception()
        else:
            try:
                await _await_bounded(task, "render task cleanup")
            except (asyncio.CancelledError, Exception):
                pass


def _process_is_alive(pid: int) -> bool | None:
    if os.name != "posix":
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_cancelled_render_reaps_child_and_prevents_output(tmp_path, monkeypatch):
    marker = tmp_path / "started"
    release = tmp_path / "release"
    out_path = tmp_path / "out.png"
    processes, returncodes = _record_render_exit(monkeypatch)

    async def run() -> None:
        task: asyncio.Task[Any] | None = None
        try:
            task = asyncio.create_task(api_export._render_export(  # pylint: disable=protected-access
                _renderer_argv(marker, out_path, release), str(out_path)))
            pid = await _wait_for_started(marker, task)
            await _wait_for_process(processes)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await _await_bounded(task, "cancelled render task")

            output_appeared = await _release_and_observe(release, out_path)
            _assert_render_reaped(returncodes)
            child_alive = _process_is_alive(pid)
            assert child_alive is not True and not output_appeared, (
                f"child_alive_immediately_after_cancel={child_alive}; "
                f"output_appeared_after_cancel={output_appeared}")
        finally:
            await _cleanup_test(task, processes, release)

    asyncio.run(run())


def test_cancelled_render_drains_flooded_stdout_before_reaping(tmp_path, monkeypatch):
    marker = tmp_path / "started"
    release = tmp_path / "release"
    out_path = tmp_path / "out.png"
    processes, returncodes, communicate_started, release_communicate = (
        _prepare_flooded_render(monkeypatch))

    async def run() -> None:
        task: asyncio.Task[Any] | None = None
        try:
            task = asyncio.create_task(api_export._render_export(  # pylint: disable=protected-access
                _renderer_argv(marker, out_path, release, _FLOOD_BYTES), str(out_path)))
            pid = await _wait_for_flooded_render(
                marker, task, processes, communicate_started)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await _await_bounded(task, "cancelled flooded render task")

            output_appeared = await _release_and_observe(release, out_path)
            _assert_render_reaped(returncodes)
            child_alive = _process_is_alive(pid)
            assert child_alive is not True and not output_appeared, (
                f"child_alive_after_flood_cancel={child_alive}; "
                f"output_appeared_after_flood_cancel={output_appeared}")
        finally:
            await _cleanup_test(task, processes, release, (release_communicate,))

    asyncio.run(run())


def test_cancelled_handler_unlinks_temp_png_after_reaping_child(tmp_path, monkeypatch):
    marker = tmp_path / "started"
    release = tmp_path / "release"
    captured_paths: list[str] = []
    processes, returncodes = _record_render_exit(monkeypatch)

    def build_argv(_rng: str, _project: str | None, out_path: str) -> list[str]:
        captured_paths.append(out_path)
        return _renderer_argv(marker, Path(out_path), release)

    monkeypatch.setattr(api_export, "build_export_argv", build_argv)

    async def run() -> None:
        task: asyncio.Task[Any] | None = None
        try:
            task = asyncio.create_task(api_export.export_png(rng="7d", project=None))
            pid = await _wait_for_started(marker, task)
            assert captured_paths
            out_path = Path(captured_paths[0])
            await _wait_for_process(processes)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await _await_bounded(task, "cancelled handler task")

            output_appeared = await _release_and_observe(release, out_path)
            _assert_render_reaped(returncodes)
            child_alive = _process_is_alive(pid)
            assert not api_export._export_lock.locked()  # pylint: disable=protected-access
            assert child_alive is not True and not output_appeared, (
                f"child_alive_immediately_after_cancel={child_alive}; "
                f"temp_png_reappeared_after_handler_cleanup={output_appeared}; "
                f"path={out_path}")
        finally:
            await _cleanup_test(task, processes, release)

    asyncio.run(run())


def test_export_timeout_still_returns_503_and_reaps_child(tmp_path, monkeypatch):
    marker = tmp_path / "started"
    release = tmp_path / "release"
    out_path = tmp_path / "out.png"
    monkeypatch.setattr(api_export, "_EXPORT_TIMEOUT_S", _TIMEOUT_S)
    processes, returncodes = _record_render_exit(monkeypatch)

    async def run() -> None:
        task: asyncio.Task[Any] | None = None
        try:
            task = asyncio.create_task(api_export._render_export(  # pylint: disable=protected-access
                _renderer_argv(marker, out_path, release), str(out_path)))
            with pytest.raises(HTTPException) as excinfo:
                await _await_bounded(task, "timed-out render task")
            output_appeared = await _release_and_observe(release, out_path)
            assert excinfo.value.status_code == 503
            assert excinfo.value.detail == "export render timed out"
            assert processes
            pid = processes[0].pid
            _assert_render_reaped(returncodes)

            child_alive = _process_is_alive(pid)
            assert child_alive is not True and not output_appeared, (
                f"child_alive_after_timeout={child_alive}; "
                f"output_appeared_after_timeout={output_appeared}")
        finally:
            await _cleanup_test(task, processes, release)

    asyncio.run(run())


def test_export_timeout_drains_flooded_stdout_before_reaping(tmp_path, monkeypatch):
    marker = tmp_path / "started"
    release = tmp_path / "release"
    out_path = tmp_path / "out.png"
    monkeypatch.setattr(api_export, "_EXPORT_TIMEOUT_S", 60.0)
    processes, returncodes, communicate_started, release_communicate = (
        _prepare_flooded_render(monkeypatch, asyncio.TimeoutError))

    async def run() -> None:
        task: asyncio.Task[Any] | None = None
        try:
            task = asyncio.create_task(api_export._render_export(  # pylint: disable=protected-access
                _renderer_argv(marker, out_path, release, _FLOOD_BYTES), str(out_path)))
            await _wait_for_flooded_render(marker, task, processes, communicate_started)
            release_communicate.set()
            with pytest.raises(HTTPException) as excinfo:
                await _await_bounded(task, "timed-out flooded render task")

            output_appeared = await _release_and_observe(release, out_path)
            assert excinfo.value.status_code == 503
            assert excinfo.value.detail == "export render timed out"
            _assert_render_reaped(returncodes)
            assert not output_appeared
        finally:
            await _cleanup_test(task, processes, release, (release_communicate,))

    asyncio.run(run())


def test_cancel_during_timeout_reap_waits_and_propagates_cancel(tmp_path, monkeypatch):
    marker = tmp_path / "started"
    release = tmp_path / "release"
    reap_started = asyncio.Event()
    release_reap = asyncio.Event()
    monkeypatch.setattr(api_export, "_EXPORT_TIMEOUT_S", _TIMEOUT_S)
    processes, returncodes = _record_render_exit(monkeypatch)
    reap_calls = _gate_reap_wait(monkeypatch, reap_started, release_reap)

    original_create = api_export.asyncio.create_subprocess_exec

    # The render timeout starts after this wrapper returns.
    async def create_after_started(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        proc = await original_create(*args, **kwargs)
        await _wait_for_started(marker)
        return proc

    monkeypatch.setattr(api_export.asyncio, "create_subprocess_exec", create_after_started)

    async def run() -> None:
        task: asyncio.Task[Any] | None = None
        try:
            task = asyncio.create_task(api_export._render_export(  # pylint: disable=protected-access
                _renderer_argv(marker, tmp_path / "out.png", release),
                str(tmp_path / "out.png")))
            proc = await _wait_for_process(processes)
            await _wait_for_started(marker, task)
            await _await_bounded(reap_started.wait(), "timeout reap wait start")
            assert reap_calls == [proc]
            task.cancel()
            for _ in range(5):
                await asyncio.sleep(0)
            assert not task.done(), "render task finished while timeout reap gate was closed"
            release_reap.set()

            with pytest.raises(asyncio.CancelledError):
                await _await_bounded(task, "cancelled timeout reap task")
            output_appeared = await _release_and_observe(release, tmp_path / "out.png")
            _assert_render_reaped(returncodes)
            child_alive = _process_is_alive(proc.pid)
            assert child_alive is not True and not output_appeared, (
                f"child_alive_after_timeout_cancel={child_alive}; "
                f"output_appeared_after_timeout_cancel={output_appeared}")
        finally:
            await _cleanup_test(task, processes, release, (release_reap,))

    asyncio.run(run())


def test_communicate_error_still_reaps_child_before_propagating(tmp_path, monkeypatch):
    marker = tmp_path / "started"
    release = tmp_path / "release"
    out_path = tmp_path / "out.png"
    processes: list[asyncio.subprocess.Process] = []
    allow_communicate_error = asyncio.Event()
    original_create = api_export.asyncio.create_subprocess_exec

    async def capture_process(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        proc = await original_create(*args, **kwargs)
        processes.append(proc)
        original_communicate = proc.communicate
        first_call = True

        async def fail_communicate_once() -> tuple[bytes | None, bytes | None]:
            nonlocal first_call
            if first_call:
                first_call = False
                await allow_communicate_error.wait()
                raise OSError("synthetic communicate failure")
            return await original_communicate()

        monkeypatch.setattr(proc, "communicate", fail_communicate_once)
        return proc

    monkeypatch.setattr(api_export.asyncio, "create_subprocess_exec", capture_process)

    async def run() -> None:
        task: asyncio.Task[Any] | None = None
        try:
            task = asyncio.create_task(api_export._render_export(  # pylint: disable=protected-access
                _renderer_argv(marker, out_path, release), str(out_path)))
            await _wait_for_started(marker, task)
            allow_communicate_error.set()
            with pytest.raises(OSError, match="synthetic communicate failure"):
                await _await_bounded(task, "render after communicate error")

            assert processes and processes[0].returncode is not None
            output_appeared = await _release_and_observe(release, out_path)
            assert not output_appeared
        finally:
            await _cleanup_test(task, processes, release)

    asyncio.run(run())


def test_pipe_read_error_still_reaps_child_and_preserves_error(tmp_path, monkeypatch):
    marker = tmp_path / "started"
    release = tmp_path / "release"
    out_path = tmp_path / "out.png"
    allow_read_failure = asyncio.Event()
    original_create = api_export.asyncio.create_subprocess_exec

    async def capture_process(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        proc = await original_create(*args, **kwargs)
        assert proc.stdout is not None
        original_read = proc.stdout.read
        first_read = True
        original_kill = proc.kill
        kill_calls = 0

        def defer_kill_one_tenth_second() -> None:
            nonlocal kill_calls
            kill_calls += 1
            if kill_calls == 1:
                asyncio.get_running_loop().call_later(0.1, original_kill)
            else:
                original_kill()

        async def fail_first_read(*read_args: Any, **read_kwargs: Any) -> bytes:
            nonlocal first_read
            if first_read:
                first_read = False
                await _await_bounded(
                    allow_read_failure.wait(), "synthetic pipe read failure gate")
                raise OSError("synthetic pipe read failure")
            return await original_read(*read_args, **read_kwargs)

        monkeypatch.setattr(proc.stdout, "read", fail_first_read)
        monkeypatch.setattr(proc, "kill", defer_kill_one_tenth_second)
        return proc

    monkeypatch.setattr(api_export.asyncio, "create_subprocess_exec", capture_process)
    processes, returncodes = _record_render_exit(monkeypatch)

    async def run() -> None:
        task: asyncio.Task[Any] | None = None
        try:
            task = asyncio.create_task(api_export._render_export(  # pylint: disable=protected-access
                _renderer_argv(marker, out_path, release), str(out_path)))
            await _wait_for_started(marker, task)
            allow_read_failure.set()
            with pytest.raises(OSError, match="synthetic pipe read failure"):
                await _await_bounded(task, "render after pipe read error")

            _assert_render_reaped(returncodes)
            output_appeared = await _release_and_observe(release, out_path)
            assert not output_appeared
        finally:
            await _cleanup_test(task, processes, release, (allow_read_failure,))
            for proc in processes:
                await _await_bounded(proc.wait(), "renderer process cleanup wait")

    asyncio.run(run())


def test_second_cancel_during_reap_still_waits_for_child(tmp_path, monkeypatch):
    marker = tmp_path / "started"
    release = tmp_path / "release"
    out_path = tmp_path / "out.png"
    reap_started = asyncio.Event()
    release_reap = asyncio.Event()
    processes, returncodes = _record_render_exit(monkeypatch)
    reap_calls = _gate_reap_wait(monkeypatch, reap_started, release_reap)

    async def run() -> None:
        task: asyncio.Task[Any] | None = None
        try:
            task = asyncio.create_task(api_export._render_export(  # pylint: disable=protected-access
                _renderer_argv(marker, out_path, release), str(out_path)))
            pid = await _wait_for_started(marker, task)
            proc = await _wait_for_process(processes)
            task.cancel()
            await _await_bounded(reap_started.wait(), "cancellation reap wait start")
            assert reap_calls == [proc]
            task.cancel()
            for _ in range(5):
                await asyncio.sleep(0)
            assert not task.done(), "render task finished while reap gate was closed"
            release_reap.set()

            with pytest.raises(asyncio.CancelledError):
                await _await_bounded(task, "second-cancel reap task")
            output_appeared = await _release_and_observe(release, out_path)
            _assert_render_reaped(returncodes)
            child_alive = _process_is_alive(pid)
            assert child_alive is not True and not output_appeared, (
                f"child_alive_after_second_cancel={child_alive}; "
                f"output_appeared_after_second_cancel={output_appeared}")
        finally:
            await _cleanup_test(task, processes, release, (release_reap,))

    asyncio.run(run())


def test_process_lookup_race_during_kill_still_reaps_child(tmp_path, monkeypatch):
    marker = tmp_path / "started"
    release = tmp_path / "release"
    out_path = tmp_path / "out.png"
    processes, returncodes = _record_render_exit(monkeypatch)

    async def run() -> None:
        task: asyncio.Task[Any] | None = None
        try:
            task = asyncio.create_task(api_export._render_export(  # pylint: disable=protected-access
                _renderer_argv(marker, out_path, release), str(out_path)))
            pid = await _wait_for_started(marker, task)
            proc = await _wait_for_process(processes)
            original_kill = proc.kill

            def kill_then_report_race() -> None:
                original_kill()
                raise ProcessLookupError("renderer exited during kill")

            monkeypatch.setattr(proc, "kill", kill_then_report_race)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await _await_bounded(task, "cancelled render after kill race")

            output_appeared = await _release_and_observe(release, out_path)
            assert proc.returncode is not None
            _assert_render_reaped(returncodes)
            assert _process_is_alive(pid) is not True and not output_appeared
        finally:
            await _cleanup_test(task, processes, release)

    asyncio.run(run())


def test_reap_does_not_kill_an_already_finished_child(tmp_path, monkeypatch):
    processes: list[asyncio.subprocess.Process] = []
    release = tmp_path / "release"

    async def run() -> None:
        try:
            proc = await asyncio.create_subprocess_exec(sys.executable, "-c", "pass")
            processes.append(proc)
            await _await_bounded(proc.wait(), "already-finished child wait")
            assert proc.returncode == 0
            kill_calls: list[None] = []

            def record_kill() -> None:
                kill_calls.append(None)

            monkeypatch.setattr(proc, "kill", record_kill)
            await _await_bounded(
                api_export._reap(proc), "reap already-finished child"  # pylint: disable=protected-access
            )
            assert proc.returncode == 0
            assert not kill_calls
        finally:
            await _cleanup_test(None, processes, release)

    asyncio.run(run())
