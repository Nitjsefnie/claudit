"""Per-run ingest timing state and phase helpers."""
from __future__ import annotations

from contextvars import ContextVar
from contextlib import nullcontext
from dataclasses import dataclass

from backend import timing


@dataclass
class _RunTiming:
    phases: timing.Phases
    todo: int = 0
    changed: int = 0
    outcome: str = "fatal"
    persist_seconds: float = 0.0


_RUN_TIMING: ContextVar[_RunTiming | None] = ContextVar(
    "claudit_ingest_timing", default=None)


def _timed_step(label: str):
    current = _RUN_TIMING.get()
    return current.phases.step(label) if current is not None else nullcontext()


def _record_phase(label: str, seconds: float) -> None:
    current = _RUN_TIMING.get()
    if current is not None:
        current.phases.mark(label, seconds)
