"""Per-run ingest timing state and phase helpers."""
from __future__ import annotations

import threading
from contextvars import ContextVar
from contextlib import nullcontext
from dataclasses import dataclass, field

from backend import timing


@dataclass
class _RunTiming:
    phases: timing.Phases
    todo: int = 0
    changed: int = 0
    outcome: str = "fatal"
    persist_seconds: float = 0.0
    scope: str = "full"
    # Process-pool pipeline accounting: the instant the last parse future
    # completed, so fetch_parse (wall until it) and persist (the drain
    # after it) stay disjoint phases that sum under the run's total.
    last_parse_done: float | None = None
    # persist_seconds accrues from the persist pool's threads.
    persist_lock: threading.Lock = field(default_factory=threading.Lock)
    # Pipeline provenance, stamped once per run for the TIMING tail.
    parse_processes: int = 1
    persist_threads: int = 1
    # Issue #662: the pool pipeline's fetch_parse parts, accumulated by
    # pipeline_pool (child work summed across the forked children via the
    # stages dict parse_wire returns, persist work via persist_seconds)
    # and emitted as mark_part breakdown figures beside the wall marks.
    wait_parse: float = 0.0
    wait_persist: float = 0.0
    child_work: dict[str, float] = field(default_factory=dict)


_RUN_TIMING: ContextVar[_RunTiming | None] = ContextVar(
    "claudit_ingest_timing", default=None)


def _timed_step(label: str):
    current = _RUN_TIMING.get()
    return current.phases.step(label) if current is not None else nullcontext()


def _record_phase(label: str, seconds: float) -> None:
    current = _RUN_TIMING.get()
    if current is not None:
        current.phases.mark(label, seconds)


def _record_scope(scope: str) -> None:
    current = _RUN_TIMING.get()
    if current is not None:
        current.scope = scope
