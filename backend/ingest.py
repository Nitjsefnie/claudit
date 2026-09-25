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

import json
import logging
import lzma
import os
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import NamedTuple

import psycopg
from botocore.exceptions import BotoCoreError, ClientError

from backend import agent_sidecar, cache, constants, db, events, key_layout, lane_markers, lane_projects, parse, r2
from backend.ingest_persist import _persist  # noqa: F401  (re-export)
# Re-exported so `ingest.recompute_canonical(...)` and friends keep
# resolving after the split; _rebuild_derived_state below is their
# only in-module caller.
from backend.ingest_rollups import (  # noqa: F401  (re-export)
    purge_suppressed, rebuild_agent_rollup, rebuild_ctx_cost_rollup,
    rebuild_dispatch_brief_rollup,
    rebuild_dispatch_rollup, rebuild_latency_rollup, rebuild_rollup,
    rebuild_tool_error_rollup, rebuild_tool_rollup,
    recompute_canonical, resolve_teammate_agent_types,
)
from backend.ingest_warm import WARM_RANGES, warm_common  # noqa: F401  (re-export)  # pylint: disable=unused-import
from backend.ingest_progress import (  # noqa: F401  (re-export)  # pylint: disable=unused-import
    _set_progress, progress_snapshot)

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


class IngestAborted(Exception):
    """Internal signal: _SHUTDOWN was seen between bounded steps.

    run_ingest_locked catches it SEPARATELY from the generic fatal path:
    the row is closed as aborted, but the rebuild, the cache
    invalidation and the ingest_done broadcast are all skipped — an
    aborted run must not tell clients data changed.
    """


def _check_shutdown() -> None:
    """Raise IngestAborted if shutdown was requested (bounded steps only)."""
    if _SHUTDOWN.is_set():
        raise IngestAborted("shutdown requested")


# The ingest_runs.error text an aborted run is closed with.
_ABORT_ERROR = "aborted: shutdown requested"


# What the fetch retry treats as transient. None of the boto3 failures is an
# OSError — ConnectionClosedError and EndpointConnectionError are
# BotoCoreErrors, and ClientError descends from neither — so catching
# OSError alone would miss exactly the drops this retry exists for.
# Deliberately NOT `Exception`: see FatalFetchError.
TRANSIENT_FETCH_ERRORS = (OSError, BotoCoreError, ClientError)

# A corrupt object, not a corrupt connection. r2.get_object inflates `.xz`
# keys transparently, so lzma raises from INSIDE the fetch — and every
# production object is `.xz`, which makes this the likeliest per-object
# failure there is. It is deterministic: retrying cannot un-truncate an
# upload, and treating it as a bug would abort the whole run over one bad
# file, which is the exact failure issue #2 is about.
CORRUPT_PAYLOAD_ERRORS = (lzma.LZMAError, EOFError)


class VanishedObject(Exception):
    """A listed object that was gone by the time its GET ran.

    The archiver deletes and moves objects while a run is in flight (a
    session pruned mid-run, or re-filed into another lane's bucket), so a
    NoSuchKey after a successful listing is the ordinary shape of "this
    key is an orphan that appeared early", not a drop and not a failure:
    retrying cannot bring the bytes back, and the next listing will not
    show the key at all. Raised on the first attempt, never booked in
    ingest_runs.error, and the key is handed to the orphan sweep so a row
    a previous run left for it does not linger as a stale file.
    """


class FatalFetchError(Exception):
    """A non-transient failure of an R2 GET, i.e. a bug rather than a drop.

    Routed past the per-object collector to the run-level handler on
    purpose: it is not something the next hourly run will fix, and booking
    it per object would report a code defect as a partial-data problem.
    """


# Bounded retry for the R2 GET only (see _fetch_with_retry). The tuple is
# the backoff BETWEEN attempts, so this is three attempts sleeping 0.5s then
# 1.0s: long enough to ride out a dropped connection, short enough that a
# genuinely dead object costs 1.5s rather than a run. Attempts are derived
# from the tuple so the two can never disagree.
FETCH_BACKOFF_S = (0.5, 1.0)
FETCH_ATTEMPTS = len(FETCH_BACKOFF_S) + 1

# How many failing keys the run's `error` summary names before it truncates.
# The point is a diagnosable message, not a transcript of every key.
FAILURE_KEYS_IN_SUMMARY = 5


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
        try:
            yield acquired
        finally:
            if acquired:
                # Issue #154: the unlock must never mask the body's error
                # (the expected failure is the lock session dying mid-run,
                # and the server releases a session-scoped advisory lock
                # at session death), so the swallow leaves no stale lock.
                try:
                    conn.execute(  # pylint: disable=no-member
                        "SELECT pg_advisory_unlock(%s)", (_INGEST_LOCK_KEY,))
                except Exception as exc:
                    log.warning(
                        "ingest advisory-lock unlock failed; the lock is "
                        "released server-side when the lock session dies, "
                        "and any in-flight run error is preserved: %s", exc)
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


def _open_run(started: datetime, trigger: str) -> int:
    """Insert the ingest_runs row, returning its id."""
    with db.viz_conn() as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO ingest_runs (started_at, trigger) VALUES (%s, %s) "
            "RETURNING id",
            (started, trigger),
        )
        row = cur.fetchone()
        assert row is not None  # INSERT ... RETURNING always yields a row
        run_id = row[0]
        c.commit()
    return run_id


def _existing_files() -> dict:
    """file_key → (etag, parser_version) for reparse decisions."""
    with db.viz_conn() as c:
        return {
            row[0]: (row[1], row[2])
            for row in c.execute(
                "SELECT file_key, r2_etag, parser_version FROM files"
            ).fetchall()
        }


def _track_project(seen_projects: dict[str, dict], project_id: str,
                   last_modified, project_path: str | None,
                   original_id: str | None = None) -> None:
    """Accumulate first/last seen mtimes for one project.

    `project_id` is the id this run resolved for the file — the slug of
    the project's marker path when the run read one, else the stored or
    bare-hash id (lane_projects.resolve_lane_project); `original_id` is
    the project id the OBJECT KEY carried before that resolution — the
    pre-fold slug for a Claude-layout file, the lane hash otherwise.
    display_name comes from that
    marker path where one was read; every other project — and a lane
    project whose marker was missing or malformed — displays the
    original-case id holding the MOST files this walk (ties: the first
    seen) — which for a Windows project is the one session's shell's
    casing, never the blindly-lowercased folded id.
    `display_name_set` records which case this run is, so _persist's
    upsert can PRESERVE a stored display_name on a run that read no
    marker instead of resetting it to the bare id; a stored display that
    IS the bare id gets upgraded to this run's cased form in SQL (the
    upsert's second CASE branch). The path is applied
    even when the entry already exists (a Claude-layout file of the same
    directory created it first): the merge is what the slug id is for.
    """
    proj = seen_projects.setdefault(project_id, {
        "project_id": project_id,
        "display_name": project_path or original_id or project_id,
        "display_name_set": bool(project_path),
        "first_seen_at": last_modified,
        "last_seen_at": last_modified,
        "case_counts": {},
    })
    if project_path:
        if not proj["display_name_set"]:
            proj["display_name"] = project_path
            proj["display_name_set"] = True
    else:
        original = original_id or project_id
        counts: dict[str, int] = proj["case_counts"]
        counts[original] = counts.get(original, 0) + 1
        if not proj["display_name_set"]:
            # max() keeps the FIRST maximal pair, so an equal count
            # leaves the display with the first-seen slug.
            proj["display_name"] = max(
                counts.items(), key=lambda item: item[1])[0]
    if last_modified < proj["first_seen_at"]:
        proj["first_seen_at"] = last_modified
    if last_modified > proj["last_seen_at"]:
        proj["last_seen_at"] = last_modified


def _fetch_marker(project_id: str, key: str) -> tuple[str, str] | None:
    """Fetch and parse a sessions/<project>/project.json marker.

    Returns (project_id, path) — the path the project's sessions were
    run from, which becomes the project's display_name. Runs on a pool
    thread (via _resolve) and touches no DB connection.

    The GET is retried and its failure PROPAGATES, so the caller books
    it as a per-object failure like any transcript fetch. Only decode
    and shape problems are swallowed here: a malformed marker means that
    project shows its id instead of its path, which is a degrade, not a
    failed fetch, and no retry would change it.
    """
    blob = _fetch_with_retry(key)
    try:
        data = json.loads(blob.decode("utf-8"))
        path = data.get("path")
        if isinstance(path, str) and path:
            return (project_id, path)
    except (ValueError, AttributeError):
        # ValueError covers UnicodeDecodeError and json.JSONDecodeError;
        # AttributeError covers a marker whose top level is not an object.
        pass
    return None


def _resolve_project_paths(marker_items: list[tuple[str, str, str]],
                           workers: int,
                           failed: list[tuple[str, str]]) -> dict[str, str]:
    """Resolve every listed marker's path before the todo loop starts:
    stored rows for unchanged etags, a GET on the pool for the rest.

    A marker GET is as droppable as a transcript GET, so its failures
    land in the same `failed` summary; a failed or vanished marker gives
    no path this run and is not stored, so the next run fetches it again.
    """
    project_paths, stale = lane_markers.cached_paths(marker_items)
    read: dict[str, tuple[str, str | None]] = {}
    for item, res, exc in _resolve(
        stale, lambda it: _fetch_marker(it[0], it[1]), workers
    ):
        if isinstance(exc, VanishedObject):
            continue
        if exc is not None:
            _record_failure(failed, item[1], exc)
            continue
        read[item[1]] = (item[2], res[1] if res is not None else None)
        if res is not None:
            project_paths[res[0]] = res[1]
    lane_markers.save_markers(read, {key for _, key, _ in marker_items})
    return project_paths


class _Wire(NamedTuple):
    """A listed transcript and its meta.json sidecar. `etag` joins the
    sidecar's to the transcript's: the sidecar can decide agent_type, so
    one landing after its transcript (the archiver uploads it second),
    changing or going away reparses the file, at no extra request. A main
    transcript has none, so /api/sessions still serves the object's own.
    """

    key: str
    etag: str
    size: int
    last_modified: datetime
    sidecar_key: str | None


def _scan_objects() -> tuple[list[_Wire], list[tuple[str, str, str]]]:
    """One listing pass: transcripts, each paired with the meta.json
    sidecar listed beside it (_Wire), and lane marker items.

    Markers are fetched afterwards by _resolve_project_paths, sidecars by
    _fetch_and_parse; the keys the layout rules skip (non-wire files
    inside sessions/, non-jsonl keys outside) are dropped here.
    """
    wire_objs: list = []
    marker_items: list[tuple[str, str, str]] = []
    sidecars: dict[tuple[str, str | None], r2.R2Object] = {}
    for obj in r2.list_keys():
        bucket, object_key = r2.split_key(obj.key)
        marker_project = key_layout.project_marker(object_key)
        if marker_project is not None:
            marker_items.append((marker_project, obj.key, obj.etag))
        elif (stem := key_layout.sidecar_stem(object_key)) is not None:
            sidecars[(bucket, stem)] = obj
        elif key_layout.classify(object_key) is not None:
            wire_objs.append(obj)
    wires = []
    for obj in wire_objs:
        bucket, object_key = r2.split_key(obj.key)
        side = sidecars.get((bucket, key_layout.transcript_stem(object_key)))
        wires.append(_Wire(obj.key, obj.etag if side is None else f"{obj.etag}+{side.etag}",
                           obj.size, obj.last_modified, side.key if side else None))
    return wires, marker_items


def _track_walked_project(seen_projects: dict[str, dict], info, obj,
                          project_paths: dict[str, str],
                          stored_lane: dict[str, str]) -> dict:
    """Resolve one walked file's project id and accumulate its mtimes.

    Returns the seen_projects entry (which _persist keys its project_id
    off), so the walk and every persist of the run share one identity —
    marker slug, stored mapping, or bare hash (lane_projects.resolve_lane_project).
    """
    # The PRE-canonical project id the key carried: classify() folds a
    # Windows slug on the Claude layout, and the walk needs the raw form
    # to choose display_name from (never the folded id itself). A lane
    # key's project is the hash segment; classify leaves it untouched.
    parts = r2.split_key(obj.key)[1].split("/")
    original_id = (parts[1] if parts[0] == key_layout.LANE_ROOT
                   else parts[0])
    marker_path = project_paths.get(info.project_id)
    project_id = lane_projects.resolve_lane_project(
        info.project_id, marker_path, stored_lane)
    _track_project(seen_projects, project_id,
                   obj.last_modified, marker_path, original_id=original_id)
    return seen_projects[project_id]


def _stored_version_is_newer(stored, parser_version: str) -> bool:
    """Whether a stored files row was written by a NEWER parser version.

    A rollback must not let the older binary's ingest rewrite rows it
    cannot write whole: _persist DELETEs and re-INSERTs each file's rows
    with its own column list, silently NULLing every column it does not
    know (issue #118). A stored value that does not parse as an int
    cannot be shown newer, so the ordinary reparse decision applies.
    """
    if stored is None:
        return False
    try:
        return int(stored[1]) > int(parser_version)
    except (TypeError, ValueError):
        return False


def _collect_todo(existing: dict, parser_version: str,  # pylint: disable=too-many-locals
                  failed: list[tuple[str, str]]) -> tuple:
    """Walk the bucket: count objects, remember live keys, and queue the
    files whose etag/parser_version says they need (re)parsing.

    Returns (listed, todo, seen_keys, newer). todo holds (obj, proj,
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
    wire_objs, marker_items = _scan_objects()
    project_paths = _resolve_project_paths(
        marker_items, worker_count(), failed
    )
    listed = 0
    seen_keys: set[str] = set()
    seen_projects: dict[str, dict] = {}
    todo: list[tuple] = []
    newer = 0
    stored_lane = lane_projects.stored_lane_ids()
    lane_projects.rekey_stale_lane_projects(project_paths, stored_lane)
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
    return listed, todo, seen_keys, newer


def _fetch_parse_persist(todo: list[tuple], parser_version: str,
                         failed: list[tuple[str, str]],
                         seen_keys: set[str]) -> tuple[int, int, int]:
    """Fetch+parse the queued files on a pool, persist on this thread.

    Fetch + parse is ~88% of per-file wall time and is network-bound
    (one R2 GET each), so it runs on a thread pool. Persistence stays
    on this thread: the per-file transaction boundary, and therefore
    ordering and failure semantics, are exactly as before. Work is
    submitted in bounded chunks so an 8k-file reparse does not hold
    every inflated blob in memory at once.

    A VanishedObject is discarded from `seen_keys` so _delete_orphans
    treats the key exactly as one the listing never showed.

    Returns (inserted, reparsed, vanished).
    """
    inserted = 0
    reparsed = 0
    vanished = 0
    _set_progress(phase="parsing", total=len(todo), done=0)
    workers = worker_count()
    chunk = max(1, workers * 4)
    for start in range(0, len(todo), chunk):
        _check_shutdown()
        for (obj, proj, stored), parsed, exc in _resolve(
            todo[start:start + chunk],
            lambda it: _fetch_and_parse(it[0].key, it[0].sidecar_key),
            workers,
        ):
            if isinstance(exc, VanishedObject):
                log.info("ingest: %s vanished between list and fetch", obj.key)
                seen_keys.discard(obj.key)
                vanished += 1
                continue
            if exc is not None:
                _record_failure(failed, obj.key, exc)
                continue
            _persist(obj, proj, parsed, parser_version)
            if stored is None:
                inserted += 1
            else:
                reparsed += 1
            _set_progress(done=inserted + reparsed)
    return inserted, reparsed, vanished


def _delete_orphans(seen_keys: set[str]) -> int:
    """Drop files rows whose R2 key is gone. CASCADE drops records."""
    _set_progress(phase="orphans")
    with db.viz_conn() as c, c.cursor() as cur:
        if seen_keys:
            cur.execute(
                "DELETE FROM files WHERE file_key != ALL(%s) RETURNING 1",
                (list(seen_keys),),
            )
        else:
            cur.execute("DELETE FROM files RETURNING 1")
        deleted = len(cur.fetchall())
        c.commit()
    return deleted


def _delete_orphan_projects() -> int:
    """Drop project rows no file references any more.

    A project id that moved under its files leaves the old row behind
    with nothing pointing at it — the Windows case-fold re-keys every
    pre-53 mixed-case row onto the folded id, the lane slug rekey moves
    a hash's files onto its marker slug, and a wiped subtree cascades
    its files away. /api/projects already hides a usage-less project;
    deleting keeps the table itself honest instead of only the read.
    Runs every ingest, after the orphan-file sweep.
    """
    _set_progress(phase="orphan_projects")
    with db.viz_conn() as c, c.cursor() as cur:
        cur.execute(
            "DELETE FROM projects p WHERE NOT EXISTS "
            "(SELECT 1 FROM files f WHERE f.project_id = p.project_id) "
            "RETURNING 1",
        )
        deleted = len(cur.fetchall())
        c.commit()
    if deleted:
        log.info("ingest: dropped %d orphan project row(s)", deleted)
    return deleted


def _close_run(run_id: int, finished: datetime, listed: int, reparsed: int,
               inserted: int, deleted: int, err: str | None) -> None:
    """Write the final counters onto the ingest_runs row."""
    with db.viz_conn() as c, c.cursor() as cur:
        cur.execute(
            "UPDATE ingest_runs SET finished_at=%s, r2_listed=%s, "
            "reparsed=%s, inserted=%s, deleted=%s, error=%s WHERE id=%s",
            (finished, listed, reparsed, inserted, deleted, err, run_id),
        )
        c.commit()


def _rebuild_derived_state() -> None:
    """Canonical flags and teammate roles, then the rollups that read them.

    Each phase is a bounded step, checked for shutdown between: an abort
    leaves the later rollups unbuilt; the next successful run rebuilds.
    """
    # Order matters: suppression removes rows the canonical pass would
    # otherwise rank, and the rollups read is_canonical and agent_type.
    # The names resolve through this module's globals at call time, so a
    # test can monkeypatch any phase on `ingest` itself.
    phases = (
        ("suppressed", purge_suppressed), ("canonical", recompute_canonical),
        ("teammates", resolve_teammate_agent_types),
        ("usage_rollup", rebuild_rollup), ("tool_rollup", rebuild_tool_rollup),
        ("tool_error_rollup", rebuild_tool_error_rollup),
        ("dispatch_rollup", rebuild_dispatch_rollup),
        ("dispatch_brief_rollup", rebuild_dispatch_brief_rollup),
        ("latency_rollup", rebuild_latency_rollup),
        ("ctx_cost_rollup", rebuild_ctx_cost_rollup),
        ("agent_rollup", rebuild_agent_rollup),
    )
    for phase, rebuild in phases:
        _check_shutdown()
        _set_progress(phase=phase)
        rebuild()


def _walk_and_persist(parser_version: str,
                      failed: list[tuple[str, str]]
                      ) -> tuple[int, int, int, int, int, int]:
    """The fallible body of a run: list, fetch+parse+persist, orphan sweep.

    Returns (listed, inserted, reparsed, deleted, vanished, newer).
    Exceptions propagate to run_ingest_locked, which books them as the
    run-level `fatal` — except IngestAborted, which closes the run as
    aborted.
    """
    listed, todo, seen_keys, newer = _collect_todo(
        _existing_files(), parser_version, failed
    )
    _check_shutdown()
    inserted, reparsed, vanished = _fetch_parse_persist(
        todo, parser_version, failed, seen_keys
    )
    _check_shutdown()
    deleted = _delete_orphans(seen_keys)
    _delete_orphan_projects()
    _check_shutdown()
    return listed, inserted, reparsed, deleted, vanished, newer


def run_ingest_locked(trigger: str) -> dict:  # pylint: disable=too-many-locals
    started = datetime.now(timezone.utc)
    run_id = _open_run(started, trigger)

    _set_progress(phase="listing", done=0, total=0,
                  run_id=run_id, started_at=started.isoformat())
    listed = inserted = reparsed = deleted = vanished = newer = 0
    # Per-object failures (key, message). Recorded in the run's `error`, but
    # deliberately NOT used to gate anything: one dropped connection out of
    # 9,213 files is a run with a retry pending, not a failed run.
    failed: list[tuple[str, str]] = []
    # A whole-run exception, which DOES gate the post-passes below.
    fatal = None
    # A shutdown request honoured mid-run (issue #103). Like `fatal`, it
    # gates the post-passes; unlike it, the row closes saying "aborted".
    aborted = False

    try:
        listed, inserted, reparsed, deleted, vanished, newer = (
            _walk_and_persist(constants.PARSER_VERSION, failed))
    except IngestAborted:
        log.warning("ingest (%s): aborted, shutdown requested", trigger)
        aborted = True
    except Exception as e:  # noqa: BLE001
        # The full exception goes to the logs; the stored text (served by
        # the public /health) is redacted of bucket names and the mirror
        # root, which the message of a mirror FileNotFoundError or an S3
        # error would otherwise carry.
        log.exception("ingest (%s): fatal, run aborted", trigger)
        fatal = r2.redact(f"{type(e).__name__}: {e}") or "run failed"

    if newer:
        log.warning(
            "ingest (%s): skipped reparse of %d file(s) whose stored "
            "parser_version is newer than this binary's own", trigger, newer)

    # `error` reports both kinds of trouble plus the abort; only these gate.
    err: str | None
    if aborted:
        err = _ABORT_ERROR
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
            _rebuild_derived_state()
        except IngestAborted:
            log.warning(
                "ingest (%s): aborted during the derived-state rebuild",
                trigger)
            aborted = True
            err = _ABORT_ERROR
        except Exception as e:  # noqa: BLE001
            log.exception(
                "ingest (%s): fatal, derived-state rebuild failed", trigger)
            fatal = r2.redact(f"{type(e).__name__}: {e}") or "run failed"
            err = fatal

    finished = datetime.now(timezone.utc)
    _close_run(run_id, finished, listed, reparsed, inserted, deleted, err)

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

    # Data changed: mark the response cache stale, then notify connected
    # SSE clients so the dashboard re-fetches without a page reload.
    #
    # This used to clear() the cache outright, which meant every ingest
    # dropped every user onto the uncached path — 8s+ for the dashboard,
    # and worse for /api/cache. invalidate() keeps the entries servable
    # while marking them stale, so the refetch triggered by ingest_done
    # returns the previous numbers instantly and the fresh ones land via
    # the background refresh. Threadsafe: ingest may run in a scheduler
    # thread. An aborted run skips this to match its skipped rebuild: it
    # must not tell clients data changed.
    if fatal is None and not aborted and (inserted or reparsed or deleted):
        cache.response_cache.invalidate()
        events.broadcast_threadsafe("ingest_done", summary)

    if fatal is None and not aborted:
        _set_progress(phase="warming")
        warm_common()
    _set_progress(phase="idle", done=0, total=0)
    return summary


def _resolve(items: list, call, workers: int) -> list[tuple]:
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


def _record_failure(failed: list[tuple[str, str]], key: str,
                    exc: BaseException) -> None:
    """Book one object as failed and say so in the log."""
    failed.append((key, f"{type(exc).__name__}: {exc}"))
    log.warning(
        "ingest: %s failed after %d attempt(s): %s: %s",
        key, FETCH_ATTEMPTS, type(exc).__name__, exc,
    )


def failure_summary(failed: list[tuple[str, str]]) -> str | None:
    """One line naming how many objects failed and which, or None.

    Goes into ingest_runs.error so a partial run is visible in the admin
    view, without pretending the whole run failed. The keys are
    presentation here (the column feeds the public /health), so they go
    out in their public form — the bucket segment never leaves the
    server (SV-FILES-RECORDS); the log keeps the qualified keys.
    """
    if not failed:
        return None
    keys = [r2.public_key(key) or key for key, _ in failed]
    shown = ", ".join(keys[:FAILURE_KEYS_IN_SUMMARY])
    if len(keys) > FAILURE_KEYS_IN_SUMMARY:
        shown += f", ... (+{len(keys) - FAILURE_KEYS_IN_SUMMARY} more)"
    noun = "object" if len(keys) == 1 else "objects"
    return f"{len(keys)} {noun} failed after retries: {shown}"


def worker_count() -> int:
    """Fetch+parse concurrency.

    Unset or unparseable -> auto (network-bound work, so oversubscribe
    cores). An explicit number is honoured, clamped to at least 1, so
    INGEST_WORKERS=1 is a real "go sequential" switch for debugging.
    """
    auto = min(16, (os.cpu_count() or 4) * 2)
    raw = os.environ.get("INGEST_WORKERS", "").strip()
    if not raw:
        return auto
    try:
        return max(1, int(raw))
    except ValueError:
        return auto


def _fetch_with_retry(key: str) -> bytes:
    """One R2 GET, retried on transient failure. Runs on a pool thread.

    The retry lives here rather than in backend.r2 so the transcript and
    sidecar readers keep their current single-shot semantics — only the
    ingest, which walks the whole bucket in one pass, needs to ride out a
    transient drop.

    Three outcomes, because "did that fail" is three questions, not two:

    - TRANSIENT_FETCH_ERRORS — retry, then propagate so the caller books
      one per-object failure.
    - CORRUPT_PAYLOAD_ERRORS — propagate on the FIRST attempt. Also one
      per-object failure, but no retry: the bytes will not improve.
    - a missing object (S3 NoSuchKey, file-mode ENOENT) — VanishedObject
      on the FIRST attempt: not a failure at all, see the class.
    - anything else — a bug, re-raised as FatalFetchError so the
      per-object collector does not absorb it. A TypeError inside
      get_object would otherwise become a 9,213-object "partial run" that
      slept the better part of four hours through the same bug instead of
      raising one loud traceback.
    """
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            return r2.get_object(key)
        except CORRUPT_PAYLOAD_ERRORS:
            raise
        except TRANSIENT_FETCH_ERRORS as e:
            if _is_missing(e):
                raise VanishedObject(key) from e
            if attempt == FETCH_ATTEMPTS:
                raise
            log.warning(
                "ingest: fetch of %s failed (attempt %d/%d), retrying",
                key, attempt, FETCH_ATTEMPTS,
            )
            time.sleep(FETCH_BACKOFF_S[attempt - 1])
        except Exception as e:  # noqa: BLE001
            raise FatalFetchError(f"{key}: {type(e).__name__}: {e}") from e
    raise AssertionError("unreachable")  # pragma: no cover


def _is_missing(exc: BaseException) -> bool:
    """Whether a fetch error says the object does not exist (any more).

    FileNotFoundError is the file:// mirror's spelling; boto3 folds the
    S3 404 into a ClientError whose code is `NoSuchKey`. Neither is
    transient, and neither is a bug, which is why they need a name of
    their own rather than a place in the two tuples above.
    """
    if isinstance(exc, FileNotFoundError):
        return True
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code")
        return code in ("NoSuchKey", "404")
    return False


def _fetch_and_parse(key: str, sidecar_key: str | None = None) -> dict:
    """Runs on a pool thread. Touches no DB connection.

    Only the GET is retried: a parse failure is deterministic, so a second
    attempt reproduces the same error against the same bytes and buys
    nothing but delay.

    The meta.json sidecar is fetched only for a transcript naming no role
    of its own (agent_sidecar.apply_agent_sidecar). A vanished or corrupt one
    leaves the default agent_type, which no retry would change. A
    transient failure fails the FILE like its own GET would: persisting
    it would store the pair etag with the default, and a healthy run
    would then see nothing to redo. FatalFetchError escapes to the run.
    """
    parsed = parse.parse_file(key, _fetch_with_retry(key))
    if sidecar_key is None or parsed["agent_type_in_band"]:
        return parsed
    try:
        sidecar = _fetch_with_retry(sidecar_key)
    except (VanishedObject, *CORRUPT_PAYLOAD_ERRORS):
        return parsed
    return agent_sidecar.apply_agent_sidecar(parsed, sidecar, r2.split_key(key)[1])
