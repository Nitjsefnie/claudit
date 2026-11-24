"""Reparse phase counts form an exact partition measured in bytecode instructions."""
from __future__ import annotations

import contextlib
import gc
import queue
import sys
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from types import SimpleNamespace
from typing import cast

import pytest

from backend import agent_sidecar, pricing
from tests.ci_script import CI, load_ci_script

# The bench's own importlib imports of its siblings resolve through this.
sys.path.insert(0, str(CI))
bench = load_ci_script("reparse_bench")
phases = load_ci_script("reparse_phases")
report_module = load_ci_script("reparse_report")
thresholds = sys.modules["thresholds"]


def _counts(**kwargs):
    """A short instruction-count measurement, the shape tests pin.

    The count is exact, so these assert EQUALITY, never a tolerance: a
    deterministic count is the whole reason it is worth ratcheting.
    """
    kwargs.setdefault("passes", 3)
    return phases.measure_counts(bench.corpus(), bench.run_pass, **kwargs)


def test_phase_counts_partition_the_pass_exactly():
    # The counts, like the times, are a partition: the four phases sum to
    # the measured total with nothing left over and nothing counted
    # twice. A sum that does not add up would be a hole in the instrument.
    counts = _counts()
    assert set(counts.phase_bytecodes) == set(bench.PHASES)
    assert sum(counts.phase_bytecodes.values()) == counts.total_bytecodes


def test_a_cold_count_never_prices_the_rate_load(monkeypatch):
    warm = _counts(passes=1, warmup=1)
    for name in pricing.RateTables.__annotations__:
        monkeypatch.delattr(pricing, name)
    assert _counts(passes=1, warmup=0).total_bytecodes == warm.total_bytecodes


def test_another_threads_work_changes_no_count():
    entries = bench.corpus()

    def elsewhere():
        return None

    def count(arrived, release):
        def run_pass(corpus):
            release.put(None)
            arrived.get(timeout=60)
            return bench.run_pass(corpus)

        return phases.measure_counts(entries, run_pass, passes=2, warmup=1, qualnames={
            bench.run_pass.__code__: "pass", elsewhere.__code__: "elsewhere"})

    # One queue both ways answers the pass itself, at the same bytecodes.
    loopback = queue.SimpleQueue()
    alone = count(loopback, loopback)
    assert alone.executed == {"pass"}, "the surface table is live"

    arrived, release = queue.SimpleQueue(), queue.SimpleQueue()

    def park(_key):
        arrived.put(None)
        release.get(timeout=60)

    def foreign():
        release.get(timeout=60)
        for _ in range(3):
            elsewhere()
            bench.run_pass(entries)
            # Parked inside this call while the pass runs; left in the next
            # pass, the last one after the counter closed.
            agent_sidecar.apply_agent_sidecar(cast(dict, SimpleNamespace(get=park)), b"", "")

    with ThreadPoolExecutor(1) as pool:
        worker = pool.submit(foreign)
        # A foreign failure ends the wait.
        worker.add_done_callback(lambda _: [arrived.put(None) for _ in range(3)])
        try:
            beside = count(arrived, release)
        finally:
            for _ in range(4):
                release.put(None)
    try:
        assert beside == alone
    finally:
        worker.result()


@pytest.fixture
def collector():
    before, thresholds_before = gc.isenabled(), gc.get_threshold()
    yield
    gc.set_threshold(*thresholds_before)
    (gc.enable if before else gc.disable)()


class _Cycle:
    def __init__(self):
        self.x = self

    def __del__(self):
        self.x = 1


@pytest.mark.usefixtures("collector")
def test_a_due_collection_changes_no_count():
    def count():
        calls = iter(["warmup"])

        def run_pass(corpus):
            bench.run_pass(corpus)
            if next(calls, None):
                gc.collect()
                for _ in range(10):
                    _Cycle()

        return phases.measure_counts(bench.corpus(), run_pass, passes=1, warmup=1)

    gc.disable()
    without_collections = count()
    gc.enable()
    gc.set_threshold(60)
    assert count() == without_collections


@pytest.mark.usefixtures("collector")
@pytest.mark.parametrize("enabled", (True, False))
def test_a_count_restores_the_callers_collector(monkeypatch, enabled):
    callers, opposite = (gc.enable, gc.disable) if enabled else (gc.disable, gc.enable)

    def count(failing=None):
        calls = iter(["warmup", "window"])

        def run_pass(_corpus):
            opposite()
            if next(calls) == failing:
                raise RuntimeError(failing)

        callers()
        with pytest.raises(RuntimeError) if failing else contextlib.nullcontext():
            return phases.measure_counts([], run_pass, passes=1, warmup=1)

    for failing in (None, "warmup", "window"):
        count(failing)
        assert gc.isenabled() is enabled, failing
    monkeypatch.setattr(phases, "TOOL_ID", -1)
    assert not count().available
    assert gc.isenabled() is enabled, "unavailable"


def test_counting_counts_only_inside_its_window():
    # Work outside the window must not reach the counter, or the number
    # would price the harness as well as the parse.
    counter = phases.InstructionCounter()
    assert not counter.reason, counter.reason
    try:
        def work(size):
            total = 0
            for i in range(size):
                total += i
            return total

        assert not counter.armed, "a fresh counter is not counting"
        work(1000)                      # warm, outside every window
        with counter.window("inside"):
            assert counter.armed, "the window counts while it is open"
            work(5000)
        assert not counter.armed, "the window stops counting when it closes"
        inside = counter.value("inside")
        assert inside > 0
        # Work outside every window reaches no counter: an identical call
        # in the open adds nothing to any key.
        work(5000)
        assert counter.value("inside") == inside
        # The region's own window prices the same work, to within the
        # instructions its own open and close retire — a different path
        # through the counter's entry, so it can land a few either side.
        # That difference is what wrapper_overhead() measures, and it is
        # why a nested phase can be subtracted out of the total.
        with counter:
            work(5000)
        assert abs(counter.total - inside) <= counter.wrapper_overhead() * 4
    finally:
        counter.close()


def test_the_counter_releases_its_tool_id():
    # A tool id left registered would tax every later instruction in the
    # process, long after the bench had finished measuring.
    # sys.monitoring is installed by the interpreter, not imported, so a
    # static reader cannot see its members.
    # pylint: disable=no-member
    counter = phases.InstructionCounter()
    counter.close()
    sys.monitoring.use_tool_id(phases.TOOL_ID, "probe-after-close")
    sys.monitoring.free_tool_id(phases.TOOL_ID)


def test_an_unavailable_counter_says_why_and_measures_nothing(monkeypatch):
    # Another tool holding the id, or an interpreter without
    # sys.monitoring, must be a loud no-op rather than a silent zero.
    def refuse(_tool, _name):
        raise ValueError("tool id 3 is already in use")

    monkeypatch.setattr(sys.monitoring, "use_tool_id", refuse)
    counts = phases.measure_counts(bench.corpus(), bench.run_pass, passes=2)
    assert not counts.available
    assert "already in use" in counts.reason
    assert counts.total_bytecodes is None


def test_bytecodes_per_file_is_recorded_in_hundreds():
    # The unit the document stores: hundreds of bytecodes per file, one
    # decimal place, so the shared 1.5 yardstick is 150 bytecodes rather
    # than 1.5 of one.
    counts = _counts(passes=2)
    per_file = phases.bytecodes_per_file(counts)
    assert set(per_file) == set(bench.PHASES)
    for value in per_file.values():
        assert value.as_tuple().exponent == -1
        assert value >= 0
    # The mirror carries a meta.json beside one lane wire, so the sidecar
    # reads nonzero; a sidecar-less corpus must read 0.0.
    assert per_file["sidecar"] > 0
    assert per_file["parse_body"] > 0
    unpaired = [entry for entry in bench.corpus() if entry.sidecar_key is None]
    assert unpaired, "the mirror carries no sidecar-less transcript"
    bare = phases.bytecodes_per_file(phases.measure_counts(unpaired[:1], bench.run_pass, passes=2))
    assert bare["sidecar"] == Decimal("0.0"), (
        "a corpus with no sidecar retired bytecodes in the sidecar phase")
    assert bare["parse_body"] > 0
    expected = (Decimal(counts.phase_bytecodes["parse_body"])
                / Decimal(counts.passes * counts.files) / 100)
    assert per_file["parse_body"] == expected.quantize(Decimal("0.1"))
