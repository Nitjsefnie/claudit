"""Linear suite-cost ceilings and workload drift reporting."""
from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path

import pytest


@pytest.fixture(name="report")
def report_fixture():
    ci = Path(__file__).resolve().parents[1] / "scripts" / "ci"
    for name in ("thresholds", "suite_report"):
        spec = importlib.util.spec_from_file_location(name, ci / f"{name}.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules["suite_report"]


def _budgets(per_line=200, seed=10000):
    budgets: dict = {
        phase: {"measured": Decimal("10.0"), "floor": Decimal("11.5")}
        for phase in ("collection", "run", "residual")
    }
    if per_line is not None:
        for phase in ("collection", "run", "residual"):
            budgets[phase]["per_line"] = per_line
    if seed is not None:
        budgets["tests_tree_lines"] = seed
    return budgets


def _measurement(report, lines=20000, count="12.0"):
    return report.Measurement(
        "instruction_count", "0", 1,
        {phase: Decimal(count) for phase in report.PHASES},
        {phase: "0.1" for phase in report.PHASES}, "fixture", "python", lines)


def _thresholds_file(report, tmp_path, budgets):
    thresholds = report.thresholds
    doc = {
        "schema_version": 1,
        "coverage": {
            language: {"measured": Decimal("90.0"), "floor": Decimal("88.5")}
            for language in thresholds.COVERAGE_LANGUAGES
        },
        "suite_cost": budgets,
        "reparse": {
            phase: {
                metric: {"measured": Decimal("10.0"),
                         "floor": Decimal("11.5")}
                for metric in thresholds.REPARSE_METRICS
            }
            for phase in thresholds.REPARSE_PHASES
        },
        **{member: {} for member in thresholds.BASELINE_MEMBERS},
    }
    target = tmp_path / "thresholds.json"
    thresholds.write(target, doc, False)
    return target


@pytest.mark.parametrize("per_line, verdict, note", [
    (200, 0, "growth is budgeted"),
    (0, 1, "is the remedy"),
    (None, 1, "is the remedy"),
])
def test_gate_budgets_growth_only_with_positive_slope(
        report, tmp_path, capsys, per_line, verdict, note):
    measurement = tmp_path / "measurement.json"
    report.write_measurement(measurement, _measurement(report))
    target = _thresholds_file(report, tmp_path, _budgets(per_line))
    assert report.check(measurement, target) == verdict
    output = capsys.readouterr()
    assert "workload identity drifted" in output.out + output.err
    assert note in output.out + output.err


def test_breach_names_effective_ceiling_and_growth(report, tmp_path, capsys):
    measurement = tmp_path / "measurement.json"
    report.write_measurement(measurement, _measurement(report, count="13.6"))
    target = _thresholds_file(report, tmp_path, _budgets())
    assert report.check(measurement, target) == 1
    error = capsys.readouterr().err
    assert "growth is budgeted" in error
    for phase in report.PHASES:
        line = next(line for line in error.splitlines()
                    if line.strip().startswith(f"{phase}:"))
        assert "ceiling 13.5" in line
        assert "growth 2" in line


@pytest.mark.parametrize("lines, seed", [(None, 10000), (20000, None)])
def test_missing_identity_disables_growth(report, lines, seed):
    counts = {phase: Decimal("12.0") for phase in report.PHASES}
    if lines is not None:
        counts["tests_tree_lines"] = lines
    assert len(report._over_budget(counts, _budgets(seed=seed))) == 3  # pylint: disable=protected-access


@pytest.mark.parametrize("lines", [10000, 9000])
def test_equal_or_smaller_tree_keeps_seed_ceiling(report, lines):
    counts = {phase: Decimal("11.6") for phase in report.PHASES}
    counts["tests_tree_lines"] = lines
    breaches = report._over_budget(counts, _budgets())  # pylint: disable=protected-access
    assert len(breaches) == 3
    assert all("ceiling 11.5" in line for line in breaches)
    assert all("growth" not in line for line in breaches)


def test_growth_ceiling_keeps_exact_decimal_boundary(report):
    counts: dict = {phase: Decimal("11.500004") for phase in report.PHASES}
    counts["tests_tree_lines"] = 10001
    assert report._over_budget(counts, _budgets(per_line=3))  # pylint: disable=protected-access
    counts = {phase: Decimal("11.500003") for phase in report.PHASES}
    counts["tests_tree_lines"] = 10001
    assert report._over_budget(counts, _budgets(per_line=3)) == []  # pylint: disable=protected-access


def test_drift_note_names_each_positive_slope_and_growth(report):
    budgets = _budgets()
    budgets["run"]["per_line"] = 300
    note = report._workload_drift_note(_measurement(report), budgets)  # pylint: disable=protected-access
    assert note is not None
    assert "200 instructions per line" in note
    assert "300 instructions per line" in note
    assert "2 million_instructions" in note
    assert "3 million_instructions" in note
    assert "seed candidate" in note
    assert "is the remedy" not in note


@pytest.mark.parametrize("per_line", [0, None])
def test_zero_or_absent_slope_keeps_old_drift_note(report, per_line):
    note = report._workload_drift_note(_measurement(report), _budgets(per_line))  # pylint: disable=protected-access
    assert note is not None
    assert "the budgets describe a different workload" in note
    assert "is the remedy" in note


@pytest.mark.parametrize("lines, seed", [
    (None, 10000), (20000, None), (10000, 10000),
])
def test_drift_note_needs_differing_identities(report, lines, seed):
    assert report._workload_drift_note(  # pylint: disable=protected-access
        _measurement(report, lines=lines), _budgets(seed=seed)) is None
