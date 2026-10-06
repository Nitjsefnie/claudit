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
import threading
import time
import lzma
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures.process import ProcessPoolExecutor

from botocore.exceptions import BotoCoreError, ClientError

from backend import agent_sidecar, blob_cache, db, parse, r2
from backend.ingest_reprice import IngestAborted
from backend.ingest_resolve import (  # noqa: F401  (re-export)
    FETCH_ATTEMPTS, FETCH_BACKOFF_S, FatalFetchError, VanishedObject,
    is_missing, record_failure, resolve, resolve_futures,
)
from backend.ingest_scope import capture_and_add, capture_contributions, current_scope
from backend.ingest_progress import _set_progress
from backend.ingest_timing import (
    _RUN_TIMING, _RunTiming, _emit_fetch_parse_parts,
)
from backend.ingest_workers import (
    parse_process_count, parse_worker_init, persist_thread_count,
    worker_count,
)

log = logging.getLogger("claudit.ingest")

TRANSIENT_FETCH_ERRORS = (OSError, BotoCoreError, ClientError)
CORRUPT_PAYLOAD_ERRORS = (lzma.LZMAError, EOFError)

# The pool child's per-file stage accumulator (issue #662): parse_wire
# plants a dict here before calling the parse unit, and fetch_and_parse
# writes this file's child work into it — the channel is per-thread, so
# the parse_call seam stays a plain (key, sidecar_key) callable and keeps
# resolving the patched `ingest` seams inside the forked child. The
# parent's pipeline_pool sums the returned stages into _RunTiming.
_STAGES = threading.local()


def fetch_with_retry(key: str, etag: str | None = None,
                     size: int | None = None) -> bytes:
    """Retry transient object-store failures; never retry bad payloads.

    `etag`/`size`, when given, switch this GET to the deploy's disk blob
    cache (R2_BLOB_CACHE): r2.get_object answers a hit without the round
    trip and stores a miss after it. The unit passes them only when the
    cache is enabled, so a patched fetch callable keeping the original
    (key) shape keeps working with the cache off — the bench's memory
    reader included.
    """
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            return (r2.get_object(key, etag, size)
                    if etag is not None else r2.get_object(key))
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
                    fetch: Callable,
                    parse_file: Callable[[str, bytes], dict] | None = None,
                    stages: dict[str, float] | None = None,
                    etag: str | None = None,
                    size: int | None = None) -> dict:
    """Fetch and parse one object without opening a database connection.

    `stages` books this file's child work (issue #662): the main fetch
    wall, the xz inflate it paid (popped from r2's per-thread stamp), the
    parse, and the sidecar fetch when one runs. It is inert when None —
    every caller but the timed pool child, which plants the accumulator
    on _STAGES because the `parse_call` seam's fixed shape cannot carry
    the dict through.

    `etag`/`size` carry the listing identity for the deploy's disk blob
    cache (issue #684): when the cache is enabled they are forwarded to
    the fetch callable, whose cache-aware shape is fetch(key, etag, size)
    — r2.get_object answers a hit without the GET. Without them, or with
    the cache off, the original fetch(key) shape is spoken. The sidecar
    fetch stays etag-less either way: it rides one joined etag with its
    transcript, and refetching a tiny sidecar costs the round trip alone.
    """
    if stages is None:
        stages = getattr(_STAGES, "stages", None)
    acc: dict[str, float] | None = stages
    fetch_started = None
    if acc is not None:
        # A forked child inherits the forking thread's threadlocal copy,
        # stamp included: drop anything inherited so the first timed
        # file's decompress booking is this child's own work (M-2).
        r2.pop_decompress_seconds()
        fetch_started = time.perf_counter()
    data = (fetch(key, etag, size)
            if etag is not None and blob_cache.enabled() else fetch(key))
    parse_started = (time.perf_counter()
                     if fetch_started is not None else None)
    parsed = (parse.parse_file if parse_file is None else parse_file)(
        key, data)
    if (acc is not None and parse_started is not None
            and fetch_started is not None):
        now = time.perf_counter()
        acc["child_parse"] = now - parse_started
        acc["child_fetch"] = parse_started - fetch_started
        acc["child_decompress"] = r2.pop_decompress_seconds()
    if sidecar_key is None or parsed["agent_type_in_band"]:
        return parsed
    sidecar_started = (time.perf_counter()
                       if fetch_started is not None else None)
    try:
        sidecar = fetch(sidecar_key)
    except (VanishedObject, *CORRUPT_PAYLOAD_ERRORS):
        return parsed
    finally:
        if acc is not None and sidecar_started is not None:
            acc["child_sidecar"] = time.perf_counter() - sidecar_started
    return agent_sidecar.apply_agent_sidecar(
        parsed, sidecar, r2.split_key(key)[1])


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
            lambda it: parse_call(it[0].key, it[0].sidecar_key,
                                  it[0].etag, it[0].size),
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
                # Issue #662: the pool wall's parts, emitted as breakdown
                # figures so the sum/gap accounting stays wall-only.
                _emit_fetch_parse_parts(current.phases, current)
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


def parse_wire(item: tuple, parse_call: Callable,
               timed: bool = False) -> dict | tuple[dict, dict[str, float]]:
    """The unit submitted to the parse process pool: fetch and parse one
    wire (and its sidecar) in a forked child. `parse_call` is pickled with
    the item (a module-level attr of an importable module — the fork
    inherits any patch in force); the child touches no database state.

    With `timed` (the run carries a timing context, issue #662), the unit
    plants the stage accumulator the parse unit's fetch_and_parse writes
    into, returns (parsed, stages) — the per-file child work — and always
    clears the accumulator. Without it, the bare parsed dict, the shape
    the persist submission has always expected.
    """
    obj, _proj, _stored = item
    if not timed:
        return parse_call(obj.key, obj.sidecar_key, obj.etag, obj.size)
    _STAGES.stages = {}
    try:
        parsed = parse_call(obj.key, obj.sidecar_key, obj.etag, obj.size)
        return parsed, _STAGES.stages
    finally:
        _STAGES.stages = None


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
    line's sum never exceeds its total. With a timing context in force,
    the wall's parts are also accumulated onto the run (issue #662):
    the child work parse_wire's stage dicts report, summed across the
    children; the persist work persist_seconds already sums; and this
    thread's two blocking points — waiting on parse results and draining
    persists — so one reparse's line says where its time went. The parts
    are breakdown figures (mark_part), never phases in the sum.
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
        drain_started = time.perf_counter() if current is not None else None
        try:
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
        finally:
            if current is not None and drain_started is not None:
                current.wait_persist += time.perf_counter() - drain_started

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
        timed = current is not None
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
                    parse_pool.submit(parse_wire, it, parse_call, timed): it
                    for it in items}
                if pending is not None:
                    drain(*pending)
                pending = None
                wait_started = (time.perf_counter()
                                if current is not None else None)
                for item, result, exc in resolve_futures(parse_futures):
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
                    if timed:
                        parsed, stages = result
                        for label, seconds in stages.items():
                            current.child_work[label] = (
                                current.child_work.get(label, 0.0)
                                + seconds)
                    else:
                        parsed = result
                    if scope is not None and not scope.full:
                        scope.add_contributions(old_contributions, {obj.key})
                    persist_futures[persist_pool.submit(
                        persist_call, obj, proj, parsed, parser_version,
                        current)] = obj
                # No finally: a fatal escape (FatalFetchError /
                # BrokenProcessPool) abandons the run, so the chunk's
                # partial wait is not worth a nesting level the
                # suppression baseline refuses.
                if current is not None and wait_started is not None:
                    current.wait_parse += time.perf_counter() - wait_started
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
