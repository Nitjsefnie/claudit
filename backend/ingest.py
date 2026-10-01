"""R2 → Postgres ingest.

Per-file granularity: every object key `key_layout.classify()` accepts —
across EVERY configured bucket (R2_BUCKET names one or more, joined by
'+') → one row in `files`, keyed by the bucket-qualified
`<bucket>/<object-key>`, + N rows in `records` (per Phase-1-deduped
record). Cross-file uuid dedup resolves at ingest into records.is_canonical; reads filter the flag.

Reparse trigger per FILE: row missing OR etag changed OR parser_version
mismatch — never a NEWER stored parser_version: an older binary's
ingest rewrites each file's rows with its own column list, which would
NULL every column the older binary does not know (issue #118). Orphan
files (R2 key gone) are deleted. CASCADE drops records.

Per-session work spans TWO transactions:
  1. DELETE FROM records WHERE file_key=... + INSERT INTO files (UPSERT)
     + bulk INSERT INTO records — all in one transaction so a crash
     mid-loop leaves either the old state or the new state.
  2. (Implicit) The orphan delete + projects upserts also commit in
     their own scopes; partial progress is fine because the sessions
     row's etag is only updated when the per-file txn lands.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import partial

import psycopg

from backend import cache, constants, db, events, ingest_fetch, key_layout, lane_projects, parse, r2, timing
from backend.ingest_fetch import (  # noqa: F401  (re-export)
    parse_process_count, persist_thread_count,
    pipeline_pool as _pipeline_pool, pipeline_threads as _pipeline_threads,
    record_failure as _record_failure, resolve as _resolve, worker_count,
)
from backend.ingest_persist import _persist  # noqa: F401  (re-export)
from backend.ingest_runs import (  # noqa: F401  (re-export)  # pylint: disable=unused-import
    _ABORT_ERROR, _close_run, _open_run, close_open_run, failed_public_keys,
    failure_summary, sweep_stale_runs,
)
# The listing scan (split for size; the tests reach these through ingest)
# and the lock-loss guard the bounded steps consult (issue #374).
from backend.ingest_scan import (  # noqa: F401  (re-export)  # pylint: disable=unused-import
    _Wire, _fetch_marker, _resolve_project_paths, _scan_objects,
)
from backend import ingest_lockwatch
from backend.ingest_reprice import IngestAborted, reprice_stale  # noqa: F401  (re-export)
from backend.ingest_timing import (  # noqa: F401  (re-export)
    _RUN_TIMING, _RunTiming, _record_phase, _record_scope, _timed_step,
)
from backend.ingest_walk import (  # noqa: F401  (re-export)  # pylint: disable=unused-import
    _stored_version_is_newer, _track_project, _track_walked_project,
)
# Re-exported so `ingest.recompute_canonical(...)` and friends keep
# resolving after the split; _rebuild_derived_state is their caller.
from backend.ingest_rollups import (  # noqa: F401  (re-export)
    purge_suppressed, rebuild_agent_rollup, rebuild_ctx_cost_rollup,
    rebuild_dispatch_brief_rollup,
    rebuild_dispatch_rollup, rebuild_latency_rollup, rebuild_rollup,
    rebuild_tool_error_rollup, rebuild_tool_rollup,
    recompute_canonical, resolve_teammate_agent_types,
)
from backend.ingest_scope import (  # noqa: F401  (re-export)
    begin_scope, current_scope,
    finish_scope, mark_complete,
)
from backend.project_aliases import rekey_folded_projects
# The run-row module owns the public-facing error formatter.
# These imports preserve the existing `backend.ingest` call surface.
from backend.ingest_warm import WARM_RANGES, warm_common  # noqa: F401  (re-export)  # pylint: disable=unused-import
from backend.ingest_progress import _set_progress, progress_snapshot  # noqa: F401  (re-export)  # pylint: disable=unused-import
# Orphan sweeps moved to their own module; re-exported so the bare-name
# callers above and any test patching `backend.ingest._delete_orphans`
# keep resolving through this module's globals.
from backend.ingest_orphans import (  # noqa: F401  (re-export)
    _delete_orphan_projects, _delete_orphans,
)

log = logging.getLogger("claudit.ingest")

# Only one ingest at a time. The hourly cron fires regardless of whether a
# previous run is still going, and a full reparse (PARSER_VERSION bump) takes
# ~40min — so a cron landed on top of a startup reparse and both walked the
# whole bucket: duplicate R2 GETs, duplicate parses, competing writes, and two
# sets of rollup rebuilds. The second run also built its todo list before the
# first had committed anything, so it redid work already done.
# Non-blocking: a skipped run is correct behaviour, not an error — the next
# hourly tick picks up whatever is left.
_RUN_LOCK = threading.Lock()

# Advisory-lock key for the db-wide ingest lock (_db_run_lock below). A
# sibling of db._SCHEMA_LOCK_KEY (0x5356_4D49) — same prefix, next value —
# so the schema migration and an ingest run can never take each other's
# lock on one database.
_INGEST_LOCK_KEY = 0x5356_4D4A

# Cooperative abort (issue #103): a SIGTERM during a run used to wait out
# the whole ingest until systemd SIGKILLed mid-run. Lifespan teardown sets
# this event; a run checks it between bounded steps and unwinds via
# IngestAborted. Per-file transactions are atomic, so the DB stays
# convergent: derived state is left stale and rebuilt by the next run.
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
    """Raise IngestAborted if shutdown was requested or the db-wide ingest
    lock was lost (bounded steps only; issues #103, #374)."""
    if _SHUTDOWN.is_set():
        raise IngestAborted("shutdown requested")
    ingest_lockwatch.check_lock_alive()


def _shutdown_cancel(exc: BaseException) -> bool:
    """Whether `exc` is a statement the teardown's cancellation ended.

    The bounded steps cannot fire while a run sits inside one long
    single-statement phase — the clean restamp, a rollup rebuild — so
    lifespan teardown cancels the pool's in-flight statements after
    asking the run to stop (issue #372). The driver surfaces the server's
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


@contextmanager
def _db_run_lock() -> Iterator[bool]:
    """Try the db-wide ingest advisory lock on a DEDICATED connection.

    _RUN_LOCK above serialises one process; two app instances sharing one
    database still ran their ingests concurrently (issue #102: two runs
    2.4 ms apart, one rollup rebuild UniqueViolation, two ingest_runs
    rows). pg_try_advisory_lock is session-scoped, so the server releases
    it when the holding connection dies — a crashed instance can never
    leave the ingest locked.

    The connection exists ONLY to hold the lock: opened here, closed in
    the outer finally on every exit path (success, fatal error, and any
    abort unwinding through the yield), so no pooled connection ever
    returns to the pool with the lock still held.

    Yields False when another instance holds it — a skipped run is correct
    behaviour, not an error. A connect failure propagates, the same as
    today's behaviour when the database is down.
    """
    conn = psycopg.Connection.connect(
        os.environ["DATABASE_URL_VIZ"], autocommit=True)
    try:
        row = conn.execute(  # pylint: disable=no-member
            "SELECT pg_try_advisory_lock(%s)", (_INGEST_LOCK_KEY,)
        ).fetchone()
        assert row is not None  # SELECT always yields exactly one row
        acquired = bool(row[0])
        ingest_lockwatch.hold(conn, _INGEST_LOCK_KEY)
        try:
            yield acquired
        finally:
            ingest_lockwatch.release()
            if acquired:
                # Issue #154: the unlock must never mask the body's error;
                # the swallow leaves no stale lock either way — the expected
                # failure is the lock session dying mid-run, where the
                # server releases a session-scoped advisory lock at session
                # death, and a still-live session is ended by the close()
                # in the outer finally on every exit path regardless.
                try:
                    conn.execute(  # pylint: disable=no-member
                        "SELECT pg_advisory_unlock(%s)", (_INGEST_LOCK_KEY,))
                except Exception as exc:
                    log.warning(
                        "ingest advisory-lock unlock failed; the lock is "
                        "released server-side when the lock session dies, "
                        "and a still-live session is ended by the close() "
                        "in the outer finally; any in-flight run error is "
                        "preserved: %s", exc)
    finally:
        conn.close()  # pylint: disable=no-member


def _skip(trigger: str, reason: str) -> dict:
    """The skip dict for a run that declines to start; no run row."""
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
    """file_key → (etag, parser_version) for reparse decisions."""
    with db.viz_conn() as c:
        return {
            row[0]: (row[1], row[2])
            for row in c.execute(
                "SELECT file_key, r2_etag, parser_version FROM files"
            ).fetchall()
        }


def _collect_todo(existing: dict, parser_version: str,
                  failed: list[tuple[str, str]]) -> tuple:
    """Walk the bucket: count objects, remember live keys, and queue the
    files whose etag/parser_version says they need (re)parsing.

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
    listed = 0
    seen_keys: set[str] = set()
    seen_projects: dict[str, dict] = {}
    todo: list[tuple] = []
    newer = 0
    with _timed_step("lane_ids"):
        stored_lane = lane_projects.stored_lane_ids()
        moved = lane_projects.rekey_stale_lane_projects(
            project_paths, stored_lane)
        if moved and (scope := current_scope()) is not None:
            scope.promote_full("lane-project identity move")
    with _timed_step("plan"):
        for obj in wire_objs:
            info = key_layout.classify(r2.split_key(obj.key)[1])
            if info is None:  # pragma: no cover - the scan kept only transcripts
                continue
            listed += 1
            seen_keys.add(obj.key)
            tracked = _track_walked_project(
                seen_projects, info, obj, project_paths, stored_lane)

            stored = existing.get(obj.key)
            if (stored is None or stored[0] != obj.etag
                    or stored[1] != parser_version):
                if _stored_version_is_newer(stored, parser_version):
                    newer += 1
                    continue
                todo.append((obj, tracked, stored))
    current = _RUN_TIMING.get()
    if current is not None:
        current.todo = len(todo)
    return listed, todo, seen_keys, newer, moved


def _fetch_parse_persist(todo: list[tuple], parser_version: str,
                         failed: list[tuple[str, str]],
                         seen_keys: set[str]) -> tuple[int, int, int]:
    """Dispatch the chosen pipeline with this module's seams. The
    mechanics live in ingest_fetch (module-size baseline); the seams stay
    here so tests patching `ingest.*` keep working.
    """
    return ingest_fetch.fetch_parse_persist(
        todo, parser_version, failed, seen_keys,
        parse_call=_fetch_and_parse, persist_call=_persist_one,
        check_shutdown=_check_shutdown)


def _persist_one(obj, proj, parsed, parser_version,
                 current: _RunTiming | None):
    """The unit submitted to the persist pool: one `_persist` (one file,
    one transaction), timed into the run phases. Runs on a pool thread;
    `ingest._persist` resolves from module globals at call time, so the
    test seam keeps working.
    """
    started = time.perf_counter()
    try:
        _persist(obj, proj, parsed, parser_version)
    finally:
        if current is not None:
            with current.persist_lock:
                current.persist_seconds += time.perf_counter() - started
    return obj


def _rebuild_derived_state() -> int:
    """Canonical flags and teammate roles, then the rollups that read them.

    Each bounded phase checks for shutdown. An abort skips later phases; the
    next successful run rebuilds. Returns rows changed by mutating phases.
    """
    # Order matters: suppression removes rows the canonical pass would
    # otherwise rank, the alias fold re-keys identity first, and the
    # rollups read is_canonical and agent_type. The names resolve through
    # this module's globals at call time, so a test can monkeypatch any
    # phase on `ingest` itself; the reprice partial carries should_stop.
    reprice = partial(reprice_stale, should_stop=_check_shutdown)
    phases: tuple[tuple[str, Callable[[], int]], ...] = (
        ("suppressed", purge_suppressed),
        ("reprice", reprice),
        ("aliases", rekey_folded_projects),
        ("canonical", recompute_canonical),
        ("teammates", resolve_teammate_agent_types),
        ("usage_rollup", rebuild_rollup),
        ("tool_rollup", rebuild_tool_rollup),
        ("tool_error_rollup", rebuild_tool_error_rollup),
        ("dispatch_rollup", rebuild_dispatch_rollup),
        ("dispatch_brief_rollup", rebuild_dispatch_brief_rollup),
        ("latency_rollup", rebuild_latency_rollup),
        ("ctx_cost_rollup", rebuild_ctx_cost_rollup),
        ("agent_rollup", rebuild_agent_rollup),
    )
    changed = 0
    scope = current_scope()
    if scope is not None:
        scope.check_latency_null()
    for phase, rebuild in phases:
        _check_shutdown()
        if phase == "usage_rollup" and scope is not None:
            scope.start_rollups()
        with _timed_step(phase):
            _set_progress(phase=phase)
            rows = rebuild() or 0
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
    return changed


def _walk_and_persist(parser_version: str,
                      failed: list[tuple[str, str]]
                      ) -> tuple[int, int, int, int, int, int, int]:
    """The fallible body of a run: list, fetch+parse+persist, orphan sweep.

    Returns the six walk counts plus the lane re-key's moved-file count (#370).
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
            existing, parser_version, failed
        )
    finally:
        _record_phase("existing", existing_seconds)
    _check_shutdown()
    inserted, reparsed, vanished = _fetch_parse_persist(
        todo, parser_version, failed, seen_keys
    )
    _check_shutdown()
    with _timed_step("orphans"):
        deleted = _delete_orphans(seen_keys)
    with _timed_step("orphan_projects"):
        _delete_orphan_projects()
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
        # Crash recovery (issue #440): under the advisory lock, an open
        # row's opener is provably dead, so close its row before booking
        # our own — the SIGKILL shape the graceful paths never see.
        swept = sweep_stale_runs()
        if swept:
            log.warning(
                "ingest (%s): closed %d ingest_runs row(s) the previous "
                "process left open (it died without a graceful stop)",
                trigger, swept)
        started = datetime.now(timezone.utc)
        run_id = _open_run(started, trigger)
        _set_progress(phase="listing", done=0, total=0, run_id=run_id, started_at=started.isoformat())
        listed = inserted = reparsed = deleted = vanished = newer = changed = lane_moved = 0
        # Per-object failures (qualified key, message). Counted in `error`,
        # whose public /health surface cannot carry keys (issue #253).
        # Retained for `_record_failure` logs and the authenticated
        # `failed_keys` response field after SSE serialization. They do not
        # gate the run: one dropped connection out of 9,213 files is still
        # a retry pending, not a failed run.
        failed: list[tuple[str, str]] = []
        # A whole-run exception, which DOES gate the post-passes below.
        fatal = None
        # A shutdown request honoured mid-run (issue #103). Like `fatal`, it
        # gates the post-passes; unlike it, the row closes saying "aborted".
        # The cause rides the IngestAborted message (issue #374).
        aborted = False
        abort_msg = "shutdown requested"

    try:
        with _timed_step("scope"):
            scope = begin_scope()
        _record_scope("full" if scope.full else "incremental")
        listed, inserted, reparsed, deleted, vanished, newer, lane_moved = (
            _walk_and_persist(constants.PARSER_VERSION, failed))
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
            # Full exception details go to the logs; only a static
            # type-bearing message is stored and served by public /health,
            # so a key embedded in exception text cannot cross the boundary
            # (issue #253). The logged traceback retains the original
            # message and cause chain.
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

    # The rebuild runs BEFORE the run is closed (issue #42): finished_at is
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

    changed += lane_moved  # a lane re-key (#370) counts at the gate like a derived phase

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

    # Data changed: mark the response cache stale, then notify connected SSE
    # clients so the dashboard re-fetches. invalidate(), not clear(): entries
    # stay servable while stale (clear() dropped every reader onto the 8s+
    # uncached path; the refetch refreshes them in the background). The gate
    # adds the derived-state count (issues #256, #341): a reprice-, purge- or
    # alias-fold-only run changes stored data without touching a file. Threadsafe; aborted runs skip.
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
        # so this field never reaches the guest-visible SSE payload (issue
        # #253); ingest_runs.error is count-only at the source. Keep this
        # assignment here to preserve that serialization boundary.
        summary["failed_keys"] = failed_public_keys(failed)
        if fatal is None and not aborted:
            mark_complete()
    if (current := _RUN_TIMING.get()) is not None:
        current.outcome = "aborted" if aborted else "ok" if fatal is None else "fatal"
        current.changed = changed
    return summary


def _fetch_with_retry(key: str) -> bytes:
    """Keep the ingest-level monkeypatch seam over the extracted fetcher."""
    return ingest_fetch.fetch_with_retry(key)


def _fetch_and_parse(key: str, sidecar_key: str | None = None) -> dict:
    """Run the extracted parser while preserving the patched fetch callback."""
    return ingest_fetch.fetch_and_parse(
        key, sidecar_key, _fetch_with_retry, parse.parse_file)
