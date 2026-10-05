"""Dependency-free env-gated phase timing shared by API and ingest."""
from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from typing import Any

# Per-phase wall-clock for the heavy read endpoints, emitted as one log
# line per request. Gated on CLAUDIT_TIMING so it costs nothing normally,
# but stays in the tree — reconstructing these queries by hand in psql
# drifts from what the endpoint actually runs and hides everything that
# happens outside SQL (row marshalling, response serialisation).
TIMING_ON = os.environ.get("CLAUDIT_TIMING", "").lower() not in ("", "0", "false", "no")

_CLAUDIT_LOGGER = logging.getLogger("claudit")

if not _CLAUDIT_LOGGER.handlers:
    # Operational INFO — the reprice summary, the rollback-guard skip
    # count, rekey and eviction notices (issue #378) — must reach the
    # service log without any flag. uvicorn configures its own loggers
    # and leaves the root logger at WARNING, so a bare log.info() here
    # would go nowhere. Attach our own handler rather than depending on
    # someone else's logging config.
    #
    # Attached to the "claudit" PARENT, not "claudit.api": ingest logs
    # under "claudit.ingest" and was silently discarded, so
    # recompute_canonical / rebuild_* / warm_common reported nothing and
    # the one place that says what the warmer is doing was invisible.
    #
    # CLAUDIT_TIMING gates only the TIMING lines themselves (Phases.done
    # returns early without it); the logger's handler and level are not
    # part of that gate. Propagation stays at its default, so a root
    # handler (pytest's caplog) still sees every record.
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s:     %(message)s"))
    _CLAUDIT_LOGGER.addHandler(_handler)
    _CLAUDIT_LOGGER.setLevel(logging.INFO)


class Phases:
    """Collect labelled phase timings and log them as a single line.

    `mark` records a phase wall that counts into the account line's sum;
    `mark_part` records a BREAKDOWN-ONLY figure (issue #662: the pool
    pipeline's fetch_parse parts are work summed across children and
    threads, which may exceed the run's wall, so it must not enter the
    sum/gap accounting). Parts print after the phase marks, before the
    tail.
    """

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
