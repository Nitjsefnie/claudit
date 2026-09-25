"""Tests for the CI thresholds document loader.

The loader is the one place both ratchets read their numbers from, so a
document it accepts is a document CI acts on. The cases below pin the
document shape (schema_version, one-decimal coverage values, floor below
measured and exactly the calibration gap below it, positive-integer
baseline entries, unknown keys refused) and the canonical byte layout
that ``write()`` publishes.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
THRESHOLDS_PATH = REPO_ROOT / ".github" / "ci-thresholds.json"


def _load():
    """Import scripts/ci/thresholds.py by path.

    scripts/ci is not a package and deliberately has no __init__.py — it
    holds standalone CI entry points, not an importable library.
    """
    path = REPO_ROOT / "scripts" / "ci" / "thresholds.py"
    spec = importlib.util.spec_from_file_location("thresholds", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["thresholds"] = module
    spec.loader.exec_module(module)
    return module


thresholds = _load()

GAP = Decimal("1.5")


def _document(measured="92.6", floor="91.1", baseline=None):
    return {
        "schema_version": 1,
        "coverage": {
            "python": {
                "measured": Decimal(measured),
                "floor": Decimal(floor),
            },
        },
        "module_size_baseline": baseline or {},
    }


def _written_document(tmp_path, **kwargs):
    target = tmp_path / "ci-thresholds.json"
    payload = json.loads(json.dumps(_document(**kwargs), default=float))
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


def test_committed_document_loads():
    doc = thresholds.load(THRESHOLDS_PATH)
    assert doc["schema_version"] == 1
    record = doc["coverage"]["python"]
    assert isinstance(record["measured"], Decimal)
    assert isinstance(record["floor"], Decimal)
    assert record["floor"] == record["measured"] - GAP


def test_normalised_document_keeps_exact_values():
    doc = thresholds.normalise(_document(measured="92.6", floor="91.1"))
    record = doc["coverage"]["python"]
    assert record["measured"] == Decimal("92.6")
    assert record["floor"] == Decimal("91.1")


def test_write_publishes_canonical_bytes(tmp_path):
    target = tmp_path / "ci-thresholds.json"
    doc = _document(baseline={"backend/api.py": 984})
    thresholds.write(target, doc)
    text = target.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert text == json.dumps(
        json.loads(text), indent=2, sort_keys=True) + "\n"
    assert thresholds.load(target) == thresholds.normalise(doc)


def test_unknown_top_level_key_refused(tmp_path):
    target = _written_document(tmp_path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["surprise"] = {}
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown field: surprise"):
        thresholds.load(target)


def test_unknown_coverage_language_refused(tmp_path):
    target = _written_document(tmp_path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["coverage"]["javascript"] = {"measured": 50.0, "floor": 48.5}
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown coverage language"):
        thresholds.load(target)


def test_missing_field_refused(tmp_path):
    target = _written_document(tmp_path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    del payload["coverage"]["python"]["floor"]
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="missing"):
        thresholds.load(target)


def test_floor_at_or_above_measured_refused(tmp_path):
    target = _written_document(tmp_path, measured="90.0", floor="90.0")
    with pytest.raises(ValueError, match="floor must be below measured"):
        thresholds.load(target)


def test_wrong_calibration_gap_refused(tmp_path):
    target = _written_document(tmp_path, measured="93.0", floor="90.0")
    with pytest.raises(ValueError, match="gap must be 1.5"):
        thresholds.load(target)


@pytest.mark.parametrize("value", ["92.55", "92.555"])
def test_more_than_one_decimal_place_refused(tmp_path, value):
    target = _written_document(tmp_path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["coverage"]["python"]["measured"] = json.loads(value)
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="exactly one decimal place"):
        thresholds.load(target)


def test_coverage_number_without_decimal_place_refused(tmp_path):
    # 92 is refused: the canonical spelling of a coverage number carries
    # exactly one decimal place (92.0) — what the ratchet writes and
    # what coverage --precision=1 measures.
    target = _written_document(tmp_path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["coverage"]["python"]["measured"] = 92
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="exactly one decimal place"):
        thresholds.load(target)


def test_coverage_number_with_trailing_zero_place_accepted(tmp_path):
    target = _written_document(tmp_path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["coverage"]["python"]["measured"] = 92.0
    payload["coverage"]["python"]["floor"] = 90.5
    target.write_text(json.dumps(payload), encoding="utf-8")
    doc = thresholds.load(target)
    assert doc["coverage"]["python"]["measured"] == Decimal("92.0")


def test_non_finite_number_refused(tmp_path):
    target = tmp_path / "ci-thresholds.json"
    target.write_text(
        '{"schema_version": 1, "coverage": {"python": {"measured": NaN,'
        ' "floor": 91.1}}, "module_size_baseline": {}}',
        encoding="utf-8")
    with pytest.raises(ValueError, match="non-finite"):
        thresholds.load(target)


def test_duplicate_key_refused(tmp_path):
    target = tmp_path / "ci-thresholds.json"
    target.write_text(
        '{"schema_version": 1, "coverage": {"python": {"measured": 92.6,'
        ' "floor": 91.1}}, "module_size_baseline": {},'
        ' "module_size_baseline": {}}',
        encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate JSON key"):
        thresholds.load(target)


def test_wrong_schema_version_refused(tmp_path):
    target = _written_document(tmp_path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["schema_version"] = 2
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported schema_version"):
        thresholds.load(target)


@pytest.mark.parametrize("value", ["0", "-5", "1.5"])
def test_baseline_value_must_be_positive_int(tmp_path, value):
    target = _written_document(tmp_path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["module_size_baseline"] = {"backend/api.py": json.loads(value)}
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="positive integer"):
        thresholds.load(target)


@pytest.mark.parametrize("path", ["../evil.py", "/abs.py", "a//b.py",
                                  "C:\\evil.py"])
def test_unsafe_baseline_path_refused(tmp_path, path):
    target = _written_document(tmp_path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["module_size_baseline"] = {path: 100}
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="unsafe module path"):
        thresholds.load(target)


def test_coverage_value_bounds():
    assert thresholds.coverage_value(
        Decimal("100.0"), "m") == Decimal("100.0")
    assert thresholds.coverage_value(Decimal("0.0"), "m") == Decimal("0.0")
    with pytest.raises(ValueError, match="between 0.0 and 100.0"):
        thresholds.coverage_value(Decimal("100.1"), "m")
    with pytest.raises(ValueError, match="between 0.0 and 100.0"):
        thresholds.coverage_value(Decimal("-0.1"), "m")
    with pytest.raises(ValueError, match="exactly one decimal place"):
        thresholds.coverage_value(Decimal("92"), "m")


def test_committed_document_bytes_are_canonical(tmp_path):
    # The COMMITTED bytes must be byte-identical to what write()
    # publishes for the same document — no hand-edited formatting
    # drift. The bytes come from git, not the working-tree file: a
    # Windows autocrlf checkout delivers CRLF on disk, and the pin has
    # to hold on every platform's checkout.
    doc = thresholds.load(THRESHOLDS_PATH)
    target = tmp_path / "canonical.json"
    thresholds.write(target, doc)
    committed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "cat-file", "blob",
         f"HEAD:{THRESHOLDS_PATH.relative_to(REPO_ROOT).as_posix()}"],
        capture_output=True, check=True).stdout
    assert committed == target.read_bytes()


def test_coverage_floor_cli_prints_floor():
    # The printed floor is whatever the committed document records — the
    # seed moves; the CLI's contract does not.
    recorded = thresholds.load(THRESHOLDS_PATH)["coverage"]["python"]
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "ci" / "thresholds.py"),
         "--coverage-floor", "python", "--thresholds", str(THRESHOLDS_PATH)],
        capture_output=True, text=True, check=True)
    assert result.stdout.strip() == f'{recorded["floor"]:.1f}'


def test_coverage_measured_cli_prints_measured():
    recorded = thresholds.load(THRESHOLDS_PATH)["coverage"]["python"]
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "ci" / "thresholds.py"),
         "--coverage-measured", "python", "--thresholds",
         str(THRESHOLDS_PATH)],
        capture_output=True, text=True, check=True)
    assert result.stdout.strip() == f'{recorded["measured"]:.1f}'


def test_check_cli_accepts_committed_document():
    # No check=True: the return code is itself the assertion subject.
    result = subprocess.run(  # pylint: disable=subprocess-run-check
        [sys.executable, str(REPO_ROOT / "scripts" / "ci" / "thresholds.py"),
         "--check", "--thresholds", str(THRESHOLDS_PATH)],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "thresholds valid" in result.stdout


def test_check_cli_rejects_broken_document(tmp_path):
    target = _written_document(tmp_path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["schema_version"] = 7
    target.write_text(json.dumps(payload), encoding="utf-8")
    # No check=True: a nonzero exit is the expected outcome here.
    result = subprocess.run(  # pylint: disable=subprocess-run-check
        [sys.executable, str(REPO_ROOT / "scripts" / "ci" / "thresholds.py"),
         "--check", "--thresholds", str(target)],
        capture_output=True, text=True)
    assert result.returncode == 1
    assert "schema_version" in result.stderr
