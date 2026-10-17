"""Tests for the CI thresholds document loader.

The loader is the one place both ratchets read their numbers from, so a
document it accepts is a document CI acts on. The cases below pin the
document shape (schema_version, one-decimal coverage values, floor below
measured and exactly the calibration gap below it, positive-integer
baseline entries, unknown keys refused) and the canonical byte layout
that ``write()`` publishes.
"""
from __future__ import annotations

import importlib
import json
import pickle
import subprocess
import sys
from decimal import Decimal
from functools import cache
from pathlib import Path

import pytest

from tests import git_meta

REPO_ROOT = Path(__file__).resolve().parents[1]
THRESHOLDS_PATH = REPO_ROOT / ".github" / "ci-thresholds.json"


def _load(name="thresholds"):
    """Import each CI module once, sharing its cached decode snapshots."""
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "ci"))
    return importlib.import_module(name)


thresholds = _load()

GAP = Decimal("1.5")

# Every suite phase's ceiling sits exactly the calibration gap ABOVE its
# recorded measured value: a suite phase's number is a cost ceiling, not a
# quality floor, so the gap sits above (mirrored against coverage, whose
# floor sits below).
SUITE_COST = {
    "collection": {"measured": Decimal("12.5"), "floor": Decimal("14.0")},
    "run": {"measured": Decimal("304.9"), "floor": Decimal("306.4")},
    "residual": {"measured": Decimal("6.3"), "floor": Decimal("7.8")},
}


def _reparse(shares=None):
    """A synthetic reparse family: both metrics for every bench phase."""
    shares = shares or {}
    return {
        phase: {
            metric: {
                "measured": Decimal(shares.get(f"{phase}.{metric}", "10.0")),
                "floor": Decimal(shares.get(f"{phase}.{metric}", "10.0")) + GAP,
            }
            for metric in thresholds.REPARSE_METRICS
        }
        for phase in thresholds.REPARSE_PHASES
    }


@cache
def _document_bytes():
    """An immutable synthetic seed; per-scenario mutations stay local."""
    return pickle.dumps({
        "schema_version": 1,
        "coverage": {
            "python": {"measured": Decimal("92.6"), "floor": Decimal("91.1")},
            "javascript": {"measured": Decimal("50.0"), "floor": Decimal("48.5")},
        },
        "reparse": _reparse(),
        "module_size_baseline": {},
        "comment_share_baseline": {},
        "pylint_suppression_baseline": {},
        "suite_cost": SUITE_COST,
    }, protocol=5)


def _document(measured="92.6", floor="91.1", baseline=None, reparse=None,
              suite=None):
    doc = pickle.loads(_document_bytes())
    doc["coverage"]["python"] = {"measured": Decimal(measured), "floor": Decimal(floor)}
    doc["module_size_baseline"] = baseline or {}
    if reparse is not None:
        doc["reparse"] = reparse
    if suite is not None:
        doc["suite_cost"] = pickle.loads(pickle.dumps(suite, protocol=5))
    return doc


@pytest.fixture(scope="module", name="committed_thresholds_document")
def _committed_thresholds_document():
    """Read the real document once; these consumers only inspect it."""
    return thresholds.load(THRESHOLDS_PATH)


@cache
def subprocess_committed_bytes():
    """The COMMITTED document's bytes, from git rather than the disk.

    The bytes come from git, not the working-tree file: a Windows
    autocrlf checkout delivers CRLF on disk, and a pin has to hold on
    every platform's checkout.
    """
    git_meta.require_own_git_metadata(REPO_ROOT)
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), "cat-file", "blob",
         f"HEAD:{THRESHOLDS_PATH.relative_to(REPO_ROOT).as_posix()}"],
        capture_output=True, check=True).stdout


def _written_document(tmp_path, **kwargs):
    target = tmp_path / "ci-thresholds.json"
    target.write_text(json.dumps(_document(**kwargs), default=float), encoding="utf-8")
    return target


def _thresholds_path(tmp_path, state):
    if state == "committed":
        return THRESHOLDS_PATH
    data = _document()
    data["pylint_suppression_baseline"] = {"scripts/ci/example.py": 1}
    if state in ("no-python", "no-javascript"):
        del data["coverage"][state.removeprefix("no-")]
    elif state == "no-coverage":
        del data["coverage"]
    elif state == "no-suppression":
        del data["pylint_suppression_baseline"]
    else:
        assert state == "present"
    target = tmp_path / "thresholds.json"
    target.write_text(json.dumps(data, default=float), encoding="utf-8")
    return target


def test_committed_document_loads(tmp_path, committed_thresholds_document):
    for state in [
        "committed",
        "present",
        "no-python",
        "no-javascript",
        "no-coverage",
    ]:
        doc = (committed_thresholds_document if state == "committed" else
               thresholds.load(_thresholds_path(tmp_path, state)))
        assert doc["schema_version"] == 1
        for language in thresholds.COVERAGE_LANGUAGES:
            record = doc.get("coverage", {}).get(language)
            if record is None:
                assert thresholds.coverage(doc, language) == (None, None)
                continue
            assert isinstance(record["measured"], Decimal)
            assert isinstance(record["floor"], Decimal)
            assert record["floor"] == record["measured"] - GAP


def test_javascript_coverage_language_accepted(tmp_path):
    # A javascript record carries the same shape and validation as a
    # python one: exactly one decimal place, floor exactly the gap below.
    target = tmp_path / "ci-thresholds.json"
    doc = _document()
    doc["coverage"]["javascript"] = {
        "measured": Decimal("71.3"),
        "floor": Decimal("69.8"),
    }
    thresholds.write(target, doc)
    loaded = thresholds.load(target)
    assert loaded["coverage"]["javascript"]["measured"] == Decimal("71.3")
    assert loaded["coverage"]["javascript"]["floor"] == Decimal("69.8")


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
    assert thresholds.load(target)["suite_cost"] == SUITE_COST


def test_invalid_document_shape_refused(tmp_path):
    for mutate, match in [
        (lambda doc: doc.update(surprise={}), "unknown field: surprise"),
        (lambda doc: doc["coverage"].update(
                ruby={"measured": 50.0, "floor": 48.5}), "unknown coverage language"),
        (lambda doc: doc["coverage"]["python"].pop("floor"), "missing"),
        (lambda doc: doc.update(schema_version=2), "unsupported schema_version"),
        (lambda doc: doc.update(
                pylint_suppression_baseline={"backend/api.py": 0}), "positive integer"),
        (lambda doc: doc["suite_cost"].update(
                warmup={"measured": 1.0, "floor": 2.5}), "unknown suite cost phase"),
    ]:
        target = _written_document(tmp_path)
        payload = json.loads(target.read_text(encoding="utf-8"))
        mutate(payload)
        target.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ValueError, match=match):
            thresholds.load(target)


def test_floor_at_or_above_measured_refused(tmp_path):
    target = _written_document(tmp_path, measured="90.0", floor="90.0")
    with pytest.raises(ValueError, match="floor must be below measured"):
        thresholds.load(target)


def test_wrong_calibration_gap_refused(tmp_path):
    target = _written_document(tmp_path, measured="93.0", floor="90.0")
    with pytest.raises(ValueError, match="gap must be 1.5"):
        thresholds.load(target)


def test_more_than_one_decimal_place_refused(tmp_path):
    for value in ["92.55", "92.555"]:
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


def test_absent_coverage_languages_load(tmp_path):
    target = _written_document(tmp_path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    del payload["coverage"]["python"]
    target.write_text(json.dumps(payload), encoding="utf-8")

    loaded = thresholds.load(target)

    assert loaded["coverage"] == {
        "javascript": {"measured": Decimal("50.0"),
                       "floor": Decimal("48.5")}}


def test_both_coverage_languages_may_be_absent(tmp_path):
    target = _written_document(tmp_path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["coverage"] = {}
    target.write_text(json.dumps(payload), encoding="utf-8")

    assert thresholds.load(target)["coverage"] == {}


def test_absent_coverage_family_loads(tmp_path):
    target = _written_document(tmp_path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    del payload["coverage"]
    target.write_text(json.dumps(payload), encoding="utf-8")

    assert "coverage" not in thresholds.load(target)


def test_absent_suite_cost_and_reparse_families_load(tmp_path):
    target = _written_document(tmp_path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    del payload["suite_cost"]
    del payload["reparse"]
    target.write_text(json.dumps(payload), encoding="utf-8")

    loaded = thresholds.load(target)

    assert "suite_cost" not in loaded
    assert "reparse" not in loaded
    assert thresholds.suite_cost(loaded) == {}
    assert thresholds.reparse(loaded) == {}


def test_absent_baseline_member_loads(tmp_path):
    target = _written_document(tmp_path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    del payload["module_size_baseline"]
    target.write_text(json.dumps(payload), encoding="utf-8")

    assert "module_size_baseline" not in thresholds.load(target)
    assert thresholds.module_size_baseline(
        thresholds.load(target)) == {}


def test_absent_baseline_reader_returns_empty_mapping(tmp_path):
    for member in [
        "module_size_baseline",
        "comment_share_baseline",
    ]:
        target = _written_document(tmp_path)
        payload = json.loads(target.read_text(encoding="utf-8"))
        del payload[member]
        target.write_text(json.dumps(payload), encoding="utf-8")
        loaded = thresholds.load(target)

        reader = getattr(thresholds, member)
        assert reader(loaded) == {}


def test_optional_families_round_trip_without_reappearing(tmp_path):
    data = {"schema_version": 1}
    normalised = thresholds.normalise(data)
    target = tmp_path / "ci-thresholds.json"

    thresholds.write(target, normalised)
    loaded = thresholds.load(target)

    assert loaded == normalised == {"schema_version": 1}


def test_present_but_malformed_optional_data_is_refused(tmp_path):
    for mutate, absent_family, match in [
        (lambda doc: doc.update(suite_cost={}), "reparse", "missing suite cost phase"),
        (lambda doc: doc.update(reparse={}), "suite_cost", "missing reparse CPU phase"),
        (lambda doc: doc["coverage"]["python"].update(floor=90.0), "suite_cost", "gap must be 1.5"),
        (lambda doc: doc["coverage"].update(
                python={"measured": 92.6}), "reparse", "missing.*floor"),
    ]:
        target = _written_document(tmp_path)
        payload = json.loads(target.read_text(encoding="utf-8"))
        mutate(payload)
        del payload[absent_family]
        target.write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(ValueError, match=match):
            thresholds.load(target)


def test_coverage_reader_returns_none_for_an_absent_language():
    data = _document()
    del data["coverage"]["python"]

    assert thresholds.coverage(data, "python") == (None, None)


def test_coverage_floor_cli_is_empty_for_an_absent_language(tmp_path):
    target = _written_document(tmp_path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    del payload["coverage"]["python"]
    target.write_text(json.dumps(payload), encoding="utf-8")

    result = subprocess.run(  # pylint: disable=subprocess-run-check
        [sys.executable, str(REPO_ROOT / "scripts" / "ci" / "thresholds.py"),
         "--coverage-floor", "python", "--thresholds", str(target)],
        capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_baseline_value_must_be_positive_int(tmp_path):
    for value in ["0", "-5", "1.5"]:
        target = _written_document(tmp_path)
        payload = json.loads(target.read_text(encoding="utf-8"))
        payload["module_size_baseline"] = {"backend/api.py": json.loads(value)}
        target.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ValueError, match="positive integer"):
            thresholds.load(target)


def test_unsafe_baseline_path_refused(tmp_path):
    for path in ["../evil.py", "/abs.py", "a//b.py",
                 "C:\\evil.py"]:
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


def test_committed_document_bytes_are_canonical(tmp_path, committed_thresholds_document):
    # The COMMITTED bytes must be byte-identical to what write()
    # publishes for the same document — no hand-edited formatting
    # drift. The bytes come from git, not the working-tree file: a
    # Windows autocrlf checkout delivers CRLF on disk, and the pin has
    # to hold on every platform's checkout.
    doc = committed_thresholds_document
    target = tmp_path / "canonical.json"
    thresholds.write(target, doc)
    assert subprocess_committed_bytes() == target.read_bytes()


def test_coverage_floor_cli_prints_floor(tmp_path, capsys, committed_thresholds_document):
    for flag, field in [
        ("--coverage-floor", "floor"),
        ("--coverage-measured", "measured"),
    ]:
        for state in [
            "committed",
            "present",
            "no-python",
            "no-coverage",
        ]:
            # Committed readings may move or disappear; synthetic documents
            # exercise both CLI outcomes regardless of the current recording.
            path = _thresholds_path(tmp_path, state)
            data = committed_thresholds_document if state == "committed" else thresholds.load(path)
            recorded = data.get("coverage", {}).get("python")
            assert thresholds.main([flag, "python", "--thresholds", str(path)]) == 0
            captured = capsys.readouterr()
            if recorded is None:
                assert thresholds.coverage(data, "python") == (None, None)
                assert captured.out == ""
            else:
                assert captured.out == f'{recorded[field]:.1f}\n'


def test_check_cli_accepts_committed_document(capsys):
    assert thresholds.main(["--check", "--thresholds", str(THRESHOLDS_PATH)]) == 0
    captured = capsys.readouterr()
    assert "thresholds valid" in captured.out


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


def test_committed_suppression_member_loads(tmp_path, committed_thresholds_document):
    for state in [
        "committed",
        "present",
        "no-suppression",
    ]:
        # Recorded suppression entries name production paths and carry
        # positive counts; the whole family may also be absent.
        doc = (committed_thresholds_document if state == "committed" else
               thresholds.load(_thresholds_path(tmp_path, state)))
        baseline = doc.get("pylint_suppression_baseline", {})
        if "pylint_suppression_baseline" not in doc:
            assert baseline == {}
        for path, count in baseline.items():
            assert path.startswith(("backend/", "scripts/"))
            assert not path.startswith("tests/")
            assert count > 0


def test_committed_suite_cost_family_matches_the_committed_bytes(committed_thresholds_document):
    # The committed document loads, and its suite_cost family — the one
    # property every consumer relies on — carries exactly the bench's
    # phases with every ceiling the calibration gap above its measured
    # value (a cost's gap sits above).
    #
    # The pin is on the DOCUMENT, so it asserts the family's readings
    # when they are recorded and accepts the valid absent-family shape.
    doc = committed_thresholds_document
    recorded = thresholds.suite_cost(doc)
    present = b'"suite_cost"' in subprocess_committed_bytes()
    assert (recorded != {}) is present, (
        "the reader and the committed bytes disagree about the family")
    if not present:
        return
    assert tuple(sorted(recorded)) == tuple(
        sorted(thresholds.SUITE_COST_PHASES))
    for phase in thresholds.SUITE_COST_PHASES:
        measured, floor = recorded[phase]["measured"], recorded[phase]["floor"]
        assert isinstance(measured, Decimal)
        assert floor == measured + thresholds.CALIBRATION_GAP


def test_suite_cost_reader_returns_the_committed_phases():
    doc = thresholds.normalise(_document())
    phases = thresholds.suite_cost(doc)
    assert phases == SUITE_COST


def test_missing_suite_cost_phase_refused(tmp_path):
    for phase in thresholds.SUITE_COST_PHASES:
        target = _written_document(tmp_path)
        payload = json.loads(target.read_text(encoding="utf-8"))
        del payload["suite_cost"][phase]
        target.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ValueError, match="missing suite cost phase"):
            thresholds.load(target)


def test_suite_cost_entry_validator_rejects(tmp_path):
    for mutation, match in [
        # Vary ONE property per case against the validator's conditions;
        # the nested label names the phase and the field it rejects.
        (lambda record: record.pop("measured"), "missing suite cost"),
        (lambda record: record.pop("floor"), "missing suite cost"),
        (lambda record: record.update({"kind": "share"}),
         "unknown suite cost"),
        (lambda record: record.update({"measured": "4.2"}), "JSON number"),
        (lambda record: record.update({"measured": -0.1}), "non-negative"),
        (lambda record: record.update({"measured": 4}), "exactly one"),
        (lambda record: record.update({"measured": 4.25}), "exactly one"),
        (lambda record: record.update({"floor": 14}), "exactly one"),
    ]:
        target = _written_document(tmp_path)
        payload = json.loads(target.read_text(encoding="utf-8"))
        mutation(payload["suite_cost"]["collection"])
        target.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ValueError, match=match):
            thresholds.load(target)


def test_suite_cost_gap_shape_refused(tmp_path):
    for measured, floor, match in [
        # floor == measured: no gap, a ceiling that bars the measurement
        # that recorded it.
        (12.5, 12.5, "floor must be above measured"),
        # floor BELOW measured: the gap written the coverage way round.
        (12.5, 11.0, "floor must be above measured"),
        # A gap that is not the shared yardstick.
        (12.5, 14.5, "gap must be 1.5"),
        (12.5, 13.9, "gap must be 1.5"),
    ]:
        target = _written_document(tmp_path)
        payload = json.loads(target.read_text(encoding="utf-8"))
        payload["suite_cost"]["collection"] = {
            "measured": measured, "floor": floor}
        target.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ValueError, match=match):
            thresholds.load(target)


def test_instruction_value_bounds():
    assert thresholds.instruction_value(
        Decimal("0.0"), "m") == Decimal("0.0")
    assert thresholds.instruction_value(
        Decimal("304.9"), "m") == Decimal("304.9")
    with pytest.raises(ValueError, match="non-negative"):
        thresholds.instruction_value(Decimal("-0.1"), "m")
    with pytest.raises(ValueError, match="exactly one decimal place"):
        thresholds.instruction_value(Decimal("4"), "m")
    with pytest.raises(ValueError, match="exactly one decimal place"):
        thresholds.instruction_value(Decimal("4.25"), "m")
