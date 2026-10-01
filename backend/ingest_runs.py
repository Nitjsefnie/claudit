"""The ingest_runs row lifecycle: open at run start, close at run end.

Split out of ingest.py for size: a change adding net lines to a file the
module-size baseline pins pays for it by moving a cohesive piece into a
new module, and the run row's INSERT and final UPDATE are that piece.
`backend.ingest` re-exports both so `ingest._open_run(...)` and
`ingest._close_run(...)` keep resolving for callers.
"""
from __future__ import annotations

from datetime import datetime, timezone

from backend import db, ingest_progress, r2

# The ingest_runs.error text an aborted run is closed with. The row text
# spells "aborted: <cause>"; a plain shutdown keeps this exact value.
_ABORT_ERROR = "aborted: shutdown requested"

# The ingest_runs.error text the run-start sweep closes a crash leftover
# with (issue #440). Same "aborted: ..." family as _ABORT_ERROR, but a
# distinct value: /health must let an operator tell "died without a
# graceful stop" from "stopped cleanly".
_STALE_ERROR = "aborted: previous run died without closing (crash)"


def failure_summary(failed: list[tuple[str, str]]) -> str | None:
    """Count failed objects for `ingest_runs.error`.

    This text feeds the unauthenticated /health endpoint (issue #253), so
    names must not cross that boundary. Failed keys stay in the
    `_record_failure` log and the admin-only `failed_keys` summary.
    """
    if not failed:
        return None
    count = len(failed)
    noun = "object" if count == 1 else "objects"
    return f"{count} {noun} failed after retries"


def failed_public_keys(failed: list[tuple[str, str]]) -> list[str]:
    """Return bucket-stripped failed keys for authenticated triage."""
    return [r2.public_key(key) or key for key, _ in failed]


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


def _close_run(run_id: int, finished: datetime, listed: int, reparsed: int,
               inserted: int, deleted: int, newer: int,
               err: str | None) -> None:
    """Write the final counters onto the ingest_runs row.

    `newer` counts the files this run declined to reparse because their
    stored parser_version is newer than this binary's own (the issue #118
    rollback guard). Persisted so /health can show a rollback in progress
    without trawling the logs (issue #161).
    """
    with db.viz_conn() as c, c.cursor() as cur:
        cur.execute(
            "UPDATE ingest_runs SET finished_at=%s, r2_listed=%s, "
            "reparsed=%s, inserted=%s, deleted=%s, newer=%s, "
            "error=%s WHERE id=%s",
            (finished, listed, reparsed, inserted, deleted, newer, err,
             run_id),
        )
        c.commit()


def close_open_run(err: str = _ABORT_ERROR) -> bool:
    """Close an open run's row as aborted — the fallback behind lifespan
    teardown's bounded wait (issue #372).

    A run stuck inside one long single-statement phase cannot reach a
    bounded step, so a stop that outlives the wait used to exit leaving
    finished_at NULL (the audit's live rows). Only a still-open row is
    written: the run thread's own close, either side of this one, wins.
    Returns whether this call closed the row.
    """
    run_id = ingest_progress.progress_snapshot().get("run_id")
    if run_id is None:
        return False
    with db.viz_conn() as c, c.cursor() as cur:
        cur.execute(
            "UPDATE ingest_runs SET finished_at = %s, error = %s "
            "WHERE id = %s AND finished_at IS NULL",
            (datetime.now(timezone.utc), err, run_id),
        )
        closed = bool(cur.rowcount)
        c.commit()
    return closed


def sweep_stale_runs() -> int:
    """Close every still-open ingest_runs row as a crash leftover.

    Called at the top of a run that holds the db-wide advisory lock
    (ingest._db_run_lock): the server releases that lock the moment the
    holding connection dies, so an open row under it proves the process
    that opened it is gone — the SIGKILL shape the graceful paths (#103,
    #372) can never close behind. No host/pid columns are needed: the
    lock is the liveness proof. One set-based UPDATE; returns how many
    rows it closed. The lock also serialises runs, so a row the current
    run thread will close itself cannot exist at sweep time.
    """
    with db.viz_conn() as c, c.cursor() as cur:
        cur.execute(
            "UPDATE ingest_runs SET finished_at = %s, error = %s "
            "WHERE finished_at IS NULL",
            (datetime.now(timezone.utc), _STALE_ERROR),
        )
        closed = cur.rowcount
        c.commit()
    return closed
