"""Tests for the CI thresholds document loader.

The loader is the one place both ratchets read their numbers from, so a
document it accepts is a document CI acts on. The cases below pin the
document shape (schema_version, one-decimal coverage values, floor below
measured and exactly the calibration gap below it, positive-integer
baseline entries, unknown keys refused) and the canonical byte layout
that ``write()`` publishes.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from tests import git_meta

REPO_ROOT = Path(__file__).resolve().parents[1]
THRESHOLDS_PATH = REPO_ROOT / ".github" / "ci-thresholds.json"


def _load(name="thresholds"):
    """Import a scripts/ci module by path.

    scripts/ci is not a package and deliberately has no __init__.py — it
    holds standalone CI entry points, not an importable library.
    """
    path = REPO_ROOT / "scripts" / "ci" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


reseed = _load("reseed")
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


def _document(measured="92.6", floor="91.1", baseline=None, reparse=None,
              suite=None):
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
        "reparse": _reparse() if reparse is None else reparse,
        "module_size_baseline": baseline or {},
        "pylint_suppression_baseline": {},
        "suite_cost": copy.deepcopy(SUITE_COST if suite is None else suite),
    }


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
    js_record = doc["coverage"]["javascript"]
    assert isinstance(js_record["measured"], Decimal)
    assert isinstance(js_record["floor"], Decimal)
    assert js_record["floor"] == js_record["measured"] - GAP


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
    doc = thresholds.normalise(_document(measured="92.6", floor="91.1"), False)
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
    assert thresholds.load(target) == thresholds.normalise(doc, False)


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
    payload["coverage"]["ruby"] = {"measured": 50.0, "floor": 48.5}
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown coverage language"):
        thresholds.load(target)


def test_missing_javascript_coverage_language_refused(tmp_path):
    target = _written_document(tmp_path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    del payload["coverage"]["javascript"]
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError,
                       match="missing coverage language: javascript"):
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
    assert subprocess_committed_bytes() == target.read_bytes()


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


def test_missing_suppression_member_refused(tmp_path):
    # The suppression baseline is a required member: a document that
    # omits it (or a hand-deleted member) is refused outright.
    target = _written_document(tmp_path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    del payload["pylint_suppression_baseline"]
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="missing field"):
        thresholds.load(target, False)


def test_suppression_member_entries_validated_like_size_entries(tmp_path):
    target = _written_document(tmp_path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["pylint_suppression_baseline"] = {"backend/api.py": 0}
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="positive integer"):
        thresholds.load(target)


def test_committed_suppression_member_loads():
    # The committed document carries the suppression baseline, and every
    # seed is a positive count a production file really has.
    doc = thresholds.load(THRESHOLDS_PATH)
    baseline = doc["pylint_suppression_baseline"]
    assert baseline, "the suppression baseline seeded empty"
    for path, count in baseline.items():
        assert path.startswith(("backend/", "scripts/"))
        assert not path.startswith("tests/")
        assert count > 0


def test_committed_suite_cost_family_matches_the_committed_bytes():
    # The committed document loads, and its suite_cost family — the one
    # property every consumer relies on — carries exactly the bench's
    # phases with every ceiling the calibration gap above its measured
    # value (a cost's gap sits above).
    #
    # The pin is on the DOCUMENT, not on the commit: the sanctioned
    # re-seed (issue #502) makes an absent family admissible for one
    # commit, and a pin that skipped there would run on no commit at
    # all. So this asserts the family when the document carries one and
    # says plainly when it does not — which is itself the shape the
    # delete commit has, so the delete commit is covered too.
    doc = thresholds.load(THRESHOLDS_PATH)
    recorded = thresholds.suite_cost(doc)
    present = b'"suite_cost"' in subprocess_committed_bytes()
    assert (recorded != {}) is present, (
        "the reader and the committed bytes disagree about the family")
    if not present:
        assert reseed.in_flight(REPO_ROOT), (
            "the family is absent on a tree that declares no re-seed: "
            "the loader tolerated a document the marker does not authorise")
        return
    assert tuple(sorted(recorded)) == tuple(
        sorted(thresholds.SUITE_COST_PHASES))
    for phase in thresholds.SUITE_COST_PHASES:
        measured, floor = recorded[phase]["measured"], recorded[phase]["floor"]
        assert isinstance(measured, Decimal)
        assert floor == measured + thresholds.CALIBRATION_GAP


def test_suite_cost_reader_returns_the_committed_phases():
    doc = thresholds.normalise(_document(), False)
    phases = thresholds.suite_cost(doc)
    assert phases == SUITE_COST


def test_a_marker_admits_an_absent_family_without_forbidding_a_present_one():
    # What the marker actually promises (issue #502): a family-ABSENT
    # document is admissible. It does NOT promise the family is absent,
    # and it must not forbid a document that carries one — the seed
    # commit restores the family, and a copy-pasted marker must not make
    # that state unreadable. The narrow claim is the loadable one; the
    # over-claim ("a marked commit records no budget") is what redded
    # the seed commit in review.
    doc = thresholds.normalise(_document(), False)
    assert thresholds.suite_cost(doc, True) == thresholds.suite_cost(doc, False)
    assert thresholds.normalise(doc, True) == doc


def test_missing_suite_cost_member_refused(tmp_path):
    # A document omitting the member is refused outright, like the
    # suppression baseline: every consumer can read the family unguarded.
    # The refusal is asked for explicitly, because load()'s default
    # answers the TREE (scripts/ci/reseed.py) and this tree's commit may
    # declare the sanctioned re-seed that makes the absence legal.
    target = _written_document(tmp_path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    del payload["suite_cost"]
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="missing field"):
        thresholds.load(target, False)


@pytest.mark.parametrize("phase", thresholds.SUITE_COST_PHASES)
def test_missing_suite_cost_phase_refused(tmp_path, phase):
    target = _written_document(tmp_path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    del payload["suite_cost"][phase]
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="missing suite cost phase"):
        thresholds.load(target)


def test_unknown_suite_cost_phase_refused(tmp_path):
    target = _written_document(tmp_path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["suite_cost"]["warmup"] = {"measured": 1.0, "floor": 2.5}
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown suite cost phase"):
        thresholds.load(target)


@pytest.mark.parametrize(
    "mutation, match",
    [
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
    ],
)
def test_suite_cost_entry_validator_rejects(tmp_path, mutation, match):
    target = _written_document(tmp_path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    mutation(payload["suite_cost"]["collection"])
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        thresholds.load(target)


@pytest.mark.parametrize(
    "measured, floor, match",
    [
        # floor == measured: no gap, a ceiling that bars the measurement
        # that recorded it.
        (12.5, 12.5, "floor must be above measured"),
        # floor BELOW measured: the gap written the coverage way round.
        (12.5, 11.0, "floor must be above measured"),
        # A gap that is not the shared yardstick.
        (12.5, 14.5, "gap must be 1.5"),
        (12.5, 13.9, "gap must be 1.5"),
    ],
)
def test_suite_cost_gap_shape_refused(tmp_path, measured, floor, match):
    target = _written_document(tmp_path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["suite_cost"]["collection"] = {
        "measured": measured, "floor": floor}
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        thresholds.load(target)


def test_suite_cost_values_survive_the_canonical_round_trip(tmp_path):
    # write() publishes canonical bytes and the loader reads the same
    # numbers back: the recorded value is the value measured.
    target = tmp_path / "ci-thresholds.json"
    doc = _document()
    thresholds.write(target, doc)
    assert thresholds.load(target)["suite_cost"] == SUITE_COST


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
