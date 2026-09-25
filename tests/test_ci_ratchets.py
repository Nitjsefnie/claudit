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


def _document(measured="92.6", floor="91.1"):
    return {
        "schema_version": 1,
        "coverage": {
            "python": {
                "measured": Decimal(measured),
                "floor": Decimal(floor),
            },
        },
        "module_size_baseline": {},
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


def test_measured_below_recorded_never_lowers():
    ratchet = _ratchet()
    doc = _document()
    assert ratchet.update(doc, Decimal("50.0")) is None


def test_measured_with_two_decimals_rejected():
    ratchet = _ratchet()
    with pytest.raises(ValueError, match="at most one decimal place"):
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
