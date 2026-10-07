"""Live ingest progress, for /health.

Split out of ingest.py for size: issue #154's unlock guard adds net
lines to a file the module-size baseline pins, and growth there is paid
by moving a cohesive piece into a new module.
"""
from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timezone

from fastapi.responses import JSONResponse
from starlette.responses import Response

from backend import cache, constants, db, r2

# Live ingest progress, for /health. Held in memory rather than written to
# ingest_runs: that row's counters are only filled by the final UPDATE, so for
# the minutes a full reparse takes /health could report nothing at all. Single
# process (no --workers), so the scheduler thread that mutates this and the
# request thread that reads it share an interpreter.
_PROGRESS: dict = {"phase": "idle", "done": 0, "total": 0,
                   "run_id": None, "started_at": None}
_PROGRESS_LOCK = threading.Lock()

log = logging.getLogger("claudit.app")  # /health keeps its unit name


def progress_snapshot() -> dict:
    with _PROGRESS_LOCK:
        return dict(_PROGRESS)


def _set_progress(**kw) -> None:
    """Update live progress.

    Single-writer by construction: _RUN_LOCK admits one run at a time, so
    nothing else can interleave into this dict. That guarantee is the fix for
    the readout that showed `total` changing mid-run, `done` reaching 106% and
    then going backwards — two overlapping runs sharing one slot.
    """
    with _PROGRESS_LOCK:
        _PROGRESS.update(kw)


def health() -> Response:
    """The /health handler. Moved verbatim out of app.py when the cache
    readout joined the response (issue #641); app.py was at the
    module-size ceiling and could not grow it."""

    parser_version = constants.PARSER_VERSION
    last_ingest = None
    try:
        with db.viz_conn() as c:
            row = c.execute(
                "SELECT id, started_at, finished_at, trigger, "
                "r2_listed, reparsed, newer, error "
                "FROM ingest_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row:
                error = r2.redact(row[7])
                if error:
                    # Net for rows stored by older builds; new rows are
                    # count-only at the source.
                    error = re.sub(
                        r"(?s)^(\d+ objects? failed after retries):.*", r"\1", error)
                last_ingest = {
                    "id": row[0],
                    "started_at": row[1].isoformat() if row[1] else None,
                    "finished_at": row[2].isoformat() if row[2] else None,
                    "trigger": row[3],
                    "r2_listed": row[4],
                    "reparsed": row[5],
                    "newer": row[6],
                    "error": error,
                }
    except Exception:  # noqa: BLE001
        # Driver text can name hosts, databases, buckets — the public
        # body gets a generic message; the details go to the logs.
        log.exception("health: database query failed")
        # A status-code monitor (curl -fsS, an LB probe) never parses the
        # body, so failing only in the body reads as healthy to it. 503
        # carries the same JSON fields (issue #104).
        return JSONResponse(
            status_code=503,
            content={
                "ok": False, "db": False, "error": "database unavailable",
                "version": constants.VERSION,
                "parser_version": parser_version,
                "now": datetime.now(timezone.utc).isoformat(),
            },
        )
    # Live progress for the run in flight. ingest_runs only gains its
    # counters in the final UPDATE, which is written only after the
    # derived-state rebuilds finish — so a caller watching that row sees
    # nothing for minutes, then "done" with the rollups already rebuilt;
    # the progress readout below is what shows the rebuilds in flight.
    prog = progress_snapshot()
    running = prog.get("phase") not in (None, "idle")
    ingest_progress = None
    if running:
        done, total = prog.get("done") or 0, prog.get("total") or 0
        ingest_progress = {
            "phase": prog.get("phase"),
            "done": done,
            "total": total,
            "pct": round(100.0 * done / total, 1) if total else None,
            "run_id": prog.get("run_id"),
            "started_at": prog.get("started_at"),
        }

    return JSONResponse(content={
        "ok": True, "db": True,
        "ingest_running": running,
        "ingest_progress": ingest_progress,
        "last_ingest": last_ingest,
        # Which build is answering. The DB-error branch above reports it
        # too: "which version is broken" is exactly the question asked when
        # /health is failing, so it must not be the field that goes missing.
        "version": constants.VERSION,
        "parser_version": parser_version,
        # Cache-outcome counters and pool gauges (issue #641): the
        # slow-open split and the requests-queued-on-the-ingest
        # hypothesis, readable from production without a deploy.
        "cache": cache_readout(),
        "now": datetime.now(timezone.utc).isoformat(),
    })


def cache_readout() -> dict:
    """Cache-outcome counters and the viz-pool gauges, the readout the
    slow-open investigation (issue #641) reads from production: a count
    cannot be moved by load, and the pool gauges answer the
    requests-queued-on-the-ingest hypothesis directly."""
    return {
        "outcomes": cache.response_cache.outcomes(),
        "pool": db.viz_pool_statistics(),
    }
