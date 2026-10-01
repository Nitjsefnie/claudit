#!/usr/bin/env python3
"""One pytest run, partitioned into phases and counted two ways.

The bench (``scripts/ci/suite_bench.py``) owns the CLI, the fixture list
and the run; this module owns the PARTITION, because the instruction
counter and the process-time fallback attribute a run's cost the same
way and neither should be the only copy of how.

A run over the pinned fixture is made of three phases -- collection
(inside ``pytest_collection``), run (inside every
``pytest_runtest_protocol``) and everything else. The "everything else"
is the ``residual`` phase: pytest's own bootstrap, conftest imports,
plugin registration, terminal reporting, this bench's plugin. It is
named, measured and ratcheted rather than rounded away, because an
instrument whose parts sum to less than its total has a hole in it and
a hole reads as completeness. Every output prints the phase sum beside
the total, so a partition hole cannot hide.

Two instruments:

- ``InstructionCounter`` counts PYTHON BYTECODE instructions with
  ``sys.monitoring``'s INSTRUCTION events, which fire once per bytecode
  the interpreter retires. A count is exact WITHIN one environment
  state -- the same tree, interpreter, libraries and machine state
  retire the same number of bytecodes on a loaded machine and an idle
  one, and the re-exec with ``PYTHONHASHSEED=0`` holds set-iteration
  order fixed, which is the one input left to hash order (pytest walks
  sets in several places). Across machine families the counts are not
  portable: within one family the observed wobble is <=0.2M, while
  ``residual`` reads ~2.4M higher on a GitHub runner than on this
  benchmark box (the session-level code outside the wrapped phases is
  environment-sensitive), which is why the budgets are seeded from a
  runner measurement.
- ``ProcessTimeSink`` accumulates ``time.process_time()`` per phase.
  It is the portable fallback when no ``sys.monitoring`` is available,
  and telemetry-only in every case: a CPU-time reading drifts with the
  interpreter's dispatch cost, so nothing gates on it -- ``--check`` on
  a counts-less measurement fails closed.
"""
from __future__ import annotations

import contextlib
import importlib
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if __package__:
    # pylint: disable-next=relative-beyond-top-level,no-name-in-module
    from . import thresholds
else:
    thresholds = importlib.import_module('thresholds')

# The phases of a run, and the partition they make of its total. The
# names come from the loader (thresholds.py), which validates the
# document carrying them, so a phase cannot be measured under one name
# and recorded under another.
PHASES = thresholds.SUITE_COST_PHASES
# A tool id nobody else holds in a bench process: 0-2 are the debugger's,
# coverage's and the profiler's, and 3 is reparse_bench's claim (the
# counting bench of scripts/ci/reparse_bench.py, a different path). 4 is
# the suite bench's claim on this process only, released in close().
TOOL_ID = 4
# The unit the document stores: MILLIONS of instructions. One decimal of
# this unit is 100k instructions -- a thousand times finer than the
# 1.5-unit yardstick, and exact, because a count is a count.
SCALE = Decimal(1_000_000)


class InstructionCounter:
    """Count the bytecodes retired while a phase window is open.

    One tool id, one callback, and a stack of window keys: the callback
    increments the top of the stack, so a window opened inside another
    counts into the inner phase, and instructions outside every window
    are the ones the run phases do not account for -- the residual,
    read off the total.

    ``reason`` is empty when the counter is usable and says why not when
    it is not. A counter that cannot count must never look like a
    counter that counted nothing.
    """
    # sys.monitoring is installed by the interpreter, not imported, so a
    # static reader cannot see its members. Every call below is guarded
    # by the availability check that sets `reason`.
    # pylint: disable=no-member
    def __init__(self, name: str = 'suite-bench'):
        self.phases: dict[str, int] = {
            phase: 0 for phase in PHASES if phase != 'residual'}
        self.total = 0
        self.reason = ''
        self._stack: list[str] = []
        self._monitoring: Any = getattr(sys, 'monitoring', None)
        if self._monitoring is None:
            self.reason = 'this interpreter has no sys.monitoring'
            return
        try:
            self._monitoring.use_tool_id(TOOL_ID, name)
        except ValueError as error:
            self.reason = f'tool id {TOOL_ID} is unavailable: {error}'
            return
        self._event = self._monitoring.events.INSTRUCTION
        self._monitoring.register_callback(TOOL_ID, self._event, self._count)

    def _count(self, code, instruction_offset):
        self.total += 1
        # The region window's key ('total') is on the stack whenever the
        # phase windows are not: instructions outside the phases count
        # toward the total only, and ARE the residual.
        if self._stack and self._stack[-1] in self.phases:
            self.phases[self._stack[-1]] += 1

    @property
    def available(self) -> bool:
        return not self.reason

    @contextlib.contextmanager
    def window(self, key: str):
        """Count every bytecode retired inside this block into ``key``.

        A no-op when the counter never armed: an unusable instrument's
        windows must not reach ``sys.monitoring`` with a tool id it
        does not hold.
        """
        if not self.available:
            yield
            return
        self._stack.append(key)
        if len(self._stack) == 1:
            self._monitoring.set_events(TOOL_ID, self._event)
        try:
            yield
        finally:
            self._stack.pop()
            if not self._stack:
                self._monitoring.set_events(TOOL_ID, 0)

    def close(self) -> None:
        """Stop counting and give the tool id back.

        Leaving it registered would tax every instruction the rest of
        the process retires, long after the measurement finished.
        """
        if self._monitoring is not None:
            self._monitoring.set_events(TOOL_ID, 0)
            self._monitoring.register_callback(TOOL_ID, self._event, None)
            self._monitoring.free_tool_id(TOOL_ID)
            self.reason = 'closed'


class ProcessTimeSink:
    """Accumulate ``time.process_time()`` per phase window.

    The portable fallback's instrument. Shapes like the counter's so the
    partition algebra below is shared: same windows, same phases, same
    residual. ``total`` is the reading the bench takes around the whole
    measured region, exactly as it would for the counter.
    """

    def __init__(self):
        self.phases: dict[str, float] = {
            phase: 0.0 for phase in PHASES if phase != 'residual'}
        self.total = 0.0
        self._stack: list[str] = []

    @contextlib.contextmanager
    def window(self, key: str):
        """Fold the window's process time into ``key`` at its close."""
        self._stack.append(key)
        start = time.process_time()
        try:
            yield
        finally:
            self.phases[key] += time.process_time() - start
            self._stack.pop()


# --- the pytest plugin -------------------------------------------------------

class SuitePhasePlugin:
    """Partition a pytest run by hookwrappers.

    ``pytest_collection`` wraps the whole collection phase; every
    ``pytest_runtest_protocol`` wraps one test's setup, call and
    teardown. Everything the run does outside these windows -- config,
    conftest loading before collection proper, terminal reporting --
    falls outside and is the residual phase.
    """

    def __init__(self, counter, sink):
        self._counter = counter
        self._sink = sink
        self.tests = 0

    @pytest.hookimpl(hookwrapper=True)
    def pytest_collection(self):
        with self._counter.window('collection'), \
                self._sink.window('collection'):
            yield

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(self, item, nextitem):
        self.tests += 1
        with self._counter.window('run'), self._sink.window('run'):
            yield


def partition(total, collection, run, tolerance=Decimal(0)):
    """The three phases from the two wrapped windows and the total.

    The same algebra for both instruments, so a phase means the same
    thing whichever way it was measured. ``residual`` is the divergence:
    total - collection - run, which is every instruction (or every CPU
    second) the run spent outside the windows. A NEGATIVE residual is an
    instrument error -- a window bookkeeping bug -- and fails the run.
    ``tolerance`` exists for the CPU fallback's rounding only: a float
    fold-up can leave the residual one rounding step under zero, and a
    telemetry reading may absorb that; the counts pass the default 0,
    because an exact instrument must be exact.
    """
    residual = total - collection - run
    if residual < -tolerance:
        raise ValueError(
            f'instrument error: negative residual {residual} '
            f'(total {total}, collection {collection}, run {run})')
    if residual < 0:
        residual = Decimal(0)
    return {'collection': collection, 'run': run, 'residual': residual}
