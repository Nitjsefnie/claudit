"""Worker concurrency knobs and process hygiene for the ingest pipelines.

Split out of `ingest_fetch` for the module-size ratchet; `ingest_fetch`
re-exports these names so the established call surface — `ingest`'s
seam re-exports and the tests' monkeypatch seams — keeps resolving.
Stdlib-only at import, so `ingest_fetch` may import this module freely.
"""
from __future__ import annotations

import importlib
import os
import signal
import multiprocessing
from concurrent.futures import ProcessPoolExecutor

# CPython's forkserver handles identify the OS parent and its alive pipe.
# pylint: disable=protected-access
_PRELOAD_MODULES = ("backend.ingest_forkserver", "backend.ingest")


def _worker_environment() -> dict[str, str]:
    return os.environ.copy()


def parse_pool(max_workers: int, service_pid: int) -> ProcessPoolExecutor:
    """Fork from a preloaded, single-threaded server reused across pools.

    The service's complete current environment travels in initargs,
    not the server's environment snapshot. CPython's private server PID
    identifies the actual OS parent for the #373 orphan race check.
    """
    context = multiprocessing.get_context("forkserver")
    context.set_forkserver_preload(list(_PRELOAD_MODULES))
    # Loaded on first pool build: importing backend.ingest keeps master's module set.
    forkserver = importlib.import_module("multiprocessing.forkserver")
    forkserver.ensure_running()
    return ProcessPoolExecutor(
        max_workers=max_workers,
        mp_context=context,
        initializer=parse_worker_init,
        initargs=(getattr(forkserver._forkserver, "_forkserver_pid"), service_pid,
                  _worker_environment()))

from backend.env_config import getenv


def worker_count() -> int:
    """Fetch+parse thread concurrency of the IN-PROCESS pipeline.

    Unset or unparseable -> auto (network-bound work, so oversubscribe
    cores). An explicit number is honoured, clamped to at least 1, so
    CLAUDIT_INGEST_WORKERS=1 is a real "go sequential" switch for debugging.
    With CLAUDIT_INGEST_PARSE_PROCESSES>1 the fetch runs inside the parse
    children and this knob governs only the markers pool.
    """
    auto = min(16, (os.cpu_count() or 4) * 2)
    raw = getenv("CLAUDIT_INGEST_WORKERS", "").strip()
    if not raw:
        return auto
    try:
        return max(1, int(raw))
    except ValueError:
        return auto


def parse_process_count() -> int:
    """Parse-process concurrency of the process-pool pipeline.

    The parse is pure-Python CPU work, so the in-process thread pool is
    GIL-serialised — and measured on the private restore (issue #309), 16
    threads regressed parse wall 1.63x against a serial run. Process
    children get real parallelism. Unset or unparseable -> auto =
    min(8, max(1, cpu//2)); an explicit integer is honoured, clamped to
    >= 1; CLAUDIT_INGEST_PARSE_PROCESSES=1 is the in-process pipeline.
    """
    auto = min(8, max(1, (os.cpu_count() or 4) // 2))
    raw = getenv("CLAUDIT_INGEST_PARSE_PROCESSES", "").strip()
    if not raw:
        return auto
    try:
        return max(1, int(raw))
    except ValueError:
        return auto


def persist_thread_count() -> int:
    """Persist-thread concurrency of the process-pool pipeline.

    Each thread runs the unchanged `_persist` — one file, one
    transaction, drawn from the shared viz pool (max_size 20, shared
    with API traffic), so the default stays modest. Unset or
    unparseable -> 4; an explicit integer is honoured, clamped to >= 1.
    """
    raw = getenv("CLAUDIT_INGEST_PERSIST_THREADS", "").strip()
    if not raw:
        return 4
    try:
        return max(1, int(raw))
    except ValueError:
        return 4


def parse_worker_init(parent_pid: int, service_pid: int | None = None,
                      environment: dict[str, str] | None = None) -> None:
    """Keep a parse worker from outliving its server or service (#373).

    Workers reset inherited handlers, arm PDEATHSIG on the forkserver,
    and verify both the OS parent and multiprocessing's service sentinel.
    Closing the inherited server-alive pipe makes service death exit the
    server, which kills its workers: parse units never create subprocesses
    and need no forkserver client descriptor of their own.

    - SIGTERM/SIGINT restored to SIG_DFL: a systemd control-group stop
      (SIGTERM to the cgroup) kills the worker outright;
    - the parent-death signal (prctl PR_SET_PDEATHSIG, SIGKILL): the
      worker dies with the server even when nothing sent it a SIGTERM
      (a bare uvicorn whose supervisor kills the main PID only);
    - the ppid check: PDEATHSIG is armed after process start, so a parent that
      died inside that window left an orphan — a worker whose parent is
      not the one that started it exits immediately.
    """
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, signal.SIG_DFL)
        except (OSError, ValueError):  # pragma: no cover - no signal context
            pass
    try:
        import ctypes  # pylint: disable=import-outside-toplevel

        ctypes.CDLL("libc.so.6", use_errno=True).prctl(
            1, signal.SIGKILL, 0, 0, 0)  # 1 = PR_SET_PDEATHSIG
    except Exception:  # noqa: BLE001  - best-effort; non-Linux or no libc
        pass
    if os.getppid() != parent_pid:
        os._exit(0)
    if service_pid is not None:
        service = multiprocessing.parent_process()
        if service is None or service.pid != service_pid or not service.is_alive():
            os._exit(0)
        # CPython passes this write end to every client, workers included.
        # Leaving it open would keep the server alive after service SIGKILL.
        forkserver = importlib.import_module("multiprocessing.forkserver")
        alive_fd = getattr(forkserver._forkserver, "_forkserver_alive_fd")
        if alive_fd is not None:
            os.close(alive_fd)
        setattr(forkserver._forkserver, "_forkserver_alive_fd", None)
    if environment is not None:
        os.environ.clear()
        os.environ.update(environment)
        # Import-time implementation choices (botocore CRT/retry defaults,
        # psycopg's implementation) remain service-start settings. Runtime
        # SDK configuration, including CA/proxy additions/removals, is fresh.
        from backend import timing  # pylint: disable=import-outside-toplevel
        timing.TIMING_ON = os.environ.get("CLAUDIT_TIMING", "").lower() not in (
            "", "0", "false", "no")
