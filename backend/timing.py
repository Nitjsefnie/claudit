"""Dependency-free env-gated phase timing shared by API and ingest."""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Any

from backend.env_config import getenv

# Per-phase wall-clock for the heavy read endpoints, emitted as one log
# line per request. Gated on CLAUDIT_TIMING so it costs nothing normally,
# but stays in the tree — reconstructing these queries by hand in psql
# drifts from what the endpoint actually runs and hides everything that
# happens outside SQL (row marshalling, response serialisation).
TIMING_ON = getenv("CLAUDIT_TIMING", "").lower() not in ("", "0", "false", "no")

_CLAUDIT_LOGGER = logging.getLogger("claudit")

if not _CLAUDIT_LOGGER.handlers:
    # Operational INFO reaches the service log without a flag. Uvicorn
    # leaves the root logger at WARNING, so attach a handler to claudit,
    # the parent of both API and ingest loggers.
    #
    # CLAUDIT_TIMING gates only timing lines, not the handler or level.
    # Keep default propagation so root handlers such as caplog see records.
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s:     %(message)s"))
    _CLAUDIT_LOGGER.addHandler(_handler)
    _CLAUDIT_LOGGER.setLevel(logging.INFO)


class Phases:
    """Log phase walls and breakdown-only work on one timing line.

    mark contributes to sum/gap; mark_part does not, because summed child
    and thread work can exceed elapsed wall time. Print parts after phase
    marks and before the tail."""

    __slots__ = ("_name", "_marks", "_parts", "_t0", "_logger", "_account")

    def __init__(self, name: str,
                 logger: logging.Logger | None = None, *,
                 account: bool = False) -> None:
        self._name = name
        self._marks: list[tuple[str, float]] = []
        self._parts: list[tuple[str, float]] = []
        self._t0 = time.perf_counter()
        self._logger = (logger if logger is not None
                        else logging.getLogger("claudit.api"))
        self._account = account

    @contextmanager
    def step(self, label: str):
        t = time.perf_counter()
        try:
            yield
        finally:
            self._marks.append((label, time.perf_counter() - t))

    def mark(self, label: str, seconds: float) -> None:
        self._marks.append((label, seconds))

    def mark_part(self, label: str, seconds: float) -> None:
        self._parts.append((label, seconds))

    def execute(self, label: str, cur, sql: str, args: Any = None):
        """Time a single ``cursor.execute`` and record it under `label`.

        Returns the cursor, so call sites keep their trailing
        ``.fetchall()`` / ``.fetchone()`` unchanged.
        """
        t = time.perf_counter()
        try:
            return cur.execute(sql, args) if args is not None else cur.execute(sql)
        finally:
            self._marks.append((label, time.perf_counter() - t))

    def done(self, **extra: Any) -> None:
        if not TIMING_ON:
            return
        total = (time.perf_counter() - self._t0) * 1000
        marks = " ".join(f"{k}={v * 1000:.0f}ms" for k, v in self._marks)
        parts = " ".join(f"{k}={v * 1000:.0f}ms" for k, v in self._parts)
        if parts:
            marks = f"{marks} {parts}"
        tail = " ".join(f"{k}={v}" for k, v in extra.items())
        if self._account:
            summed = sum(v for _, v in self._marks) * 1000
            gap = total - summed
            self._logger.info(
                "TIMING %s total=%.0fms sum=%.0fms gap=%.0fms %s %s",
                self._name, total, summed, gap, marks, tail)
        else:
            self._logger.info(
                "TIMING %s total=%.0fms %s %s",
                self._name, total, marks, tail)
