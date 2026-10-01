"""Tests for the coverage ratchet.

The ratchet moves the recorded calibration in one direction only: a run
whose measured coverage sits at most the hysteresis above the recorded
measured justifies no raise, and a measurement below the recorded value
never lowers anything. The cases below pin both directions, the
hysteresis boundary, the one-decimal measurement contract and the CLI's
file-writing behaviour.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "ci"))

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    """Import a scripts/ci module by path.

    scripts/ci is not a package and deliberately has no __init__.py — it
    holds standalone CI entry points, not an importable library. The
    directory itself goes on sys.path first, so the module's own
    ``importlib`` import of its sibling resolves.
    """
    path = REPO_ROOT / "scripts" / "ci" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _thresholds():
    return _load("thresholds")


def _ratchet():
    return _load("ratchet")


def _reparse_family(shares=None):
    """A synthetic reparse family: both metrics for every bench phase."""
    shares = shares or {}
    return {
        phase: {
            metric: {
                "measured": Decimal(shares.get(f"{phase}.{metric}", "10.0")),
                "floor": Decimal(
                    shares.get(f"{phase}.{metric}", "10.0")) + Decimal("1.5"),
            }
            for metric in _thresholds().REPARSE_METRICS
        }
        for phase in _thresholds().REPARSE_PHASES
    }


def _document(measured="92.6", floor="91.1"):
    return {
        "schema_version": 1,
        "coverage": {
            "python": {
                "measured": Decimal(measured),
                "floor": Decimal(floor),
            },
            "javascript": {
                "measured": Decimal("50.0"),
                "floor": Decimal("48.5"),
            },
        },
        "reparse": _reparse_family(),
        "module_size_baseline": {},
        "pylint_suppression_baseline": {},
        "suite_cost": {
            "collection": {"measured": Decimal("25.8"),
                           "floor": Decimal("27.3")},
            "run": {"measured": Decimal("309.8"),
                    "floor": Decimal("311.3")},
            "residual": {"measured": Decimal("6.5"),
                         "floor": Decimal("8.0")},
        },
    }


def _written(tmp_path, **kwargs):
    thresholds = _thresholds()
    target = tmp_path / "ci-thresholds.json"
    thresholds.write(target, _document(**kwargs))
    return target


def test_no_raise_within_hysteresis():
    ratchet = _ratchet()
    doc = _document()
    measured = Decimal("94.1")  # recorded 92.6 + hysteresis 1.5, not over
    assert ratchet.update(doc, measured) is None


def test_raise_beyond_hysteresis():
    ratchet = _ratchet()
    doc = _document()
    updated = ratchet.update(doc, Decimal("94.2"))
    assert updated is not None
    record = updated["coverage"]["python"]
    assert record["measured"] == Decimal("94.2")
    assert record["floor"] == Decimal("92.7")
    # Nothing outside the raised calibration moved.
    assert updated["schema_version"] == 1
    assert updated["module_size_baseline"] == {}
    assert updated["coverage"]["javascript"] == {
        "measured": Decimal("50.0"),
        "floor": Decimal("48.5"),
    }


def test_raise_beyond_hysteresis_javascript():
    ratchet = _ratchet()
    doc = _document()
    updated = ratchet.update(doc, Decimal("52.0"), language="javascript")
    assert updated is not None
    record = updated["coverage"]["javascript"]
    assert record["measured"] == Decimal("52.0")
    assert record["floor"] == Decimal("50.5")
    # Only the raised calibration moved.
    assert updated["coverage"]["python"] == {
        "measured": Decimal("92.6"),
        "floor": Decimal("91.1"),
    }
    assert updated["module_size_baseline"] == {}


def test_no_raise_within_hysteresis_javascript():
    ratchet = _ratchet()
    measured = Decimal("51.5")  # recorded 50.0 + hysteresis 1.5, not over
    assert ratchet.update(
        _document(), measured, language="javascript") is None


def test_measured_below_recorded_never_lowers():
    ratchet = _ratchet()
    doc = _document()
    assert ratchet.update(doc, Decimal("50.0")) is None


def test_measured_with_two_decimals_rejected():
    ratchet = _ratchet()
    with pytest.raises(ValueError, match="exactly one decimal place"):
        ratchet.update(_document(), Decimal("94.25"))


def test_floor_for_is_measured_minus_gap():
    ratchet = _ratchet()
    assert ratchet.floor_for(Decimal("92.6")) == Decimal("91.1")


def test_main_no_raise_leaves_file_untouched(tmp_path, capsys):
    ratchet = _ratchet()
    target = _written(tmp_path)
    before = target.read_text(encoding="utf-8")
    assert ratchet.main(
        ["--measured", "94.1", "--thresholds", str(target)]) == 0
    assert target.read_text(encoding="utf-8") == before
    assert "justifies no raise" in capsys.readouterr().out


def test_main_raise_rewrites_the_file(tmp_path, capsys):
    thresholds = _thresholds()
    ratchet = _ratchet()
    target = _written(tmp_path)
    assert ratchet.main(
        ["--measured", "95.0", "--thresholds", str(target)]) == 0
    doc = thresholds.load(target)
    assert doc["coverage"]["python"]["measured"] == Decimal("95.0")
    assert doc["coverage"]["python"]["floor"] == Decimal("93.5")
    out = capsys.readouterr().out
    assert "raised" in out
    assert "91.1 -> 93.5" in out


def test_main_raise_javascript_rewrites_the_file(tmp_path, capsys):
    thresholds = _thresholds()
    ratchet = _ratchet()
    target = _written(tmp_path)
    assert ratchet.main(
        ["--language", "javascript", "--measured", "52.0",
         "--thresholds", str(target)]) == 0
    doc = thresholds.load(target)
    assert doc["coverage"]["javascript"]["measured"] == Decimal("52.0")
    assert doc["coverage"]["javascript"]["floor"] == Decimal("50.5")
    # The python calibration is untouched by a javascript raise.
    assert doc["coverage"]["python"]["measured"] == Decimal("92.6")
    assert doc["coverage"]["python"]["floor"] == Decimal("91.1")
    out = capsys.readouterr().out
    assert "raised" in out
    assert "48.5 -> 50.5" in out


def test_main_invalid_measurement_fails(tmp_path, capsys):
    ratchet = _ratchet()
    target = _written(tmp_path)
    assert ratchet.main(
        ["--measured", "abc", "--thresholds", str(target)]) == 1
    assert capsys.readouterr().err


def test_main_unreadable_thresholds_fails(tmp_path, capsys):
    ratchet = _ratchet()
    missing = tmp_path / "absent.json"
    assert ratchet.main(
        ["--measured", "92.6", "--thresholds", str(missing)]) == 1


# --- the reparse CPU ratchet -------------------------------------------------
#
# The coverage ratchet only ever moves UP: more measured coverage is a
# better number. A reparse phase's share of the pass is a cost, so this
# ratchet mirrors every rule with the direction inverted — it only ever
# moves DOWN, one phase at a time, and a slower or within-hysteresis
# reading tightens nothing. The yardsticks are the same two constants,
# in the bench's own unit.

def _reparse_ratchet():
    return _load("reparse_ratchet")


def _readings(value="10.0", **overrides):
    """One measured value per phase and metric, with overrides by
    ``phase.metric``."""
    thresholds = _thresholds()
    readings = {
        phase: {metric: Decimal(value)
                for metric in thresholds.REPARSE_METRICS}
        for phase in thresholds.REPARSE_PHASES
    }
    for label, measured in overrides.items():
        phase, metric = label.split(".")
        readings[phase][metric] = Decimal(measured)
    return readings


def test_no_tighten_within_hysteresis():
    ratchet = _reparse_ratchet()
    # recorded 10.0 - hysteresis 1.5 = 8.5, not under it
    assert ratchet.update(_document(), _readings("8.5")) is None


def test_tighten_beyond_hysteresis():
    ratchet = _reparse_ratchet()
    updated = ratchet.update(_document(), _readings(**{"sniff.share": "8.4"}))
    assert updated is not None
    record = updated["reparse"]["sniff"]["share"]
    assert record["measured"] == Decimal("8.4")
    assert record["floor"] == Decimal("9.9")
    # Nothing outside the tightened phase moved.
    assert updated["schema_version"] == 1
    assert updated["coverage"] == _document()["coverage"]
    assert updated["module_size_baseline"] == {}
    assert updated["reparse"]["parse_body"]["share"] == {
        "measured": Decimal("10.0"), "floor": Decimal("11.5")}
    assert updated["reparse"]["sniff"]["bytecodes"] == {
        "measured": Decimal("10.0"), "floor": Decimal("11.5")}


def test_slower_measurement_never_raises_the_budget():
    ratchet = _reparse_ratchet()
    assert ratchet.update(_document(), _readings("10.1")) is None
    assert ratchet.update(_document(), _readings("90.0")) is None


def test_each_phase_and_metric_tightens_on_its_own_measurement():
    # Phases are independent ceilings, and so are the two metrics of one
    # phase: a cheaper share says nothing about the count, and the
    # ratchet must not carry either with the other.
    ratchet = _reparse_ratchet()
    updated = ratchet.update(_document(), _readings(**{
        "residual.bytecodes": "2.0"}))
    assert updated is not None
    assert updated["reparse"]["residual"]["bytecodes"] == {
        "measured": Decimal("2.0"), "floor": Decimal("3.5")}
    assert updated["reparse"]["residual"]["share"] == {
        "measured": Decimal("10.0"), "floor": Decimal("11.5")}
    assert updated["reparse"]["sniff"]["bytecodes"] == {
        "measured": Decimal("10.0"), "floor": Decimal("11.5")}


def test_a_uniform_slowdown_tightens_the_counts_and_not_the_shares():
    # The case a share provably cannot see: every phase gets more
    # expensive together. The counts move and the shares do not.
    ratchet = _reparse_ratchet()
    readings = _readings("10.0")
    for phase in readings:
        readings[phase]["bytecodes"] = Decimal("20.0")
    assert ratchet.update(_document(), readings) is None
    readings = {phase: dict(metrics) for phase, metrics in readings.items()}
    for phase in readings:
        readings[phase]["bytecodes"] = Decimal("4.0")
    updated = ratchet.update(_document(), readings)
    assert updated is not None
    assert updated["reparse"]["parse_body"]["bytecodes"] == {
        "measured": Decimal("4.0"), "floor": Decimal("5.5")}
    assert updated["reparse"]["parse_body"]["share"] == {
        "measured": Decimal("10.0"), "floor": Decimal("11.5")}


def test_measurement_missing_a_phase_refused():
    ratchet = _reparse_ratchet()
    with pytest.raises(ValueError, match="carries no parse_body phase"):
        ratchet.update(_document(), {"sniff": {"share": Decimal("10.0"),
                                               "bytecodes": Decimal("10.0")}})


def test_reparse_measured_with_two_decimals_rejected():
    ratchet = _reparse_ratchet()
    with pytest.raises(ValueError, match="exactly one decimal place"):
        ratchet.update(_document(), _readings(**{"sniff.share": "8.45"}))


def test_floor_for_is_measured_plus_the_gap():
    ratchet = _reparse_ratchet()
    assert ratchet.floor_for(Decimal("8.4"), "share") == Decimal("9.9")
    assert ratchet.floor_for(Decimal("26.2"), "bytecodes") == Decimal("27.7")


def _measurement_file(tmp_path, readings):
    thresholds = _thresholds()
    payload = {
        "unit": thresholds.REPARSE_UNIT,
        "count_unit": thresholds.REPARSE_COUNT_UNIT,
        "files": 5, "passes": 20000, "records": 6, "cpu_s": 8.0,
        "phases": {
            phase: {"cpu_s": 1.0, "share": str(metrics["share"]),
                    "bytecode_hundreds_per_file": str(
                        metrics["bytecodes"])}
            for phase, metrics in readings.items()},
        "share_sum": "100.0",
        "counts": {"available": True, "reason": "", "total_bytecodes": 1,
                   "overhead_per_call": 1},
        "perf_instructions_per_file": 287000,
        "perf_note": "synthetic",
    }
    target = tmp_path / "reparse.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


def test_reparse_main_tightens_from_a_written_measurement(tmp_path, capsys):
    thresholds = _thresholds()
    ratchet = _reparse_ratchet()
    target = _written(tmp_path)
    measurement = _measurement_file(
        tmp_path, _readings(**{"parse_body.share": "4.0"}))
    assert ratchet.main([
        "--measured-file", str(measurement),
        "--thresholds", str(target)]) == 0
    doc = thresholds.load(target)
    assert doc["reparse"]["parse_body"]["share"] == {
        "measured": Decimal("4.0"), "floor": Decimal("5.5")}
    assert doc["reparse"]["sniff"]["share"] == {
        "measured": Decimal("10.0"), "floor": Decimal("11.5")}
    out = capsys.readouterr().out
    assert "tightened the parse_body share budget" in out
    assert "11.5 -> 5.5" in out
    # The coverage calibrations are untouched by a reparse tighten.
    assert doc["coverage"]["python"]["measured"] == Decimal("92.6")


def test_reparse_main_no_tighten_leaves_file_untouched(tmp_path, capsys):
    ratchet = _reparse_ratchet()
    target = _written(tmp_path)
    before = target.read_text(encoding="utf-8")
    measurement = _measurement_file(tmp_path, _readings("10.0"))
    assert ratchet.main([
        "--measured-file", str(measurement),
        "--thresholds", str(target)]) == 0
    assert target.read_text(encoding="utf-8") == before
    assert "nothing to tighten" in capsys.readouterr().out


def test_reparse_main_unreadable_measurement_fails(tmp_path, capsys):
    ratchet = _reparse_ratchet()
    target = _written(tmp_path)
    assert ratchet.main([
        "--measured-file", str(tmp_path / "absent.json"),
        "--thresholds", str(target)]) == 1
    assert capsys.readouterr().err


def test_reparse_main_unreadable_thresholds_fails(tmp_path, capsys):
    ratchet = _reparse_ratchet()
    measurement = _measurement_file(tmp_path, _readings("4.0"))
    assert ratchet.main([
        "--measured-file", str(measurement),
        "--thresholds", str(tmp_path / "absent.json")]) == 1
    assert capsys.readouterr().err
