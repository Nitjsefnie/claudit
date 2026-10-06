"""The reparse member's loader contract, pinned beside its bench.

Split out of ``test_ci_thresholds.py`` when the file sat on its size
ceiling (SV-CI-RATCHETS) and the reparse family's own re-seed window
(issue #698) needed the committed-document pins branched on presence:
the pins here are on the DOCUMENT, so each one holds on the delete's
tip as well as on a budget-carrying one.
"""
from __future__ import annotations

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


def _reparse(shares=None):
    """A synthetic reparse family: both metrics for every bench phase."""
    shares = shares or {}
    return {
        phase: {
            metric: {
                "measured": Decimal(
                    shares.get(f"{phase}.{metric}", "10.0")),
                "floor": Decimal(
                    shares.get(f"{phase}.{metric}", "10.0")) + GAP,
            }
            for metric in thresholds.REPARSE_METRICS
        }
        for phase in thresholds.REPARSE_PHASES
    }


def _document(reparse=None):
    data = {
        "schema_version": 1,
        "coverage": {
            "python": {"measured": Decimal("92.6"),
                       "floor": Decimal("91.1")},
            "javascript": {"measured": Decimal("50.0"),
                           "floor": Decimal("48.5")},
        },
        "module_size_baseline": {},
        "pylint_suppression_baseline": {},
        "suite_cost": {
            phase: {"measured": Decimal("10.0"),
                    "floor": Decimal("10.0") + GAP}
            for phase in thresholds.SUITE_COST_PHASES
        },
        "reparse": _reparse() if reparse is None else reparse,
    }
    return data


def _written_document(tmp_path, **kwargs):
    target = tmp_path / "ci-thresholds.json"
    payload = json.loads(json.dumps(_document(**kwargs), default=float))
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


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


def test_committed_document_carries_a_record_for_every_reparse_phase():
    # The reparse bench's calibration is a required member: a document
    # without one leaves the bench's gate step with nothing to check the
    # measurement against, and one missing a phase leaves that phase
    # ungated while the others still look complete.
    #
    # The pin is on the DOCUMENT, not on the commit: the family's own
    # sanctioned re-seed (issue #698) makes an absent family admissible
    # for the window, and a pin that demanded a record there would run
    # red on the delete's tip. Present, every phase carries both
    # instruments; absent, the tree must be declaring the window.
    doc = thresholds.load(THRESHOLDS_PATH)
    family = doc.get("reparse")
    if family is None:
        assert reseed.in_flight(REPO_ROOT, family=thresholds.REPARSE_FAMILY), (
            "the family is absent on a tree that declares no re-seed: "
            "the loader tolerated a document the marker does not authorise")
        return
    assert set(family) == set(thresholds.REPARSE_PHASES)
    for phase, metrics in family.items():
        assert set(metrics) == set(thresholds.REPARSE_METRICS), phase
        for metric, record in metrics.items():
            label = f'{phase}.{metric}'
            assert isinstance(record["measured"], Decimal), label
            assert isinstance(record["floor"], Decimal), label
            assert record["measured"] >= 0, label
            assert record["floor"] > record["measured"], label
            assert record["floor"] == record["measured"] + GAP, label


def test_reparse_reader_returns_every_phase_and_metric():
    # The reader and the committed bytes must agree about the family in
    # both states: the phases mid-window (issue #698), where the reader
    # returns {} and nothing further is pinned, and the full shape
    # everywhere else.
    doc = thresholds.load(THRESHOLDS_PATH)
    records = thresholds.reparse(doc)
    present = b'"reparse"' in subprocess_committed_bytes()
    assert (records != {}) is present, (
        "the reader and the committed bytes disagree about the family")
    if not present:
        return
    assert set(records) == set(thresholds.REPARSE_PHASES)
    for phase, metrics in records.items():
        assert set(metrics) == set(thresholds.REPARSE_METRICS), phase
        assert metrics == doc["reparse"][phase], phase


def test_missing_reparse_cpu_member_refused(tmp_path):
    # The refusal is asked for explicitly, because load()'s default
    # answers the TREE (scripts/ci/reseed.py) and inside the family's
    # own re-seed window (issue #698) that tree admits the absence —
    # the suite-cost member's rule exactly.
    target = _written_document(tmp_path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    del payload["reparse"]
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="missing field: reparse"):
        thresholds.load(target, False)


def test_missing_reparse_phase_refused(tmp_path):
    target = _written_document(tmp_path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    del payload["reparse"]["residual"]
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError,
                       match="missing reparse CPU phase: residual"):
        thresholds.load(target)


def test_unknown_reparse_phase_refused(tmp_path):
    target = _written_document(tmp_path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["reparse"]["persist"] = {"measured": 1.0, "floor": 2.5}
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown reparse CPU phase: persist"):
        thresholds.load(target)


def test_missing_reparse_phase_field_refused(tmp_path):
    target = _written_document(tmp_path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    del payload["reparse"]["sniff"]["share"]["floor"]
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="missing reparse CPU sniff.share: floor"):
        thresholds.load(target)


def test_unknown_reparse_phase_field_refused(tmp_path):
    target = _written_document(tmp_path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["reparse"]["sniff"]["unit"] = "percent"
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown reparse CPU sniff: unit"):
        thresholds.load(target)


@pytest.mark.parametrize("value", ["100.1", "-0.1"])
def test_reparse_share_must_be_a_percentage(tmp_path, value):
    target = _written_document(tmp_path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["reparse"]["sniff"]["share"] = {
        "measured": json.loads(value),
        "floor": json.loads(value) + 1.5,
    }
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="between 0.0 and 100.0"):
        thresholds.load(target)


def test_reparse_zero_share_is_a_real_reading(tmp_path):
    # A phase this corpus never reaches reads 0.0 (the sidecar step: no
    # meta.json sidecar is committed). That is a measurement, not an
    # absence, so the loader takes it.
    target = _written_document(tmp_path, reparse=_reparse(
        {"sidecar.share": "0.0", "sidecar.bytecodes": "0.0"}))
    doc = thresholds.load(target)
    assert doc["reparse"]["sidecar"]["share"]["measured"] == Decimal("0.0")
    assert doc["reparse"]["sidecar"]["bytecodes"]["measured"] == Decimal("0.0")


@pytest.mark.parametrize("value", ["6", "6.45"])
def test_reparse_share_needs_exactly_one_decimal_place(tmp_path, value):
    target = _written_document(tmp_path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["reparse"]["sniff"]["share"] = {
        "measured": json.loads(value),
        "floor": json.loads(value) + 1.5,
    }
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="exactly one decimal place"):
        thresholds.load(target)


def test_reparse_wrong_calibration_gap_refused(tmp_path):
    target = _written_document(tmp_path)
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["reparse"]["sniff"]["share"] = {"measured": 6.4, "floor": 8.4}
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="gap must be 1.5"):
        thresholds.load(target)


def test_reparse_floor_below_measured_refused(tmp_path):
    # The floor is a CEILING on a cost, so it sits above the measured
    # value: a record the other way round would gate every future run
    # below a number it cannot influence.
    target = _written_document(tmp_path, reparse=_reparse())
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["reparse"]["sniff"]["share"] = {"measured": 6.4, "floor": 5.9}
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError,
                       match=r"reparse\.sniff\.share\.floor must be above"):
        thresholds.load(target)


@pytest.mark.parametrize("metric", ["share", "bytecodes"])
def test_reparse_cli_prints_one_phase_metric(metric):
    phase = thresholds.REPARSE_PHASES[0]
    doc = thresholds.load(THRESHOLDS_PATH)
    family = doc.get("reparse")
    if family is None:
        assert reseed.in_flight(REPO_ROOT, family=thresholds.REPARSE_FAMILY), (
            "the family is absent on a tree that declares no re-seed: "
            "the loader tolerated a document the marker does not authorise")
        pytest.skip("the reparse family sits mid-re-seed (issue #698)")
    recorded = family[phase][metric]
    for flag, field in (("--reparse-floor", "floor"),
                        ("--reparse-measured", "measured")):
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "ci" / "thresholds.py"),
             flag, phase, metric, "--thresholds", str(THRESHOLDS_PATH)],
            capture_output=True, text=True, check=True)
        assert result.stdout.strip() == f'{recorded[field]:.1f}'


def test_reparse_cli_refuses_an_unknown_phase(tmp_path):
    # A written synthetic document, not the committed one: the pin is
    # the CLI's unknown-phase refusal, which must not depend on the
    # committed document carrying a budget (it does not, mid-window —
    # issue #698).
    target = _written_document(tmp_path)
    result = subprocess.run(  # pylint: disable=subprocess-run-check
        [sys.executable, str(REPO_ROOT / "scripts" / "ci" / "thresholds.py"),
         "--reparse-floor", "persist", "share", "--thresholds",
         str(target)],
        capture_output=True, text=True)
    assert result.returncode == 1
    assert "unknown reparse phase" in result.stderr
