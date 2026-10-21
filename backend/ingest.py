"""R2 → Postgres ingest.

Per-file granularity: every object key `key_layout.classify()` accepts —
across EVERY configured bucket (CLAUDIT_R2_BUCKET names one or more, joined by
'+') → one row in `files`, keyed by the bucket-qualified
`<bucket>/<object-key>`, + N rows in `records` (per Phase-1-deduped
record). Cross-file uuid dedup resolves at ingest into records.is_canonical; reads filter the flag.

Reparse trigger per FILE: missing row, changed etag, parser version or
collection policy — never a NEWER stored parser_version: an older binary's
ingest rewrites each file's rows with its own column list, which would
NULL every column the older binary does not know. Orphan
files (R2 key gone) are deleted. CASCADE drops records.

Per-session work spans TWO transactions:
  1. DELETE FROM records WHERE file_id=... + INSERT INTO files (UPSERT)
     + bulk INSERT INTO records — all in one transaction so a crash
     mid-loop leaves either the old state or the new state.
  2. (Implicit) The orphan delete + projects upserts also commit in
     their own scopes; partial progress is fine because the sessions
     row's etag is only updated when the per-file txn lands.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import partial

import psycopg

from backend import flags, ingest_collection, collection_rollups
from backend.flags import CollectionPolicy
from backend import blob_cache, cache, constants, db, events, ingest_fetch, ingest_vacuum, key_layout, lane_projects, parse, parser_version, r2, timing  # pylint: disable=unused-import
from backend.ingest_fetch import (  # noqa: F401  (re-export)
    fetch_with_retry as _fetch_with_retry,
    parse_process_count, persist_thread_count,
    pipeline_pool as _pipeline_pool, pipeline_threads as _pipeline_threads,
    record_failure as _record_failure, resolve as _resolve, worker_count,
)
from backend.ingest_persist import _persist  # noqa: F401  (re-export)
from backend.ingest_runs import (  # noqa: F401  (re-export)  # pylint: disable=unused-import
    _ABORT_ERROR, _close_run, _open_run, _open_run_swept, close_open_run,
    failed_public_keys, failure_summary, sweep_stale_runs,
)
# The listing scan (split for size; the tests reach these through ingest)
# and the lock-loss guard the bounded steps consult.
from backend.ingest_scan import (  # noqa: F401  (re-export)  # pylint: disable=unused-import
    _Wire, _fetch_marker, _resolve_project_paths, _scan_objects,
)
from backend import ingest_lockwatch
from backend.ingest_lockwatch import (  # noqa: F401  (re-export)  # pylint: disable=unused-import
    _INGEST_LOCK_KEY, _db_run_lock,
)
from backend.ingest_reprice import IngestAborted, reprice_stale  # noqa: F401  (re-export)
from backend.ingest_timing import (  # noqa: F401  (re-export)
    _RUN_TIMING, _RunTiming, _record_phase, _record_scope, _timed_step,
)
from backend.ingest_walk import (  # noqa: F401  (re-export)  # pylint: disable=unused-import
    _stored_version_is_newer, _track_project, _track_walked_project,
)
# Re-export canonical helpers for ingest callers and test patch seams;
# _rebuild_derived_state resolves them through this module.
from backend.ingest_rollups import (  # noqa: F401  (re-export)
    purge_suppressed, rebuild_agent_rollup, rebuild_ctx_cost_rollup,
    rebuild_dispatch_brief_rollup, rebuild_dispatch_rollup,
    rebuild_latency_rollup, rebuild_rollup, rebuild_tool_error_rollup,
    rebuild_tool_rollup, rebuild_web_metrics_rollup, recompute_canonical,
    resolve_teammate_agent_types,
)
from backend.ingest_scope import (  # noqa: F401  (re-export)
    begin_scope, current_scope, finish_scope, mark_complete,
)
from backend.project_aliases import rekey_folded_projects
# The run-row module owns the public-facing error formatter.
# These imports preserve the existing `backend.ingest` call surface.
from backend.ingest_warm import WARM_RANGES, warm_common  # noqa: F401  (re-export)  # pylint: disable=unused-import
from backend.ingest_progress import _set_progress, progress_snapshot  # noqa: F401  (re-export)  # pylint: disable=unused-import
# Re-export orphan sweeps so the bare-name
# callers above and any test patching `backend.ingest._delete_orphans`
# keep resolving through this module's globals.
from backend.ingest_orphans import (  # noqa: F401  (re-export)
    _delete_orphan_projects, _delete_orphans,
)

log = logging.getLogger("claudit.ingest")

# Permit one ingest at a time: hourly ticks can overlap startup or a
# full reparse, duplicating GETs, parses, writes and rollup rebuilds.
# A second run's todo can also predate the first run's commits. Acquire
# non-blockingly: skipping is correct, and the next tick resumes work.
_RUN_LOCK = threading.Lock()

# Lifespan teardown requests cooperative abort. Bounded steps raise
# IngestAborted instead of waiting for SIGKILL. Atomic per-file writes
# leave derived state convergent: the next successful run rebuilds it.
_SHUTDOWN = threading.Event()


def request_shutdown() -> None:
    """Ask any in-flight run to stop at its next bounded step, and every
    later run to skip. Called from lifespan teardown."""
    _SHUTDOWN.set()


def clear_shutdown() -> None:
    """Revoke a shutdown request once it can no longer serve a purpose.

    Lifespan teardown calls this AFTER the bounded wait, when nothing can
    start a run in this process any more: a shutdown request must not
    outlive the teardown it belongs to, or it poisons whatever else
    shares this interpreter.
    """
    _SHUTDOWN.clear()


def _check_shutdown() -> None:
    """Raise IngestAborted on requested shutdown or lost db-wide lock.

    Consult only at bounded steps."""
    if _SHUTDOWN.is_set():
        raise IngestAborted("shutdown requested")
    ingest_lockwatch.check_lock_alive()


def _shutdown_cancel(exc: BaseException) -> bool:
    """Whether `exc` is a statement the teardown's cancellation ended.

    The bounded steps cannot fire while a run sits inside one long
    single-statement phase — the clean restamp, a rollup rebuild — so
    lifespan teardown cancels the pool's in-flight statements after
    asking the run to stop. The driver surfaces the server's
    cancel as QueryCanceled; with the shutdown request in force that
    cancel IS the abort arriving, and the run closes as aborted, not
    fatal.
    """
    return (_SHUTDOWN.is_set()
            and isinstance(exc, psycopg.errors.QueryCanceled))


TRANSIENT_FETCH_ERRORS = ingest_fetch.TRANSIENT_FETCH_ERRORS
CORRUPT_PAYLOAD_ERRORS = ingest_fetch.CORRUPT_PAYLOAD_ERRORS
FETCH_BACKOFF_S = ingest_fetch.FETCH_BACKOFF_S
FETCH_ATTEMPTS = ingest_fetch.FETCH_ATTEMPTS
VanishedObject = ingest_fetch.VanishedObject
FatalFetchError = ingest_fetch.FatalFetchError
ClientError = ingest_fetch.ClientError
BotoCoreError = ingest_fetch.BotoCoreError


@contextmanager
def _run_lock_nonblocking():
    """Acquire _RUN_LOCK without blocking, as a context manager.

    Yields False when another run holds the lock — a skipped run is
    correct behaviour, not an error; the next hourly tick picks up
    whatever is left.
    """
    acquired = _RUN_LOCK.acquire(blocking=False)
    try:
        yield acquired
    finally:
        if acquired:
            _RUN_LOCK.release()


def _skip(trigger: str, reason: str) -> dict:
    return {"skipped": True, "reason": reason, "trigger": trigger}


def run_ingest(trigger: str) -> dict:
    if _SHUTDOWN.is_set():
        log.warning("ingest (%s) skipped: shutdown requested", trigger)
        return _skip(trigger, "shutdown requested")
    with _run_lock_nonblocking() as acquired:
        if not acquired:
            log.warning(
                "ingest (%s) skipped: another run is still in flight", trigger
            )
            return _skip(trigger, "ingest already running")
        with _db_run_lock() as db_acquired:
            if not db_acquired:
                log.warning(
                    "ingest (%s) skipped: another instance is still running",
                    trigger,
                )
                return _skip(trigger, "ingest already running (another instance)")
            if _SHUTDOWN.is_set():  # landed while waiting on the locks
                log.warning("ingest (%s) skipped: shutdown requested", trigger)
                return _skip(trigger, "shutdown requested")
            return run_ingest_locked(trigger)


def wait_for_run(timeout: float) -> bool:
    """Bounded wait for an in-flight run to release _RUN_LOCK.

    True when the lock is free (idle, or the run finished inside the
    timeout), False when one still holds it. The timeout keeps lifespan
    teardown inside the unit's stop window.
    """
    # A timeout'd acquire has no `with` form; this release IS its finally.
    if _RUN_LOCK.acquire(timeout=timeout):  # pylint: disable=consider-using-with
        _RUN_LOCK.release()
        return True
    return False


def _existing_files() -> dict:
    return ingest_collection.existing_files()


def _collect_todo(existing: dict, parser_version_value: str,
                  failed: list[tuple[str, str]]) -> tuple:
    """Walk the bucket: count objects, remember live keys, and queue the
    files whose source/code/collection policy needs (re)parsing.

    Returns (listed, todo, seen_keys, newer, moved). todo holds (obj, proj,
    stored) per file needing work, fetched+parsed later on a pool. newer
    counts the files NOT queued because their stored parser_version is
    newer than this binary's own — a rollback's older binary must not
    rewrite rows it cannot write whole (see _stored_version_is_newer).
    What a key maps to lives in key_layout: the walk keeps only the keys
    classify() accepts as transcripts; session/is_main come out of the
    same classify() in _persist, and the PROJECT id is resolved once,
    here —
    marker slug, stored mapping, or bare hash (lane_projects.resolve_lane_project) —
    so the walk and every _persist of the run agree on it.

    Marker bodies are fetched before the todo loop so a lane project's
    display_name is settled by the time _track_project runs — the same
    scan → resolve → plan shape codexmeter's ingest uses.
    """
    # pylint: disable=too-many-locals
    wire_objs, marker_items = _scan_objects()
    with _timed_step("markers"):
        project_paths = _resolve_project_paths(
            marker_items, worker_count(), failed
        )
    with _timed_step("lane_ids"):
        stored_lane = lane_projects.stored_lane_ids()
        moved = lane_projects.rekey_stale_lane_projects(
            project_paths, stored_lane)
        if moved and (scope := current_scope()) is not None:
            scope.promote_full("lane-project identity move")
    with _timed_step("plan"):
        listed, todo, seen_keys, newer = ingest_collection.collect_wires(
            wire_objs, existing, parser_version_value, flags.CONFIG.collection,
            partial(_track_walked_project, project_paths=project_paths,
                    stored_lane=stored_lane))
    current = _RUN_TIMING.get()
    if current is not None:
        current.todo = len(todo)
    return listed, todo, seen_keys, newer, moved


def _fetch_parse_persist(todo: list[tuple], parser_version_value: str,
                         failed: list[tuple[str, str]],
                         seen_keys: set[str]) -> tuple[int, int, int]:
    """Dispatch the chosen pipeline with this module's seams. The
    mechanics live in ingest_fetch (module-size baseline); the seams stay
    here so tests patching `ingest.*` keep working.
    """
    return ingest_fetch.fetch_parse_persist(
        todo, parser_version_value, failed, seen_keys,
        parse_call=partial(_fetch_and_parse, collection=flags.CONFIG.collection),
        persist_call=_persist_one,
        check_shutdown=_check_shutdown)


def _persist_one(obj, proj, parsed, parser_version_value,
                 current: _RunTiming | None):
    """The unit submitted to the persist pool: one `_persist` (one file,
    one transaction), timed into the run phases. Runs on a pool thread;
    `ingest._persist` resolves from module globals at call time, so the
    test seam keeps working.
    """
    started = time.perf_counter()
    try:
        _persist(obj, proj, parsed, parser_version_value)
    finally:
        if current is not None:
            with current.persist_lock:
                current.persist_seconds += time.perf_counter() - started
    return obj


def _rebuild_derived_state() -> int:
    """Canonical flags and teammate roles, then the rollups that read them.

    Each bounded phase checks for shutdown; an abort skips later phases and
    the next run rebuilds. Returns rows changed by mutating phases.
    """
    # Order matters: suppression removes rows the canonical pass would
    # otherwise rank, the alias fold re-keys identity first, and the rollups
    # read is_canonical and agent_type. The names resolve through this
    # module's globals, so a test can monkeypatch any phase on `ingest`.
    reprice = partial(reprice_stale, should_stop=_check_shutdown)
    phases: tuple[tuple[str, Callable[[], int]], ...] = (
        ("suppressed", purge_suppressed),
        ("reprice", reprice),
        ("aliases", rekey_folded_projects),
        ("canonical", partial(recompute_canonical, should_stop=_check_shutdown)),
        ("teammates", resolve_teammate_agent_types),
        ("usage_rollup", rebuild_rollup),
        ("tool_rollup", rebuild_tool_rollup),
        ("tool_error_rollup", rebuild_tool_error_rollup),
        ("dispatch_rollup", rebuild_dispatch_rollup),
        ("dispatch_brief_rollup", rebuild_dispatch_brief_rollup),
        ("latency_rollup", rebuild_latency_rollup),
        ("ctx_cost_rollup", rebuild_ctx_cost_rollup),
        ("agent_rollup", rebuild_agent_rollup),
        # The only phase reading a table no other phase writes.
        ("web_metrics_rollup", rebuild_web_metrics_rollup),
    )
    disabled = collection_rollups.disabled_rollups(flags.CONFIG.collection)
    _check_shutdown()
    changed = collection_rollups.clear_disabled_rollups(disabled)
    phases = collection_rollups.collected_phases(phases, disabled)
    scope = current_scope()
    if scope is not None:
        scope.check_latency_null()
    for phase, rebuild in phases:
        _check_shutdown()
        if phase == "usage_rollup" and scope is not None:
            scope.start_rollups()
        with _timed_step(phase):
            _set_progress(phase=phase)
            rows = ingest_vacuum.note_phase(phase, rebuild() or 0)
        if phase == "reprice" and rows and scope is not None:
            scope.promote_full("reprice changed records")
        if phase == "canonical" and scope is not None:
            scope.check_latency_null()
        changed += rows if phase in ("suppressed", "reprice", "aliases", "canonical", "teammates") else 0
        current = _RUN_TIMING.get()
        if current is not None:
            current.changed = changed
    if scope is not None:
        scope.check_dirty_threshold(scope.dirty_files)
        scope.check_latency_null()
    # Post-ingest VACUUM: outside every transaction, once
    # the rollup rebuild has finished — before the run closes, so
    # finished_at still marks the run's work as complete. The run's own
    # stop guard is consulted inside; a failure is booked as a summary
    # warning, never as the run's error.
    with _timed_step("vacuum"):
        ingest_vacuum.vacuum_after_run(check_shutdown=_check_shutdown)
    return changed


def _walk_and_persist(parser_version_value: str,
                      failed: list[tuple[str, str]]
                      ) -> tuple[int, int, int, int, int, int, int]:
    """The fallible body of a run: list, fetch+parse+persist, orphan sweep.

    Returns the six walk counts plus the lane re-key's moved-file count.
    Exceptions propagate to run_ingest_locked, which books them as the
    run-level `fatal` — except IngestAborted, which closes the run as
    aborted.
    """
    existing_started = time.perf_counter()
    existing = _existing_files()
    existing_seconds = time.perf_counter() - existing_started
    # Defer its mark to preserve the walk's reported order without moving SQL.
    try:
        listed, todo, seen_keys, newer, lane_moved = _collect_todo(
            existing, parser_version_value, failed
        )
    finally:
        _record_phase("existing", existing_seconds)
    _check_shutdown()
    inserted, reparsed, vanished = _fetch_parse_persist(
        todo, parser_version_value, failed, seen_keys
    )
    _check_shutdown()
    with _timed_step("orphans"):
        deleted = _delete_orphans(seen_keys)
    with _timed_step("orphan_projects"):
        _delete_orphan_projects()
    with _timed_step("blob_cache_prune"):
        blob_cache.prune()
    _check_shutdown()
    if (scope := current_scope()) is not None:
        scope.check_dirty_threshold(scope.dirty_files)
        scope.check_latency_null()
    return listed, inserted, reparsed, deleted, vanished, newer, lane_moved


def run_ingest_locked(trigger: str) -> dict:
    """Run ingest under one phase-timing context.

    The context-local holder preserves every helper's existing signature;
    tests and downstream instrumentation call and patch those helpers.
    The delegate owns the original run lifecycle, including cache warming
    and the progress reset, while this wrapper emits the aggregate only
    after that lifecycle returns or raises.

    Partial counts survive abort and fatal walk paths. The outcome defaults
    to fatal until the normal path sets a more specific result, so a failure
    before summary construction still emits a useful terminal line. With
    `CLAUDIT_TIMING` off, the delegate runs without a timing context.
    """
    if not timing.TIMING_ON:
        try:
            return _run_ingest_locked(trigger)
        finally:
            finish_scope()
    phases = timing.Phases("ingest", logger=log, account=True)
    current = _RunTiming(phases)
    current.parse_processes = parse_process_count()
    current.persist_threads = persist_thread_count()
    token = _RUN_TIMING.set(current)
    summary: dict | None = None
    try:
        summary = _run_ingest_locked(trigger)
        return summary
    finally:
        scope = finish_scope()
        if scope is not None:
            used_full_scope = (scope.rollups_full if scope.rollups_full is not None
                               else scope.full)
            current.scope = "full" if used_full_scope else "incremental"
        _RUN_TIMING.reset(token)
        try:
            phases.done(
                listed=summary["r2_listed"] if summary is not None else 0,
                todo=current.todo,
                inserted=summary["inserted"] if summary is not None else 0,
                reparsed=summary["reparsed"] if summary is not None else 0,
                deleted=summary["deleted"] if summary is not None else 0,
                changed=current.changed,
                outcome=current.outcome,
                scope=current.scope,
                parse_processes=current.parse_processes,
                persist_threads=current.persist_threads,
            )
        except BaseException:
            # Instrumentation must not change what the run returns or raises.
            pass


def _run_ingest_locked(trigger: str) -> dict:  # pylint: disable=too-many-locals,too-many-statements,too-many-branches
    with _timed_step("open_run"):
        started, run_id = _open_run_swept(trigger)
        _set_progress(phase="listing", done=0, total=0, run_id=run_id, started_at=started.isoformat())
        ingest_vacuum.begin_run()
        listed = inserted = reparsed = deleted = vanished = newer = changed = lane_moved = 0
        # Per-object failures (qualified key, message). Counted in `error`,
        # whose public /health surface cannot carry keys.
        # Retained for `_record_failure` logs and the authenticated
        # `failed_keys` response field after SSE serialization. They do not
        # gate the run: an object failure is a pending retry, not a failed run.
        failed: list[tuple[str, str]] = []
        # A whole-run exception, which DOES gate the post-passes below.
        fatal = None
        # A shutdown request honoured mid-run. Like `fatal`, it
        # gates the post-passes; unlike it, the row closes saying "aborted".
        # The cause rides the IngestAborted message.
        aborted = False
        abort_msg = "shutdown requested"

    try:
        with _timed_step("scope"):
            scope = begin_scope()
        _record_scope("full" if scope.full else "incremental")
        listed, inserted, reparsed, deleted, vanished, newer, lane_moved = (
            _walk_and_persist(parser_version.PARSER_VERSION, failed))
        ingest_vacuum.note_files(inserted + reparsed + deleted + lane_moved)
    except IngestAborted as exc:
        abort_msg = str(exc) or "shutdown requested"
        log.warning("ingest (%s): aborted, %s", trigger, abort_msg)
        aborted = True
    except Exception as e:  # noqa: BLE001
        if _shutdown_cancel(e):
            log.warning(
                "ingest (%s): aborted, statement cancelled by the shutdown",
                trigger)
            aborted = True
        else:
            # Log full exception details, but store only a static type-bearing
            # message for public /health. Object keys in exceptions must not cross
            # that boundary; logs retain the original message and cause chain.
            log.exception("ingest (%s): fatal, run aborted", trigger)
            fatal = f"{type(e).__name__}: details are in the server log"

    if newer:
        log.warning(
            "ingest (%s): skipped reparse of %d file(s) whose stored "
            "parser_version is newer than this binary's own", trigger, newer)

    # `error` reports whole-run trouble plus abort and controls post-passes.
    # Per-object trouble becomes a count because /health is public; key
    # details remain in logs and the admin response.
    err: str | None
    if aborted:
        err = f"aborted: {abort_msg}"
    elif fatal is not None:
        err = fatal
    else:
        err = failure_summary(failed)

    # The rebuild runs BEFORE the run is closed: finished_at is
    # the signal that the rollups and canonical flags now describe what
    # THIS run persisted, so a reader that waits on it never lands on the
    # previous run's aggregates. Gated on `fatal` and `aborted`, NOT on
    # `err`: the derived state describes whatever `records` now holds, so
    # skipping it because one object out of a thousand could not be
    # fetched would leave the rollups describing the PREVIOUS dataset. An
    # aborted run skips it too — the walk it interrupted is incomplete, so
    # a rebuild would describe a half-written dataset. A rebuild failure
    # is more severe than any per-object failure summary, so it books
    # itself as the run's error — and the run still closes, so the next
    # run can start.
    if fatal is None and not aborted:
        try:
            changed = _rebuild_derived_state()
        except IngestAborted as exc:
            abort_msg = str(exc) or "shutdown requested"
            log.warning(
                "ingest (%s): aborted during the derived-state rebuild, %s",
                trigger, abort_msg)
            aborted = True
            err = f"aborted: {abort_msg}"
        except Exception as e:  # noqa: BLE001
            if _shutdown_cancel(e):
                log.warning(
                    "ingest (%s): aborted during the derived-state "
                    "rebuild, statement cancelled by the shutdown", trigger)
                aborted = True
                err = f"aborted: {abort_msg}"
            else:
                log.exception(
                    "ingest (%s): fatal, derived-state rebuild failed",
                    trigger)
                fatal = f"{type(e).__name__}: details are in the server log"
                err = fatal

    changed += lane_moved  # a lane re-key counts at the gate like a derived phase

    with _timed_step("close_run"):
        finished = datetime.now(timezone.utc)
        _close_run(run_id, finished, listed, reparsed, inserted, deleted,
                   newer, err)
        summary = {
            "id": run_id,
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "trigger": trigger,
            "r2_listed": listed,
            "inserted": inserted,
            "reparsed": reparsed,
            "deleted": deleted,
            "failed": len(failed),
            "vanished": vanished,
            "newer": newer,
            "aborted": aborted,
            "error": err,
        }

    # On data change, mark cache entries stale and notify SSE clients to
    # refetch; invalidate preserves servable values and refreshes them in
    # the background. Count derived-only changes too: reprice, purge and
    # alias folds change stored data without touching files. Threadsafe;
    # aborted runs skip.
    if fatal is None and not aborted and any((inserted, reparsed, deleted, changed)):
        with _timed_step("notify"):
            cache.response_cache.invalidate()
            events.broadcast_threadsafe("ingest_done", summary)

    if fatal is None and not aborted:
        with _timed_step("warm"):
            _set_progress(phase="warming")
            warm_common()
    with _timed_step("finish"):
        _set_progress(phase="idle", done=0, total=0)
        # Authenticated triage only (/admin/ingest response). The broadcast
        # above serializes eagerly (json.dumps inside broadcast_threadsafe),
        # so these fields never reach the guest-visible SSE payload; ingest_runs.error is count-only at the
        # source. Keep these assignments here to preserve that boundary.
        summary["failed_keys"] = failed_public_keys(failed)
        if (vacuum_warning := ingest_vacuum.take_warning()) is not None:
            summary["vacuum_warning"] = vacuum_warning
        if fatal is None and not aborted and not failed:
            mark_complete()
    if (current := _RUN_TIMING.get()) is not None:
        current.outcome = "aborted" if aborted else "ok" if fatal is None else "fatal"
        current.changed = changed
    return summary


def _fetch_and_parse(key: str, sidecar_key: str | None = None,
                     etag: str | None = None, size: int | None = None, *,
                     collection: CollectionPolicy | None = None) -> dict:
    return ingest_fetch.fetch_and_parse(key, sidecar_key, _fetch_with_retry,
                                        parse.parse_file, etag=etag, size=size,
                                        collection=collection)
