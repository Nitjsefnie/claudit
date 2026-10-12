"""An isolated Linux subreaper for service/server/worker lifetime tests."""
from __future__ import annotations

import ctypes
import json
import multiprocessing
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path
from concurrent.futures.process import BrokenProcessPool


def blocked_worker(connection) -> None:
    """Report readiness after the production initializer, then keep working."""
    connection.send({"server": os.getppid(), "worker": os.getpid(),
                     "term": signal.getsignal(signal.SIGTERM),
                     "interrupt": signal.getsignal(signal.SIGINT)})
    connection.close()
    time.sleep(90)


def service() -> None:
    """A service with an ignored SIGTERM, to challenge the server preload."""
    from backend.ingest_workers import parse_pool  # pylint: disable=import-outside-toplevel
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    receive, send = multiprocessing.Pipe(duplex=False)
    with parse_pool(1, os.getpid()) as pool:
        future = pool.submit(blocked_worker, send)
        print(json.dumps(receive.recv()), flush=True)
        try:
            future.result(timeout=100)
        except BrokenProcessPool:
            pass  # the server-term probe deliberately breaks this pool


def reaped(pid: int) -> bool:
    """True only after waitpid proves this owned child has exited."""
    try:
        return os.waitpid(pid, os.WNOHANG)[0] == pid
    except ChildProcessError:
        return True


def wait_dead(pid: int) -> bool:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if reaped(pid):
            return True
        time.sleep(0.01)
    return False


def stopped(pid: int) -> bool:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()[0]
        except FileNotFoundError:
            return True
        if state == "Z":
            return True
        time.sleep(0.01)
    return False


def probe(mode: str) -> None:
    # Orphans are adopted here, so server/worker death can be proved by
    # waitpid instead of mistaking a zombie for a surviving process.
    _subreaper()
    with subprocess.Popen(
        [sys.executable, "-m", "tests.ingest_lifetime_probe", "service"],
            stdout=subprocess.PIPE, text=True) as proc:
        _run_probe(proc, mode)


def _run_probe(proc: subprocess.Popen, mode: str) -> None:
    try:
        assert proc.stdout is not None
        assert select.select([proc.stdout], [], [], 30)[0], "service did not start"
        ready = json.loads(proc.stdout.readline())
        server, worker = ready["server"], ready["worker"]
        assert server != proc.pid, "worker still has the service as its parent"
        assert ready["term"] == signal.SIG_DFL
        assert ready["interrupt"] == signal.SIG_DFL
        assert len(list(Path(f"/proc/{server}/task").iterdir())) == 1
        if mode == "service-kill":
            os.kill(proc.pid, signal.SIGKILL)
            proc.wait(timeout=10)
            assert wait_dead(server), "server survived service death"
            assert wait_dead(worker), "worker survived server death"
        else:
            os.kill(server, signal.SIGTERM)
            assert stopped(server), "server survived SIGTERM"
            # Reap the service first to adopt any orphaned worker.
            proc.wait(timeout=15)
            assert wait_dead(server), "server survived SIGTERM"
            assert wait_dead(worker), "worker survived server SIGTERM"
        print(json.dumps({"server_dead": True, "worker_dead": True, "mode": mode}))
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
        _cleanup_children()


def _subreaper() -> None:
    if sys.platform == "linux":
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        assert libc.prctl(36, 1, 0, 0, 0) == 0  # PR_SET_CHILD_SUBREAPER


def _cleanup_children() -> None:
    """Kill/reap only adopted children; let trackers clean owned semaphores."""
    children = Path(f"/proc/self/task/{os.getpid()}/children")
    trackers = set()
    for _ in range(2):  # also collect a killed server's adopted workers
        for raw in children.read_text(encoding="utf-8").split():
            pid = int(raw)
            if reaped(pid):
                continue
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
            if b"multiprocessing.resource_tracker" in cmdline:
                trackers.add(pid)
                continue
            os.kill(pid, signal.SIGKILL)
            assert wait_dead(pid), "owned child survived SIGKILL"
    for pid in trackers:
        if not wait_dead(pid):
            os.kill(pid, signal.SIGKILL)
            assert wait_dead(pid), "resource tracker survived SIGKILL"


def check(target: str) -> None:
    """The executor owner's cleanup can hang; this independent parent cannot."""
    _subreaper()
    proc = subprocess.Popen(  # pylint: disable=consider-using-with
        [sys.executable, "-m", "tests.ingest_lifetime_probe", "test", target],
        start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True)
    try:
        try:
            out, err = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired as exc:
            raise AssertionError("process-pool test exceeded 30s bound") from exc
        print(out, end="")
        print(err, end="", file=sys.stderr)
        assert proc.returncode == 0, "isolated test failed"
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
        if sys.platform == "linux":
            _cleanup_children()
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        assert proc.stdout is not None and proc.stderr is not None
        proc.stdout.close()
        proc.stderr.close()


if __name__ == "__main__":
    if sys.argv[1] == "service":
        service()
    elif sys.argv[1] == "check":
        check(sys.argv[2])
    elif sys.argv[1] == "test":
        import pytest
        from tests import pool_test_bounds
        pool_test_bounds.ISOLATED = True
        raise SystemExit(pytest.main([sys.argv[2], "-q", "--tb=short"]))
    else:
        probe(sys.argv[1])
