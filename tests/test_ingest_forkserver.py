"""Forkserver controls, importable child units and bounded process cleanup."""
from __future__ import annotations

import ctypes
import functools
import errno
import importlib
import json
import logging
import lzma
import multiprocessing
from multiprocessing import forkserver
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import threading
import time
import types
import warnings
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, call, patch

import pytest

from backend import ingest, ingest_fetch, ingest_workers, pricing, r2, rate_fingerprint
from backend.ingest_workers import parse_pool
from backend import timing as _timing

pytest_plugins = ("test_ingest",)

_DECOMPRESS = lzma.decompress


def noop_parse(key: str, sidecar_key: str | None = None, etag: str | None = None, size: int | None = None) -> dict:
    return {}


def worker_report() -> tuple:
    return (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT), os.getppid())


def server_report_parse(key: str, sidecar_key: str | None = None, etag: str | None = None, size: int | None = None) -> dict:
    parent = os.getppid()
    return {"parent": parent, "method": multiprocessing.get_start_method(), "threads": len(list(Path(f"/proc/{parent}/task").iterdir()))}


def configuration() -> dict:
    keys = ("R2_ENDPOINT", "R2_BUCKET", "R2_BLOB_CACHE", "CLAUDIT_TIMING")
    return {"env": {key: os.environ.get(key) for key in keys}, "timing": _timing.TIMING_ON, "parent": os.getppid()}


def client_configuration() -> dict:
    client = r2._boto_client()  # pylint: disable=protected-access
    return {"env": os.environ.get("AWS_CA_BUNDLE"), "parent": os.getppid(),
            "verify": client._endpoint.http_session._verify}  # pylint: disable=protected-access


def priced_parse(key: str, sidecar_key: str | None, etag: str | None = None, size: int | None = None, *, tables: dict) -> dict:
    with ExitStack() as stack:
        for name, value in tables.items():
            stack.enter_context(patch.object(pricing, name, value))
        rate_fingerprint.clear_fingerprint_cache()
        try:
            return ingest._fetch_and_parse(key, sidecar_key, etag, size)  # pylint: disable=protected-access
        finally:
            rate_fingerprint.clear_fingerprint_cache()


def fetch_failure(key: str, sidecar_key: str | None, etag: str | None = None, size: int | None = None, *, failed_key: str, error: type[Exception]) -> dict:

    def fetch(file_key: str, *rest) -> bytes:
        if file_key.endswith(failed_key):
            raise error("connection dropped" if error is OSError else file_key)
        return ingest_fetch.fetch_with_retry(file_key, *rest)
    return ingest_fetch.fetch_and_parse(key, sidecar_key, fetch, etag=etag, size=size)


def _broken_get_object(_key: str) -> bytes:
    raise TypeError("no client at all")


def fatal_fetch(key: str, sidecar_key: str | None, etag: str | None = None, size: int | None = None) -> dict:
    with patch.object(r2, "get_object", _broken_get_object):
        return ingest_fetch.fetch_and_parse(key, sidecar_key, ingest_fetch.fetch_with_retry, etag=etag, size=size)


def _padded_decompress(data: bytes) -> bytes:
    out = _DECOMPRESS(data)
    time.sleep(0.05)
    return out


def padded_inflate(key: str, sidecar_key: str | None, etag: str | None = None, size: int | None = None) -> dict:
    with patch.object(lzma, "decompress", _padded_decompress):
        return ingest_fetch.fetch_and_parse(key, sidecar_key, ingest_fetch.fetch_with_retry, etag=etag, size=size)


ISOLATED = False


requires_forkserver = pytest.mark.skipif("forkserver" not in multiprocessing.get_all_start_methods(), reason="forkserver context unavailable")


def bounded_pool_test(function):
    """Run pool controls in a disposable pytest process."""

    @functools.wraps(function)
    def bounded(*args, **kwargs):
        if ISOLATED:
            return function(*args, **kwargs)
        node = function.__module__.removeprefix("tests.")
        target = f"tests/{node}.py::{function.__name__}"
        proc = subprocess.run([sys.executable, "-m", "tests.test_ingest_forkserver", "check", target], capture_output=True, text=True, timeout=120, check=False)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert not proc.stderr, proc.stderr
        return None
    return bounded


def blocked_worker(connection) -> None:
    connection.send({"server": os.getppid(), "worker": os.getpid(), "term": signal.getsignal(signal.SIGTERM), "interrupt": signal.getsignal(signal.SIGINT)})
    connection.close()
    time.sleep(90)


def service() -> None:
    # Challenge the preload's reset: exec preserves an ignored SIGTERM.
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
    # Adopt orphans so waitpid proves server/worker death.
    _subreaper()
    with subprocess.Popen([sys.executable, "-m", "tests.test_ingest_forkserver", "service"], stdout=subprocess.PIPE, text=True) as proc:
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
    """Kill adopted children and let trackers clean their semaphores."""
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
    """Bound the executor owner from an independent process."""
    _subreaper()
    # Nested sessions must not age the outer suite's numbered temp tree.
    with TemporaryDirectory() as temporary:
        proc = subprocess.Popen(  # pylint: disable=consider-using-with
            [sys.executable, "-m", "tests.test_ingest_forkserver", "test", target,
             "--basetemp", temporary],
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


def test_bounded_check_uses_a_private_temporary_tree(monkeypatch):
    proc = Mock(returncode=0)
    proc.communicate.return_value = ("", "")
    proc.poll.return_value = 0

    def starting(args, **kwargs):
        assert Path(args[-1]).is_dir()
        return proc
    start = Mock(side_effect=starting)
    monkeypatch.setattr(subprocess, "Popen", start)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys.modules[__name__], "_subreaper", Mock())
    monkeypatch.setattr(sys.modules[__name__], "_cleanup_children", Mock())
    check("test-target")
    args = start.call_args.args[0]
    assert "--basetemp" in args
    assert not Path(args[args.index("--basetemp") + 1]).exists()


_TRACKER_CLEANUP = re.compile(
    r"[^\n]*/multiprocessing/resource_tracker\.py:\d+: UserWarning: "
    r"resource_tracker: There appear to be 5 leaked semaphore objects "
    r"to clean up at shutdown: \{(?:'/mp-[\w-]+'(?:, )?){5}\}\n"
    r"  warnings\.warn\(\n")


@requires_forkserver
@bounded_pool_test
def test_reused_server_refreshes_configuration(monkeypatch, tmp_path, request):
    # The child consumed check()'s --basetemp, not pytest's numbered tree.
    basetemp = request.config.option.basetemp
    assert basetemp and tmp_path.is_relative_to(Path(basetemp).resolve()), (
        "the nested pytest child did not consume check()'s --basetemp")
    monkeypatch.setenv("R2_ENDPOINT", "file:///first/")
    monkeypatch.setenv("R2_BUCKET", "claude")
    monkeypatch.setenv("CLAUDIT_TIMING", "1")
    monkeypatch.delenv("R2_BLOB_CACHE", raising=False)
    results = []

    def first_pool():
        with parse_pool(1, os.getpid()) as pool:
            results.append(pool.submit(configuration).result(timeout=60))
    thread = threading.Thread(target=first_pool)
    thread.start()
    thread.join(timeout=90)
    assert not thread.is_alive()
    first = results[0]
    assert first["timing"] is True
    assert first["env"]["R2_ENDPOINT"] == "file:///first/"
    assert first["env"]["R2_BLOB_CACHE"] is None
    monkeypatch.setenv("R2_ENDPOINT", "file:///second/")
    monkeypatch.setenv("R2_BLOB_CACHE", str(tmp_path))
    monkeypatch.delenv("R2_BUCKET")
    monkeypatch.delenv("CLAUDIT_TIMING")
    with parse_pool(1, os.getpid()) as pool:
        second = pool.submit(configuration).result(timeout=60)
    assert second["parent"] == first["parent"], "the server was not reused"
    assert second["env"] == {"R2_ENDPOINT": "file:///second/", "R2_BUCKET": None, "R2_BLOB_CACHE": str(tmp_path), "CLAUDIT_TIMING": None}
    assert second["timing"] is False


@requires_forkserver
@bounded_pool_test
def test_worker_rejects_wrong_server_parent(monkeypatch):

    def wrong_parent(**kwargs):
        kwargs["initargs"] = (-1, *kwargs["initargs"][1:])
        return ProcessPoolExecutor(**kwargs)
    monkeypatch.setattr(ingest_workers, "ProcessPoolExecutor", wrong_parent)
    with parse_pool(1, os.getpid()) as pool:
        with pytest.raises(BrokenProcessPool):
            pool.submit(int).result(timeout=60)


@requires_forkserver
@bounded_pool_test
def test_reused_server_refreshes_sdk_configuration(monkeypatch):
    monkeypatch.setenv("R2_ENDPOINT", "https://storage.invalid")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "test-key")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "test-secret")
    monkeypatch.setenv("AWS_CA_BUNDLE", "/tmp/first.pem")
    with parse_pool(1, os.getpid()) as pool:
        first = pool.submit(client_configuration).result(timeout=60)
    assert first["verify"] == "/tmp/first.pem"
    monkeypatch.setenv("AWS_CA_BUNDLE", "/tmp/second.pem")
    with parse_pool(1, os.getpid()) as pool:
        second = pool.submit(client_configuration).result(timeout=60)
    assert second["parent"] == first["parent"]
    assert second["verify"] == "/tmp/second.pem"
    assert second["env"] == "/tmp/second.pem"
    monkeypatch.delenv("AWS_CA_BUNDLE")
    with parse_pool(1, os.getpid()) as pool:
        removed = pool.submit(client_configuration).result(timeout=60)
    assert removed["parent"] == first["parent"]
    assert removed["env"] is None


@requires_forkserver
@pytest.mark.skipif(sys.platform != "linux", reason="Linux parent-death signals")
@pytest.mark.parametrize("mode", ["service-kill", "server-term"])
def test_service_server_worker_death_chain(mode):
    proc = subprocess.run([sys.executable, "-m", "tests.test_ingest_forkserver", mode], capture_output=True, text=True, timeout=120, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not proc.stderr or _TRACKER_CLEANUP.fullmatch(proc.stderr), proc.stderr
    assert json.loads(proc.stdout) == {"server_dead": True, "worker_dead": True, "mode": mode}


@pytest.mark.skipif(sys.platform != "linux", reason="server task count uses Linux procfs")
@requires_forkserver
@bounded_pool_test
def test_parse_pool_starts_with_another_thread_alive(monkeypatch):
    monkeypatch.setattr(ingest_fetch, "parse_process_count", lambda: 1)
    monkeypatch.setattr(ingest_fetch, "persist_thread_count", lambda: 1)
    release = threading.Event()
    thread = threading.Thread(target=release.wait)
    thread.start()
    persisted = []
    try:
        assert thread.is_alive()
        obj = types.SimpleNamespace(key="claude/p/s/s.jsonl", sidecar_key=None, etag="e", size=1)
        with warnings.catch_warnings(record=True) as emitted:
            warnings.simplefilter("always", DeprecationWarning)
            assert ingest_fetch.pipeline_pool(
                [(obj, None, None)], "v", [], None, None, {}, set(), set(),
                server_report_parse,
                lambda _obj, _proj, parsed, *_args: persisted.append(parsed),
                lambda: None) == (1, 0, 0)
        assert not [str(w.message) for w in emitted if issubclass(w.category, DeprecationWarning) and "multi-threaded" in str(w.message)]
        assert persisted[0]["threads"] == 1
        assert persisted[0]["parent"] != os.getpid()
        assert persisted[0]["method"] == "forkserver"
    finally:
        release.set()
        thread.join(timeout=60)


@requires_forkserver
def test_process_pool_timing_line_marks_disjoint_phases(fresh_db, mini_r2_env, monkeypatch, caplog):
    monkeypatch.setenv("INGEST_WORKERS", "1")
    monkeypatch.setenv("INGEST_PARSE_PROCESSES", "2")
    monkeypatch.setenv("INGEST_PERSIST_THREADS", "2")
    monkeypatch.setenv("CLAUDIT_TIMING", "1")
    monkeypatch.setattr(_timing, "TIMING_ON", True)
    with caplog.at_level(logging.INFO, logger="claudit.ingest"):
        summary = ingest.run_ingest("test-proc-timing")
    assert summary["error"] is None
    line = next(r.getMessage() for r in caplog.records if r.name == "claudit.ingest" and r.getMessage().startswith("TIMING ingest "))

    def _ms(field):
        m = re.search(rf"\b{field}=(\d+)ms", line)
        assert m is not None, line
        return int(m.group(1))
    total, parse_ms, persist_ms = (_ms("total"), _ms("fetch_parse"), _ms("persist"))
    assert parse_ms >= 0 and persist_ms >= 0
    assert parse_ms + persist_ms <= total + 100, line
    assert "parse_processes=2" in line
    assert "persist_threads=2" in line
    for part in ("child_fetch", "child_decompress", "child_parse", "child_sidecar", "persist_work", "wait_parse", "wait_persist"):
        assert _ms(part) >= 0, line
    assert _ms("child_parse") > 0, line
    assert _ms("wait_parse") > 0, line
    assert _ms("wait_persist") > 0, line
    assert _ms("persist_work") > 0, line


@requires_forkserver
def test_pool_timing_parts_are_disjoint_slices(fresh_db, mini_r2_env, monkeypatch, caplog):
    monkeypatch.setenv("INGEST_WORKERS", "1")
    monkeypatch.setenv("INGEST_PARSE_PROCESSES", "2")
    monkeypatch.setenv("INGEST_PERSIST_THREADS", "2")
    monkeypatch.setenv("CLAUDIT_TIMING", "1")
    monkeypatch.setattr(_timing, "TIMING_ON", True)
    # Keep SV-FIXTURE-SIZE: one project and its transcript's .xz twin.
    for child in mini_r2_env.iterdir():
        if child.name != "projA":
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    raw = (mini_r2_env / "projA" / "sess-A" / "sess-A.jsonl").read_bytes()
    payload = raw * (1024 * 1024 // len(raw))
    (mini_r2_env / "projA" / "sess-A" / "sess-A.jsonl.xz").write_bytes(lzma.compress(payload))
    # Padding makes inflate dominate GET despite filesystem timing noise.
    monkeypatch.setattr(ingest, "_fetch_and_parse", padded_inflate)
    with caplog.at_level(logging.INFO, logger="claudit.ingest"):
        summary = ingest.run_ingest("test-proc-parts")
    assert summary["error"] is None
    line = next(r.getMessage() for r in caplog.records if r.name == "claudit.ingest" and r.getMessage().startswith("TIMING ingest "))

    def _ms(field):
        m = re.search(rf"\b{field}=(\d+)ms", line)
        assert m is not None, line
        return int(m.group(1))
    assert _ms("child_decompress") > 0, line
    assert _ms("child_fetch") < _ms("child_decompress"), line
    # One chunk puts the final persist drain outside wait_persist (#680).
    assert _ms("wait_persist") == 0, line


def _safe_initializer(monkeypatch) -> types.SimpleNamespace:
    """Stub process effects and restore them through monkeypatch."""
    state = types.SimpleNamespace(
        prctl=Mock(return_value=0), exit=Mock(side_effect=SystemExit(0)),
        close=Mock(wraps=os.close),
        service=types.SimpleNamespace(pid=23, is_alive=Mock(return_value=True)),
        server=types.SimpleNamespace(_forkserver_alive_fd=None))
    state.parent = Mock(return_value=state.service)
    monkeypatch.setattr(ctypes, "CDLL", lambda *_args, **_kw: state)
    monkeypatch.setattr(signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(signal, "signal", Mock())
    monkeypatch.setattr(os, "_exit", state.exit)
    monkeypatch.setattr(os, "getppid", lambda: 17)
    monkeypatch.setattr(os, "close", state.close)
    monkeypatch.setattr(os, "environ", {"OLD_SETTING": "old", "CLAUDIT_TIMING": "0"})
    monkeypatch.setattr(multiprocessing, "parent_process", state.parent)
    monkeypatch.setattr(forkserver, "_forkserver", state.server)
    monkeypatch.setattr(_timing, "TIMING_ON", False)
    return state


def test_initializer_exits_before_service_checks_when_parent_mismatches(monkeypatch):
    state = _safe_initializer(monkeypatch)
    with pytest.raises(SystemExit):
        ingest_workers.parse_worker_init(-1, 23, {"NEW_SETTING": "new"})
    state.exit.assert_called_once_with(0)
    state.parent.assert_not_called()
    assert os.environ == {"OLD_SETTING": "old", "CLAUDIT_TIMING": "0"}


def test_initializer_exits_before_cleanup_when_service_is_gone(monkeypatch):
    state = _safe_initializer(monkeypatch)
    state.service.is_alive.return_value = False
    with pytest.raises(SystemExit):
        ingest_workers.parse_worker_init(17, 23, {"NEW_SETTING": "new"})
    state.exit.assert_called_once_with(0)
    state.close.assert_not_called()
    assert os.environ == {"OLD_SETTING": "old", "CLAUDIT_TIMING": "0"}


def test_initializer_arms_parent_death_and_closes_alive_fd(monkeypatch):
    state = _safe_initializer(monkeypatch)
    descriptor = os.open(os.devnull, os.O_RDONLY)
    setattr(state.server, "_forkserver_alive_fd", descriptor)
    try:
        ingest_workers.parse_worker_init(17, 23)
        state.prctl.assert_called_once_with(1, 9, 0, 0, 0)
        with pytest.raises(OSError) as error:
            os.fstat(descriptor)
        assert error.value.errno == errno.EBADF
        assert getattr(state.server, "_forkserver_alive_fd") is None
    finally:
        if call(descriptor) not in state.close.call_args_list:
            state.close(descriptor)


def test_initializer_replaces_environment_and_rereads_timing(monkeypatch):
    _safe_initializer(monkeypatch)
    snapshot = {"NEW_SETTING": "new", "CLAUDIT_TIMING": "1"}
    ingest_workers.parse_worker_init(17, environment=snapshot)
    assert os.environ == {"NEW_SETTING": "new", "CLAUDIT_TIMING": "1"}
    assert _timing.TIMING_ON is True


def test_forkserver_preload_restores_default_sigterm():
    original = signal.getsignal(signal.SIGTERM)
    try:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        importlib.reload(importlib.import_module("backend.ingest_forkserver"))
        assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL
    finally:
        signal.signal(signal.SIGTERM, original)


if __name__ == "__main__":
    if sys.argv[1] == "service":
        service()
    elif sys.argv[1] == "check":
        check(sys.argv[2])
    elif sys.argv[1] == "test":
        controls = importlib.import_module("tests.test_ingest_forkserver")
        setattr(controls, "ISOLATED", True)
        raise SystemExit(pytest.main([sys.argv[2], "-q", "--tb=short", *sys.argv[3:]]))
    else:
        probe(sys.argv[1])
