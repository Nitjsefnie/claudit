"""Bounded object fetch and per-file parsing for the ingest pool.

Also owns the ingest run's cooperative-abort state and the bounded
fetch+parse and persist pipelines (issue #309): the fetch machinery,
the pools, and the per-file unit callables live together so this module
stays leaf-ward of `ingest`, which imports and re-exports these names
to keep its established call surface.
"""
from __future__ import annotations

import logging
import multiprocessing
import os
import signal
import time
import lzma
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool, ProcessPoolExecutor

from botocore.exceptions import BotoCoreError, ClientError

from backend import agent_sidecar, db, parse, r2
from backend.ingest_reprice import IngestAborted
from backend.ingest_scope import capture_and_add, capture_contributions, current_scope
from backend.ingest_progress import _set_progress
from backend.ingest_timing import _RUN_TIMING, _RunTiming

log = logging.getLogger("claudit.ingest")

TRANSIENT_FETCH_ERRORS = (OSError, BotoCoreError, ClientError)
CORRUPT_PAYLOAD_ERRORS = (lzma.LZMAError, EOFError)
FETCH_BACKOFF_S = (0.5, 1.0)
FETCH_ATTEMPTS = len(FETCH_BACKOFF_S) + 1


class VanishedObject(Exception):
    """A listed transcript that disappeared before its fetch ran."""


class FatalFetchError(Exception):
    """A non-transient fetch failure that indicates a code defect."""


def is_missing(exc: BaseException) -> bool:
    """Whether a fetch error says the object no longer exists."""
    if isinstance(exc, FileNotFoundError):
        return True
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code")
        return code in ("NoSuchKey", "404")
    return False


def fetch_with_retry(key: str) -> bytes:
    """Retry transient object-store failures; never retry bad payloads."""
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            return r2.get_object(key)
        except CORRUPT_PAYLOAD_ERRORS:
            raise
        except TRANSIENT_FETCH_ERRORS as exc:
            if is_missing(exc):
                raise VanishedObject(key) from exc
            if attempt == FETCH_ATTEMPTS:
                raise
            log.warning(
                "ingest: fetch of %s failed (attempt %d/%d), retrying",
                key, attempt, FETCH_ATTEMPTS,
            )
            time.sleep(FETCH_BACKOFF_S[attempt - 1])
        except Exception as exc:  # noqa: BLE001
            log.error("ingest: fatal fetch failure on %s", key)
            raise FatalFetchError(
                f"{type(exc).__name__} while fetching an object; "
                "details are in the server log") from exc
    raise AssertionError("unreachable")  # pragma: no cover


def fetch_and_parse(key: str, sidecar_key: str | None,
                    fetch: Callable[[str], bytes],
                    parse_file: Callable[[str, bytes], dict] | None = None
                    ) -> dict:
    """Fetch and parse one object without opening a database connection."""
    parsed = (parse.parse_file if parse_file is None else parse_file)(
        key, fetch(key))
    if sidecar_key is None or parsed["agent_type_in_band"]:
        return parsed
    try:
        sidecar = fetch(sidecar_key)
    except (VanishedObject, *CORRUPT_PAYLOAD_ERRORS):
        return parsed
    return agent_sidecar.apply_agent_sidecar(
        parsed, sidecar, r2.split_key(key)[1])


def record_failure(failed: list[tuple[str, str]], key: str,
                   exc: BaseException) -> None:
    """Book one failed object and retain its qualified key for triage.

    `_record_failure` logs the key for server-side investigation. The
    failure list feeds the admin-only response field; `ingest_runs.error`
    receives `failure_summary`'s count because /health is public.
    """
    failed.append((key, f"{type(exc).__name__}: {exc}"))
    log.warning(
        "ingest: %s failed after %d attempt(s): %s: %s",
        key, FETCH_ATTEMPTS, type(exc).__name__, exc,
    )


def resolve(items: list, call, workers: int) -> list[tuple]:
    """Run `call(item)` over `items`, pairing each with its result OR its
    exception instead of letting the first failure escape.

    Sequential when workers == 1, on a pool otherwise. Collecting with
    `[f.result() for f in as_completed(...)]` re-raised the worker's
    exception out of the collection step, which aborted the whole ingest
    AND discarded every already-fetched result alongside it. The two shapes
    have to behave identically, which is easiest to guarantee with one
    implementation.

    FatalFetchError is the one exception that still escapes: it means the
    fetch is broken rather than one object being unlucky, so it belongs to
    the run, not to the item.

    Returns [(item, result, None) | (item, None, exception)].
    """
    outcomes: list[tuple] = []
    if workers == 1:
        for item in items:
            try:
                outcomes.append((item, call(item), None))
            except FatalFetchError:
                raise
            except Exception as e:  # noqa: BLE001
                outcomes.append((item, None, e))
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(call, item): item for item in items}
            for f in as_completed(futures):
                item = futures[f]
                try:
                    outcomes.append((item, f.result(), None))
                except FatalFetchError:
                    raise
                except Exception as e:  # noqa: BLE001
                    outcomes.append((item, None, e))
    return outcomes


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
      worker dies with the process even when nothing sent it a SIGTERM
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


def pipeline_threads(todo: list[tuple], parser_version: str,
                     failed: list[tuple[str, str]],
                     current: _RunTiming | None, scope,
                     old_contributions: dict,
                     persisted_keys: set[str],
                     seen_keys: set[str],
                     parse_call: Callable, persist_call: Callable,
                     check_shutdown: Callable) -> tuple[int, int, int]:
    # pylint: disable=too-many-arguments,too-many-positional-arguments
    """The in-process pipeline: thread-pool fetch+parse in bounded chunks,
    persist inline on the calling thread (one file per transaction).
    Behaviour and phase accounting are exactly the pre-pool behaviour.
    """
    # pylint: disable=too-many-locals,too-many-branches
    inserted = 0
    reparsed = 0
    vanished = 0
    workers = worker_count()
    chunk = max(1, workers * 4)
    for start in range(0, len(todo), chunk):
        check_shutdown()
        for (obj, proj, stored), parsed, exc in resolve(
            todo[start:start + chunk],
            lambda it: parse_call(it[0].key, it[0].sidecar_key),
            workers,
        ):
            if isinstance(exc, VanishedObject):
                log.info("ingest: %s vanished between list and fetch", obj.key)
                seen_keys.discard(obj.key)
                vanished += 1
                continue
            if exc is not None:
                record_failure(failed, obj.key, exc)
                continue
            persist_call(obj, proj, parsed, parser_version, current)
            if scope is not None and not scope.full:
                scope.add_contributions(old_contributions, {obj.key})
                persisted_keys.add(obj.key)
            if stored is None:
                inserted += 1
            else:
                reparsed += 1
            _set_progress(done=inserted + reparsed)
    return inserted, reparsed, vanished


def fetch_parse_persist(todo: list[tuple], parser_version: str,
                        failed: list[tuple[str, str]],
                        seen_keys: set[str],
                        parse_call: Callable, persist_call: Callable,
                        check_shutdown: Callable) -> tuple[int, int, int]:
    """Fetch+parse the queued files and persist as results arrive.

    Two pipelines, chosen by parse_process_count(): 1 = the in-process
    thread pipeline (the GIL serialises the pure-Python parse; the test
    suite pins this), >1 = the process pool (the parse is real CPU work,
    so forked children scale with cores where threads actively regress —
    measured 1.63x slower than serial at 16 threads). Both share the
    unit of work, the failure booking, and the per-file transaction
    boundary; the equality test pins their DB state together. Chunking
    bounds memory; each chunk's persist drain runs while the next
    chunk's parses execute.

    `parse_call`/`persist_call`/`check_shutdown` are this module's host's
    seams, passed in so the mechanics stay leaf-ward of `ingest`.

    A VanishedObject is discarded from `seen_keys` so the orphan sweep
    treats the key as one the listing never showed.

    Returns (inserted, reparsed, vanished).
    """
    # pylint: disable=too-many-locals
    inserted = 0
    reparsed = 0
    vanished = 0
    scope = current_scope()
    todo_keys = {obj.key for obj, _, _ in todo}
    old_contributions = {}
    if scope is not None and not scope.full and todo_keys:
        with db.viz_conn() as conn:
            old_contributions = capture_contributions(scope, conn, todo_keys)
    persisted_keys: set[str] = set()
    current = _RUN_TIMING.get()
    fetch_parse_started = time.perf_counter() if current is not None else None
    _set_progress(phase="parsing", total=len(todo), done=0)
    try:
        if parse_process_count() > 1:
            inserted, reparsed, vanished = pipeline_pool(
                todo, parser_version, failed, current, scope,
                old_contributions, persisted_keys, seen_keys,
                parse_call, persist_call, check_shutdown)
        else:
            inserted, reparsed, vanished = pipeline_threads(
                todo, parser_version, failed, current, scope,
                old_contributions, persisted_keys, seen_keys,
                parse_call, persist_call, check_shutdown)
    finally:
        if current is not None and fetch_parse_started is not None:
            if current.last_parse_done is not None:
                parse_wall = current.last_parse_done - fetch_parse_started
                current.phases.mark("fetch_parse", parse_wall)
                current.phases.mark(
                    "persist", time.perf_counter() - current.last_parse_done)
            else:
                current.phases.mark(
                    "fetch_parse",
                    time.perf_counter() - fetch_parse_started
                    - current.persist_seconds)
                current.phases.mark("persist", current.persist_seconds)
    if scope is not None and not scope.full and persisted_keys:
        with db.viz_conn() as conn:
            capture_and_add(scope, conn, persisted_keys)
        scope.check_latency_null()
    return inserted, reparsed, vanished


def resolve_futures(futures: dict) -> Iterator[tuple]:
    """Iterate a prefabricated futures map, pairing each future's item
    with its result OR its exception — the same shape `resolve` returns,
    over a caller-owned executor whose lifetime spans chunks.

    FatalFetchError still escapes: it means the fetch path is broken, so
    it belongs to the run, not to the item. A dead parse child
    (BrokenProcessPool — OOM, kill) means the same and escapes too.
    """
    for f in as_completed(futures):
        item = futures[f]
        try:
            yield (item, f.result(), None)
        except (FatalFetchError, BrokenProcessPool):
            raise
        except Exception as e:  # noqa: BLE001
            yield (item, None, e)


def parse_wire(item: tuple, parse_call: Callable):
    """The unit submitted to the parse process pool: fetch and parse one
    wire (and its sidecar) in a forked child. `parse_call` is pickled with
    the item (a module-level attr of an importable module — the fork
    inherits any patch in force); the child touches no database state.
    """
    obj, _proj, _stored = item
    return parse_call(obj.key, obj.sidecar_key)


def pipeline_pool(todo: list[tuple], parser_version: str,
                  failed: list[tuple[str, str]],
                  current: _RunTiming | None, scope,
                  old_contributions: dict,
                  persisted_keys: set[str],
                  seen_keys: set[str],
                  parse_call: Callable, persist_call: Callable,
                  check_shutdown: Callable) -> tuple[int, int, int]:
    # pylint: disable=too-many-arguments,too-many-positional-arguments
    """The process-pool pipeline: forked children fetch+parse, a bounded
    pool of threads persists. Chunks still bound memory; each chunk's
    persist drain runs while the NEXT chunk's parses execute, so neither
    stage idles on the other and no thread is a serial floor.

    Per-file failure isolation is per stage: a parse error or a persist
    error books its file via record_failure and the run continues.
    BrokenProcessPool and FatalFetchError mean the fetch path itself is
    broken, so they belong to the run and escape.

    Phase accounting: fetch_parse is the wall until the last parse
    completion, persist the drain after it — disjoint, so the TIMING
    line's sum never exceeds its total.
    """
    # pylint: disable=too-many-locals,too-many-branches,too-many-statements
    inserted = 0
    reparsed = 0
    vanished = 0
    processes = parse_process_count()
    persist_workers = persist_thread_count()
    chunk = max(1, processes * 4)

    def drain(persist_futures: dict, stored_by_key: dict) -> None:
        nonlocal inserted, reparsed
        for pfuture in as_completed(persist_futures):
            pobj = persist_futures[pfuture]
            try:
                pfuture.result()
            except Exception as e:  # noqa: BLE001
                record_failure(failed, pobj.key, e)
                continue
            if scope is not None and not scope.full:
                persisted_keys.add(pobj.key)
            if stored_by_key[pobj.key] is None:
                inserted += 1
            else:
                reparsed += 1
            _set_progress(done=inserted + reparsed)

    # fork is pinned: children inherit the caller's patched seams (the
    # property the failure-injection tests rely on) and the pickled parse
    # unit resolves module attributes the same way the parent would. The
    # forked child can log (the retry warning); a fork of a multithreaded
    # parent carries a small deadlock risk on an inherited logging lock,
    # accepted here because children log at most a few lines per run.
    with ProcessPoolExecutor(
            max_workers=processes,
            mp_context=multiprocessing.get_context("fork"),
            initializer=parse_worker_init,
            initargs=(os.getpid(),)) as parse_pool, \
            ThreadPoolExecutor(max_workers=persist_workers) as persist_pool:
        # The previous chunk's (persist futures, stored map), drained while
        # this chunk's parses run — the drain costs no parse throughput.
        pending: tuple[dict, dict] | None = None
        try:
            for start in range(0, len(todo), chunk):
                # Checked BEFORE the previous chunk's drain: on abort the
                # in-flight persists still execute during the with-block
                # shutdown and land in the DB, but their counts are never
                # booked — the run closes aborted with partial counts, which
                # is the documented abort contract; the next run converges.
                check_shutdown()
                items = todo[start:start + chunk]
                stored_by_key = {o.key: stored for o, _p, stored in items}
                persist_futures: dict = {}
                parse_futures = {
                    parse_pool.submit(parse_wire, it, parse_call): it
                    for it in items}
                if pending is not None:
                    drain(*pending)
                pending = None
                for item, parsed, exc in resolve_futures(parse_futures):
                    obj, proj, _stored = item
                    if current is not None:
                        current.last_parse_done = time.perf_counter()
                    if exc is not None:
                        if isinstance(exc, VanishedObject):
                            log.info(
                                "ingest: %s vanished between list and fetch",
                                obj.key)
                            seen_keys.discard(obj.key)
                            vanished += 1
                        else:
                            record_failure(failed, obj.key, exc)
                        continue
                    if scope is not None and not scope.full:
                        scope.add_contributions(old_contributions, {obj.key})
                    persist_futures[persist_pool.submit(
                        persist_call, obj, proj, parsed, parser_version,
                        current)] = obj
                pending = (persist_futures, stored_by_key)
            if pending is not None:
                drain(*pending)
        except IngestAborted:
            # Abort mid-chunk: drop the queued parses and let the pools
            # stop behind their current items. The workers themselves die
            # on the service's stop (parse_worker_init); the run closes
            # aborted and the next run converges.
            parse_pool.shutdown(wait=False, cancel_futures=True)
            persist_pool.shutdown(wait=False, cancel_futures=True)
            raise
    return inserted, reparsed, vanished
