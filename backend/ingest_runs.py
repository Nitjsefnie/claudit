"""The ingest_runs row lifecycle: open at run start, close at run end.

Split out of ingest.py for size: a change adding net lines to a file the
module-size baseline pins pays for it by moving a cohesive piece into a
new module, and the run row's INSERT and final UPDATE are that piece.
`backend.ingest` re-exports both so `ingest._open_run(...)` and
`ingest._close_run(...)` keep resolving for callers.
"""
from __future__ import annotations

from datetime import datetime

from backend import db


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
