"""Live ingest progress, for /health.

Split out of ingest.py for size: issue #154's unlock guard adds net
lines to a file the module-size baseline pins, and growth there is paid
by moving a cohesive piece into a new module.
"""
from __future__ import annotations

import threading

# Live ingest progress, for /health. Held in memory rather than written to
# ingest_runs: that row's counters are only filled by the final UPDATE, so for
# the minutes a full reparse takes /health could report nothing at all. Single
# process (no --workers), so the scheduler thread that mutates this and the
# request thread that reads it share an interpreter.
_PROGRESS: dict = {"phase": "idle", "done": 0, "total": 0,
                   "run_id": None, "started_at": None}
_PROGRESS_LOCK = threading.Lock()


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
