#!/usr/bin/env python3
"""One reparse pass, split into its phases and measured two ways.

The bench (``scripts/ci/reparse_bench.py``) owns the corpus, the pass and
the reporting; this module owns the DECOMPOSITION, because both
instruments decompose a pass the same way and neither should be the only
copy of how.

A pass is made of three callables — ``parse.sniff_format`` (nested inside
``parse.parse_file``, so it is subtracted rather than added),
``parse.parse_file`` and ``agent_sidecar.apply_agent_sidecar`` — plus
everything they do not account for. That remainder is the ``residual``
phase: named, measured and ratcheted, never spread over the others,
because an instrument whose parts sum to less than its total has a hole
in it and a hole reads as completeness.

Two instruments, because each sees something the other cannot:

- ``instrumented`` times the phases with ``time.process_time``. Wall
  clock never appears: co-tenant load moved wall measurements 2-4x on
  the deployment host without changing the work. Time is what makes a
  PHASE SHARE, which is scale-free inside one run — but a share cannot
  see the parse getting slower as a whole, because every phase grows
  together.

- ``InstructionCounter`` counts the phases with
  ``sys.monitoring``'s INSTRUCTION event, which fires once per bytecode
  instruction the interpreter retires. A count is exact — the same tree
  retires the same number of bytecodes on a loaded machine and an idle
  one, under any ``PYTHONHASHSEED`` (measured, not assumed) — so it is
  the instrument that catches a uniform slowdown, and it needs no
  amplification at all to be stable. It counts PYTHON BYTECODE
  instructions, not machine instructions: the two differ by the
  interpreter's own dispatch cost, and only ``perf`` can count machine
  instructions, which needs a privileged counter this bench cannot
  assume a runner has.
"""
from __future__ import annotations

import contextlib
import sys
import time
from decimal import Decimal
from pathlib import Path
from typing import Any, NamedTuple

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# pylint: disable-next=wrong-import-position
from backend import agent_sidecar, parse  # noqa: E402

# The phases of a pass, and the partition they make of a total.
PHASES = ('sniff', 'parse_body', 'sidecar', 'residual')
# A tool id nobody else holds: 0-2 are the debugger's, coverage's and the
# profiler's, and 3 is the first of the ids the docs leave to third
# parties. Taking it is a claim on this process only, released in close().
TOOL_ID = 3
# A count needs no amplification to be stable — one pass is exact — but a
# few passes make the reported per-file figure a whole number of
# bytecodes per transcript rather than a fraction, and cost under 0.1 s.
COUNT_PASSES = 20
COUNT_WARMUP = 2
# The unit the document stores: HUNDREDS of bytecodes per file, so the
# shared 1.5 yardstick is 150 bytecodes — about 6% of a transcript parse,
# and a thousand times coarser than the count's own resolution. A count
# is exact, so this gap is sized for interpreter drift (a 3.13.x micro
# release can move a stdlib call's bytecode count by tens), not for
# measurement noise, which does not exist here.
_QUANTUM = Decimal('0.1')
_SCALE = Decimal(100)


class Counts(NamedTuple):
    """One counted run: bytecodes per phase, and the total they partition.

    ``executed`` names the parse-surface functions the run retired an
    instruction in, which is what the surface gate diffs the reachable set
    against (issue #503). It is populated only when the caller supplies
    the code-object table, and an empty one then means "nothing was
    looked for", never "nothing ran".
    """
    available: bool
    reason: str
    phase_bytecodes: dict
    total_bytecodes: int | None
    files: int
    passes: int
    overhead_per_call: int
    executed: frozenset = frozenset()


def partition(total, sniff, parse_file, sidecar) -> dict:
    """The four phases from the three wrapped callables and the total.

    The same algebra for both instruments, so a phase means the same
    thing whichever way it was measured. ``residual`` is the divergence:
    total − parse_file − sidecar, which is every instruction (or every
    CPU second) the pass spent outside the three.
    """
    return {
        'sniff': sniff,
        'parse_body': parse_file - sniff,
        'sidecar': sidecar,
        'residual': total - parse_file - sidecar,
    }


# --- CPU time ----------------------------------------------------------------

@contextlib.contextmanager
def instrumented(totals: dict):
    """Time the callables the real path is made of, then put them back.

    ``totals`` accumulates under the three keys ``partition`` expects
    (``sniff``, ``parse_file``, ``sidecar``).
    """
    originals = (
        (parse, 'sniff_format', parse.sniff_format),
        (parse, 'parse_file', parse.parse_file),
        (agent_sidecar, 'apply_agent_sidecar',
         agent_sidecar.apply_agent_sidecar),
    )

    def timed(name, function):
        def wrapper(*args, **kwargs):
            start = time.process_time()
            try:
                return function(*args, **kwargs)
            finally:
                totals[name] += time.process_time() - start
        return wrapper

    try:
        for module, attribute, _original in originals:
            setattr(module, attribute,
                    timed(_key(attribute), getattr(module, attribute)))
        yield
    finally:
        for module, attribute, original in originals:
            setattr(module, attribute, original)


def _key(attribute):
    return {'sniff_format': 'sniff', 'parse_file': 'parse_file',
            'apply_agent_sidecar': 'sidecar'}[attribute]


# --- instruction counts ------------------------------------------------------

class InstructionCounter:
    """Count the bytecodes retired while a window is open.

    One tool id, one callback, and a stack of counters: the callback
    increments every counter on the stack, so a nested window (the sniff
    inside parse_file) counts into both, and the total under both. That
    is what makes the partition a partition rather than an overlap.

    ``reason`` is empty when the counter is usable, and says why not when
    it is not — an interpreter without ``sys.monitoring``, or another
    tool already holding the id. A counter that cannot count must never
    look like a counter that counted nothing.
    """

    # sys.monitoring is installed by the interpreter, not imported, so a
    # static reader cannot see its members. Every call below is guarded
    # by the availability check that sets `reason`.
    # pylint: disable=no-member
    def __init__(self, name: str = 'reparse-bench', qualnames: dict | None = None):
        # Each key holds a one-element list: a counter the callback can
        # reach without rebinding, and one object per open window.
        self.totals: dict[str, list[int]] = {}
        self.reason = ''
        self._stack: list[list[int]] = []
        # {code object: "module.qualname"} for the parse surface, and the
        # functions the run retired an instruction in. Empty means the
        # caller asked for no surface, which is every caller but the
        # surface gate's — the lookup is skipped entirely then, so the
        # ordinary measurement pays nothing for it.
        self.qualnames: dict = qualnames or {}
        self.executed: set[str] = set()
        # sys.monitoring is installed by the interpreter, not imported;
        # Any says what that is, and `reason` says whether it is there.
        self._monitoring: Any = getattr(sys, 'monitoring', None)
        if self._monitoring is None:
            self.reason = 'this interpreter has no sys.monitoring'
            return
        try:
            self._monitoring.use_tool_id(TOOL_ID, name)
        except ValueError as error:
            self.reason = f'tool id {TOOL_ID} is unavailable: {error}'
            return
        self._monitoring.register_callback(
            TOOL_ID, self._monitoring.events.INSTRUCTION, self._count)
        self._event = self._monitoring.events.INSTRUCTION

    def _count(self, code, instruction_offset):
        if self.qualnames:
            name = self.qualnames.get(code)
            if name is not None:
                self.executed.add(name)
        for cell in self._stack:
            cell[0] += 1

    @property
    def available(self) -> bool:
        return not self.reason

    @property
    def armed(self) -> bool:
        """Whether the interpreter is still firing events at this tool.

        Read back from ``sys.monitoring`` rather than from the window
        stack, because the failure that matters is the events staying
        enabled: the callback would go on running for every instruction
        the rest of the process retires, long after the measurement. A
        leaked window stack would be a wrong number; leaked events are a
        wrong bill.
        """
        if not self.available:
            return False
        return bool(self._monitoring.get_events(TOOL_ID))

    def _open(self, key: str) -> list:
        cell = self.totals.setdefault(key, [0])
        self._stack.append(cell)
        if len(self._stack) == 1:
            self._monitoring.set_events(TOOL_ID, self._event)
        return cell

    def _close(self) -> None:
        self._stack.pop()
        if not self._stack:
            self._monitoring.set_events(TOOL_ID, 0)

    @contextlib.contextmanager
    def window(self, key: str):
        """Count every bytecode retired inside this block, into ``key``."""
        cell = self._open(key)
        try:
            yield cell
        finally:
            self._close()

    def __enter__(self):
        self._open('total')
        return self

    def __exit__(self, *exc_info):
        self._close()
        return False

    def value(self, key: str) -> int:
        """One window's bytecodes so far; 0 for a window never opened."""
        cell = self.totals.get(key)
        return cell[0] if cell else 0

    @property
    def total(self) -> int:
        """The region window's bytecodes: every instruction the pass
        retired, whatever was inside it."""
        return self.value('total')

    def wrap(self, function, key: str):
        """A wrapper counting ``function``'s bytecodes into ``key``."""
        def wrapper(*args, **kwargs):
            with self.window(key):
                return function(*args, **kwargs)
        return wrapper

    def close(self) -> None:
        """Stop counting and give the tool id back.

        Leaving it registered would tax every instruction the rest of the
        process retires, long after the measurement finished.
        """
        if not self.available:
            return
        self._monitoring.set_events(TOOL_ID, 0)
        self._stack.clear()
        self._monitoring.register_callback(TOOL_ID, self._event, None)
        self._monitoring.free_tool_id(TOOL_ID)
        self.reason = 'closed'

    def wrapper_overhead(self, calls: int = 2000) -> int:
        """Bytecodes one wrapped call adds: what the count prices for the
        harness measuring it, measured rather than assumed. The same
        no-op is called wrapped and unwrapped, and the difference is the
        harness."""
        def nothing():
            return None

        plain = self.totals.setdefault('overhead_plain', [0])
        wrapped = self.totals.setdefault('overhead_wrapped', [0])
        with self.window('overhead_plain'):
            for _ in range(calls):
                nothing()
        wrapped_call = self.wrap(nothing, 'overhead_wrapped')
        for _ in range(calls):
            wrapped_call()
        return max(wrapped[0] - plain[0], 0) // calls


def measure_counts(entries, run_pass, passes: int = COUNT_PASSES,
                   warmup: int = COUNT_WARMUP,
                   qualnames: dict | None = None) -> Counts:
    """Count `passes` passes over the corpus, split by phase.

    `qualnames` is the surface gate's `{code object: "module.qualname"}`
    table; supplying it makes the run also report which surface functions
    it retired an instruction in.

    An unusable counter returns ``available=False`` with the reason and
    no numbers — never zeros, which would read as "this phase retires no
    bytecodes" and quietly tighten a floor.
    """
    counter = InstructionCounter(qualnames=qualnames)
    if not counter.available:
        return Counts(False, counter.reason, {}, None, len(entries),
                      passes, 0)
    originals = (
        (parse, 'sniff_format', parse.sniff_format),
        (parse, 'parse_file', parse.parse_file),
        (agent_sidecar, 'apply_agent_sidecar',
         agent_sidecar.apply_agent_sidecar),
    )
    try:
        for _ in range(warmup):
            run_pass(entries)
        try:
            for module, attribute, _original in originals:
                setattr(module, attribute,
                        counter.wrap(getattr(module, attribute),
                                     _key(attribute)))
            with counter:
                for _ in range(passes):
                    run_pass(entries)
        finally:
            for module, attribute, original in originals:
                setattr(module, attribute, original)
        overhead = counter.wrapper_overhead()
        # A phase the corpus never reaches (the sidecar step, with no
        # meta.json committed) retires no bytecodes: zero is its reading,
        # not a missing one.
        counted = partition(counter.total, counter.value('sniff'),
                            counter.value('parse_file'),
                            counter.value('sidecar'))
        total_bytecodes = counter.total
        executed = frozenset(counter.executed)
    finally:
        counter.close()
    return Counts(True, '', counted, total_bytecodes,
                  len(entries), passes, overhead, executed)


def bytecodes_per_file(counts: Counts) -> dict:
    """Each phase's bytecodes per file, in the recorded unit."""
    divisor = Decimal(counts.passes * counts.files) * _SCALE
    return {phase: (Decimal(counts.phase_bytecodes[phase]) / divisor
                    ).quantize(_QUANTUM) for phase in PHASES}
