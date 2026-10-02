"""Tests for the reparse bench's GATE — what it compares, and what it refuses.

Split out of `test_reparse_bench.py` (issue #513): that file pins the
bench's SHAPE — the corpus it walks, the code path it drives, the
partition it reports and the arithmetic behind the recorded numbers —
while this one pins the step CI runs against its output. Since #513 the
gate holds a phase to its BYTECODE budget alone and the per-phase share
is telemetry, so the enforced set, its reader and the fail-closed rule
for an absent count all live here.
"""
from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
# The same by-path loader the sibling CI-script suites use: scripts/ci is
# not a package and deliberately has no __init__.py, and the directory
# goes on sys.path first so a module's own importlib imports resolve.
SHAPE_PASSES = 4000


def _load(name):
    if str(REPO_ROOT / "scripts" / "ci") not in sys.path:
        sys.path.insert(0, str(REPO_ROOT / "scripts" / "ci"))
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "scripts" / "ci" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bench = _load("reparse_bench")
report_module = _load("reparse_report")
thresholds = sys.modules["thresholds"]


def _short(**kwargs):
    """A measurement too short to be a recorded number, fast enough for
    a test: the shape is what these assert, never the value."""
    kwargs.setdefault("passes", SHAPE_PASSES)
    kwargs.setdefault("warmup", 1)
    return bench.measure(bench.corpus(), **kwargs)


def _document(reparse):
    return {
        "schema_version": 1,
        "coverage": {
            "python": {"measured": Decimal("92.6"), "floor": Decimal("91.1")},
            "javascript": {"measured": Decimal("50.0"),
                           "floor": Decimal("48.5")},
        },
        "reparse": reparse,
        "module_size_baseline": {},
        "pylint_suppression_baseline": {},
        # The loader requires EVERY top-level family, so a synthetic
        # reparse document must also carry a valid suite_cost family. It
        # is inert here — nothing in this module reads it.
        "suite_cost": {
            phase: {"measured": Decimal("10.0"),
                    "floor": Decimal("10.0") + Decimal("1.5")}
            for phase in thresholds.SUITE_COST_PHASES
        },
    }


def _budgets(measured="10.0", **overrides):
    """A synthetic family: both metrics for every phase, one override
    set per call."""
    budgets = {
        phase: {metric: {"measured": Decimal(measured),
                         "floor": Decimal(measured) + Decimal("1.5")}
                for metric in thresholds.REPARSE_METRICS}
        for phase in thresholds.REPARSE_PHASES
    }
    for label, value in overrides.items():
        phase, metric = label.split(".")
        budgets[phase][metric] = {"measured": Decimal(value),
                                  "floor": Decimal(value) + Decimal("1.5")}
    return budgets


def _measurement_with(shares, counted=None):
    """A measurement whose phases read the given values, under both
    instruments."""
    measurement = _short()
    return measurement._replace(
        shares=shares,
        phase_cpu_s={name: 0.0 for name in bench.PHASES},
        instruction_per_file=(dict.fromkeys(bench.PHASES, None) if counted
                              is None else counted))


# --- the phase table (issue #490) ---------------------------------------------

def _report_lines(counted=None):
    """The report block for a synthetic measurement: shares of 9.0%
    apiece and, unless a per-phase mapping is given, 1.0 counted apiece.
    Pass a dict of Nones for the instrument-absent shape."""
    shares = {name: Decimal("9.0") for name in bench.PHASES}
    if counted is None:
        counted = {name: Decimal("1.0") for name in bench.PHASES}
    return report_module.report(
        _measurement_with(shares, counted)).splitlines()


def test_report_phase_table_is_markdown():
    """Padded markdown: readable in the monospace job log, and pasted
    elsewhere it renders as a table (issue #490)."""
    lines = _report_lines()
    header, separator, *rows = [
        line for line in lines if line.startswith("|")]
    assert [cell.strip() for cell in header.strip("|").split("|")] == [
        "phase", "share", "ms/file", "bytecode_hundreds_per_file"]
    assert set(separator) <= set("|-: ")
    assert {row.split("|")[1].strip() for row in rows} == set(bench.PHASES)
    assert lines[lines.index(header) - 1] == "", (
        "a blank line separates the prose intro from the table, so the "
        "table renders when the block is pasted as markdown")


def test_report_table_stays_monospace_aligned():
    """Padded cells keep the job log's columns: every table line is one
    visual width, the numeric columns right-aligned, the phase column
    left-aligned."""
    lines = _report_lines(counted={name: None for name in bench.PHASES})
    table = [line for line in lines if line.startswith("|")]
    assert len({len(line) for line in table}) == 1
    assert all(cell.strip().endswith(":")
               for cell in table[1].strip("|").split("|")[1:]), (
        "the numeric columns are right-aligned")
    # A rjust->ljust mutant on the data cells survives the width and
    # separator pins above: the padding SIDE is its own assertion.
    row = next(line for line in table[2:] if "sniff" in line)
    cells = row.split("|")
    assert cells[1].startswith(" sniff "), cells
    assert cells[2].endswith("9.0% "), cells
    assert cells[3].endswith("0.0000 "), cells
    assert cells[4].endswith("- "), cells


def test_report_marks_an_uncounted_phase_with_a_dash():
    row = next(line for line in _report_lines(
                   counted={name: None for name in bench.PHASES})
               if line.startswith("|") and "parse_body" in line)
    assert row.strip("| ").split("|")[-1].strip() == "-"


def test_report_shows_a_phase_counted_value_in_its_row():
    counted = {name: Decimal("1.0") for name in bench.PHASES}
    counted["parse_body"] = Decimal("40.0")
    row = next(line for line in _report_lines(counted=counted)
               if line.startswith("|") and "parse_body" in line)
    assert row.strip("| ").split("|")[-1].strip() == "40.0"


def test_report_totals_and_instrument_notes_stay_prose():
    """The intro, the sum and the two instrument notes are not rows of
    the table (issue #490 keeps them prose)."""
    prose = [line for line in _report_lines()
             if not line.startswith("|")]
    assert prose[0].startswith("reparse CPU")
    assert any(line.lstrip().startswith("sum ") for line in prose)
    assert any(line.lstrip().startswith("bytecodes:") for line in prose)
    assert any(line.lstrip().startswith("perf:") for line in prose)


def test_check_passes_while_every_phase_is_within_its_floor(tmp_path):
    thresholds_path = tmp_path / "ci-thresholds.json"
    thresholds.write(thresholds_path, _document(_budgets()))
    path = tmp_path / "m.json"
    report_module.write_measurement(path, _measurement_with(
        {name: Decimal("9.0") for name in bench.PHASES},
        {name: Decimal("1.0") for name in bench.PHASES}))
    assert bench.main(["--check", str(path), "--thresholds",
                       str(thresholds_path)]) == 0


def test_check_fails_on_a_phase_over_its_bytecode_floor(tmp_path, capsys):
    # The count is what catches a uniform slowdown, where every share
    # stays put, so it is the one the gate holds a phase to.
    thresholds_path = tmp_path / "ci-thresholds.json"
    thresholds.write(thresholds_path, _document(_budgets()))
    counted = {name: Decimal("1.0") for name in bench.PHASES}
    counted["parse_body"] = Decimal("40.0")
    path = tmp_path / "m.json"
    report_module.write_measurement(path, _measurement_with(
        {name: Decimal("9.0") for name in bench.PHASES}, counted))
    assert bench.main(["--check", str(path), "--thresholds",
                       str(thresholds_path)]) == 1
    err = capsys.readouterr().err
    assert "parse_body: 40.0" in err
    assert "sniff" not in err, "only the offending phase is named"
    assert "never raised by hand" in err


def test_check_ignores_a_share_over_its_floor(tmp_path, capsys):
    """A share no longer gates (issue #513) — it is telemetry.

    A phase share is a proportion of a timed run: its runner-to-runner
    spread measures wider than the 1.5-point gap it is compared against
    (#500, #506), and it moves when the corpus MIX shifts between
    formats of different parse cost even with no code path slower and
    every count under its ceiling (PR #512). The recorded share budgets
    stay in the document as the reading they recorded; nothing is
    compared against them.
    """
    thresholds_path = tmp_path / "ci-thresholds.json"
    thresholds.write(thresholds_path, _document(_budgets()))
    shares = {name: Decimal("99.9") for name in bench.PHASES}
    path = tmp_path / "m.json"
    report_module.write_measurement(path, _measurement_with(
        shares, {name: Decimal("1.0") for name in bench.PHASES}))
    assert bench.main(["--check", str(path), "--thresholds",
                       str(thresholds_path)]) == 0
    out = capsys.readouterr().out
    assert "99.9" in out, "the share is still printed, as telemetry"


def test_check_passes_a_share_over_floor_against_the_COMMITTED_budgets(tmp_path):
    """The same claim against the real document, whose values the ratchet
    moves on master — so nothing here is pinned, only the invariant: a
    count of zero is under any floor a positive measured value can
    produce (thresholds.py refuses floor <= measured, and a measured
    count is never negative)."""
    path = tmp_path / "m.json"
    report_module.write_measurement(path, _measurement_with(
        {name: Decimal("99.9") for name in bench.PHASES},
        {name: Decimal("0.0") for name in bench.PHASES}))
    assert bench.main(["--check", str(path),
                       "--thresholds", str(thresholds.THRESHOLDS)]) == 0


def test_check_fails_when_the_gated_count_was_not_measured(tmp_path, capsys):
    """Fail closed: the count is the ONLY instrument the gate holds, so an
    absent one is an absent gate, not a passing one. An interpreter
    without `sys.monitoring` must not silence the step, and the reason
    the bench recorded has to travel with the refusal."""
    thresholds_path = tmp_path / "ci-thresholds.json"
    thresholds.write(thresholds_path, _document(_budgets()))
    path = tmp_path / "m.json"
    report_module.write_measurement(path, _measurement_with(
        {name: Decimal("9.0") for name in bench.PHASES})._replace(
            instruction_counts={'available': False,
                                'reason': 'no INSTRUCTION event'},
            instruction_note='no INSTRUCTION event'))
    assert bench.main(["--check", str(path), "--thresholds",
                       str(thresholds_path)]) == 1
    err = capsys.readouterr().err
    assert "NOT MEASURED" in err
    assert "no INSTRUCTION event" in err


def test_check_names_the_offending_phase(tmp_path, capsys):
    thresholds_path = tmp_path / "ci-thresholds.json"
    thresholds.write(thresholds_path, _document(_budgets()))
    counted = {name: Decimal("1.0") for name in bench.PHASES}
    counted["sniff"] = Decimal("40.0")
    path = tmp_path / "m.json"
    report_module.write_measurement(path, _measurement_with(
        {name: Decimal("9.0") for name in bench.PHASES}, counted))
    assert bench.main(["--check", str(path), "--thresholds",
                       str(thresholds_path)]) == 1
    err = capsys.readouterr().err
    assert "sniff: 40.0" in err
    assert "never raised by hand" in err


def test_the_report_calls_the_share_telemetry_not_a_budget():
    """The printed label is what a reader takes the number's status from;
    a share printed in the same column as the gated counts reads as one."""
    measurement = _measurement_with(
        {name: Decimal("9.0") for name in bench.PHASES},
        {name: Decimal("1.0") for name in bench.PHASES})
    text = report_module.report(measurement)
    assert "telemetry" in text
    assert "share" in text


def test_the_gate_holds_exactly_the_bytecode_budgets():
    """The enforced set is named in one place, and the share is not in
    it — so a later metric cannot join the gate by being added to the
    recorded one."""
    assert thresholds.REPARSE_GATED_METRICS == ("bytecodes",)
    assert "share" in thresholds.REPARSE_METRICS, (
        "the share stays recorded; it only stops gating")


def test_the_gate_reads_the_enforced_set_rather_than_a_hardcoded_metric(
        tmp_path, monkeypatch):
    """A metric that joins the enforced set with no reader fails the gate.

    The gate body used to spell 'bytecodes', so the constant the doctrine
    names as the enforced set was consulted only by the ratchet: widening
    it tightened shares again while the gate kept passing on the count it
    read by name. Reading through the constant binds the two, and a gated
    metric the gate cannot read refuses rather than passing silently —
    the shape of the defect issue #513 exists to close.
    """
    thresholds_path = tmp_path / "ci-thresholds.json"
    thresholds.write(thresholds_path, _document(_budgets()))
    path = tmp_path / "m.json"
    report_module.write_measurement(path, _measurement_with(
        {name: Decimal("9.0") for name in bench.PHASES},
        {name: Decimal("1.0") for name in bench.PHASES}))
    monkeypatch.setattr(
        thresholds, "REPARSE_GATED_METRICS", ("bytecodes", "share"))
    with pytest.raises(ValueError, match="no reader for the gated metric"):
        report_module.check(path, thresholds_path)
