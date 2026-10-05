"""Worker concurrency knobs and fork hygiene for the ingest pipelines.

Split out of `ingest_fetch` for the module-size ratchet; `ingest_fetch`
re-exports these names so the established call surface — `ingest`'s
seam re-exports and the tests' monkeypatch seams — keeps resolving.
Stdlib-only, so `ingest_fetch` may import this module freely.
"""
from __future__ import annotations

import os
import signal


def worker_count() -> int:
    """Fetch+parse thread concurrency of the IN-PROCESS pipeline.

    Unset or unparseable -> auto (network-bound work, so oversubscribe
    cores). An explicit number is honoured, clamped to at least 1, so
    INGEST_WORKERS=1 is a real "go sequential" switch for debugging.
    With INGEST_PARSE_PROCESSES>1 the fetch runs inside the parse
    children and this knob governs only the markers pool.
    """
    auto = min(16, (os.cpu_count() or 4) * 2)
    raw = os.environ.get("INGEST_WORKERS", "").strip()
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
    threads regressed parse wall 1.63x against a serial run. Forked
    children get real parallelism. Unset or unparseable -> auto =
    min(8, max(1, cpu//2)); an explicit integer is honoured, clamped to
    >= 1; INGEST_PARSE_PROCESSES=1 is the in-process pipeline.
    """
    auto = min(8, max(1, (os.cpu_count() or 4) // 2))
    raw = os.environ.get("INGEST_PARSE_PROCESSES", "").strip()
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
    raw = os.environ.get("INGEST_PERSIST_THREADS", "").strip()
    if not raw:
        return 4
    try:
        return max(1, int(raw))
    except ValueError:
        return 4


def parse_worker_init(parent_pid: int) -> None:
    """Keep a forked parse worker from outliving the service (issue #373).

    A fork inherits uvicorn's SIGTERM handler, which only sets a flag the
    worker never checks — so a worker ignored SIGTERM and survived the
    service, holding its port, database sessions and the ingest advisory
    lock until killed by hand. Three defences, each best-effort so a
    worker on a platform missing one still parses:

    - SIGTERM/SIGINT restored to SIG_DFL: a systemd control-group stop
      (SIGTERM to the cgroup) kills the worker outright;
    - the parent-death signal (prctl PR_SET_PDEATHSIG, SIGKILL): the
      worker dies with the work even when nothing sent it a SIGTERM
      (a bare uvicorn whose supervisor kills the main PID only);
    - the ppid check: PDEATHSIG is armed after fork, so a parent that
      died inside that window left an orphan — a worker whose parent is
      not the one that forked it exits immediately.
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
