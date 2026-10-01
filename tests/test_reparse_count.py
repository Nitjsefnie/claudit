"""Tests for the reparse bench's instruction-count instrument (issue #436).

The share of a phase is scale-free inside one run and catches work
MOVING between phases; it cannot catch the parse getting slower as a
whole, because every phase grows together. This instrument does: the
bytecodes a phase retires per file, counted with ``sys.monitoring``'s
INSTRUCTION event, which is exact — the same tree retires the same
number of bytecodes on a loaded machine and an idle one, under any
``PYTHONHASHSEED``. Nothing here pins a measured number: what the cases
pin is that the count is exact, that the phases partition it, that it
counts only what is inside a window, and that an unusable counter says
so instead of reporting zeros.
"""
from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load(name="reparse_bench"):
    """Import a scripts/ci module by path.

    scripts/ci is not a package and deliberately has no __init__.py, so
    the loader is the same by-path dance the sibling CI-script tests use;
    the directory goes on sys.path first so the module's own importlib
    imports of its siblings resolve.
    """
    if str(REPO_ROOT / "scripts" / "ci") not in sys.path:
        sys.path.insert(0, str(REPO_ROOT / "scripts" / "ci"))
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "scripts" / "ci" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bench = _load()
phases = _load("reparse_phases")
report_module = _load("reparse_report")
thresholds = sys.modules["thresholds"]


def _counts(**kwargs):
    """A short instruction-count measurement, the shape tests pin.

    The count is exact, so these assert EQUALITY, never a tolerance: a
    deterministic count is the whole reason it is worth ratcheting.
    """
    kwargs.setdefault("passes", 3)
    return phases.measure_counts(
        bench.corpus(), bench.run_pass, **kwargs)


def test_phase_counts_partition_the_pass_exactly():
    # The counts, like the times, are a partition: the four phases sum to
    # the measured total with nothing left over and nothing counted
    # twice. A sum that does not add up would be a hole in the instrument.
    counts = _counts()
    assert set(counts.phase_bytecodes) == set(bench.PHASES)
    assert sum(counts.phase_bytecodes.values()) == counts.total_bytecodes


def test_parse_body_is_parse_file_minus_the_sniff():
    counts = _counts()
    assert counts.phase_bytecodes["parse_body"] > 0
    assert (counts.phase_bytecodes["parse_body"]
            + counts.phase_bytecodes["sniff"]
            + counts.phase_bytecodes["sidecar"]) <= counts.total_bytecodes


def test_residual_is_measured_not_assumed():
    # Whatever the three wrapped callables do not account for is a real,
    # non-zero remainder on the count side too, and it is a phase.
    counts = _counts()
    assert counts.phase_bytecodes["residual"] > 0


def test_sidecar_phase_is_measured_when_a_sidecar_exists():
    entry = bench.corpus()[0]
    assert entry.sidecar_key is None
    sided = entry._replace(
        sidecar_key=f"{entry.key}.meta.json",
        sidecar_blob=b'{"agentType": "bench-sidecar-role"}')
    counts = phases.measure_counts([sided], bench.run_pass, passes=3)
    assert counts.phase_bytecodes["sidecar"] > 0


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

        work(1000)                      # warm, outside every window
        with counter.window("inside"):
            work(5000)
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
    counts = phases.measure_counts(bench.corpus(), bench.run_pass,
                                   passes=2)
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
    for phase, value in per_file.items():
        assert value.as_tuple().exponent == -1
        assert value >= 0
    # The sidecar step reads 0.0 on this corpus (no meta.json committed),
    # which is a real reading; the parse body is not.
    assert per_file["sidecar"] == Decimal("0.0")
    assert per_file["parse_body"] > 0
    expected = (Decimal(counts.phase_bytecodes["parse_body"])
                / Decimal(counts.passes * counts.files) / 100)
    assert per_file["parse_body"] == expected.quantize(Decimal("0.1"))
