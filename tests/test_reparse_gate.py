"""Tests for the reparse measurement's phase table and telemetry labels."""
from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path

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


def _short(**kwargs):
    """A measurement too short to be a recorded number, fast enough for
    a test: the shape is what these assert, never the value."""
    kwargs.setdefault("passes", SHAPE_PASSES)
    kwargs.setdefault("warmup", 1)
    return bench.measure(bench.corpus(), **kwargs)


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


def test_the_report_calls_the_share_telemetry_not_a_budget():
    """The printed label is what a reader takes the number's status from;
    a share printed in the same column as the gated counts reads as one."""
    measurement = _measurement_with(
        {name: Decimal("9.0") for name in bench.PHASES},
        {name: Decimal("1.0") for name in bench.PHASES})
    text = report_module.report(measurement)
    assert "telemetry" in text
    assert "share" in text
