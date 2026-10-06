"""Tests for the reparse family's seed writer (issue #703).

The landing half of the sanctioned re-seed: after the marked delete,
only a runner measurement through the loader's writer may restore the
family. The pins below hold the two directions apart — seeding works
only onto a family-ABSENT document and is refused over a recorded one
(the hand-raise's doorway), restores BOTH metrics per phase, refuses a
counts-less measurement rather than seeding an absence, and leaves the
committed bytes in the loader's canonical form.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from decimal import Decimal
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CI = REPO_ROOT / "scripts" / "ci"


def _load(name):
    """Import one scripts/ci module by path, as the CI entry points do."""
    if str(CI) not in sys.path:
        sys.path.insert(0, str(CI))
    spec = importlib.util.spec_from_file_location(name, CI / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# Load order is the import graph: thresholds imports thresholds_validate
# by name, and the ratchet reaches its sibling the same way.
thresholds = _load("thresholds")
reseed = _load("reseed")
reparse_ratchet = _load("reparse_ratchet")

GAP = Decimal("1.5")


def _document(reparse=True):
    """A valid document, with or without the reparse family."""
    data = {
        "schema_version": 1,
        "coverage": {
            "python": {"measured": Decimal("95.6"),
                       "floor": Decimal("94.1")},
            "javascript": {"measured": Decimal("90.4"),
                           "floor": Decimal("88.9")},
        },
        "module_size_baseline": {},
        "pylint_suppression_baseline": {},
        "suite_cost": {
            phase: {"measured": Decimal("10.0"),
                    "floor": Decimal("11.5")}
            for phase in thresholds.SUITE_COST_PHASES
        },
    }
    if reparse:
        data[thresholds.REPARSE_FAMILY] = {
            phase: {metric: {"measured": Decimal("10.0"),
                             "floor": Decimal("11.5")}
                    for metric in thresholds.REPARSE_METRICS}
            for phase in thresholds.REPARSE_PHASES
        }
    return data


def _write(tmp_path: Path, data, name="ci-thresholds.json") -> Path:
    path = tmp_path / name
    if thresholds.REPARSE_FAMILY in data:
        thresholds.write(path, thresholds.normalise(data, False), False)
    else:
        # The family-absent shape is writable only under the verdict the
        # delete's marker declares — the same door the seed's target
        # went out of.
        verdict = thresholds.validate.ReseedVerdict(False, True)
        thresholds.write(path, thresholds.normalise(data, verdict), verdict)
    return path


def _write_measurement(path: Path, counted=True) -> Path:
    """The bench --write shape: every phase, share and count spelled."""
    phases = {
        phase: {"share": "9.0", "cpu_s": 0.25,
                "bytecode_hundreds_per_file": ("1.0" if counted else None)}
        for phase in thresholds.REPARSE_PHASES
    }
    path.write_text(json.dumps({"phases": phases}), encoding="utf-8")
    return path


def test_the_seed_refuses_a_family_that_is_still_recorded():
    with pytest.raises(ValueError, match="already recorded"):
        reparse_ratchet.seed(_document(), {
            phase: {"share": "9.0", "bytecodes": "1.0"}
            for phase in thresholds.REPARSE_PHASES})


def test_the_refusal_names_the_own_marker():
    with pytest.raises(ValueError, match=re.escape(
            str(reseed.REPARSE_MARKER))):
        reparse_ratchet.seed(_document(), {
            phase: {"share": "9.0", "bytecodes": "1.0"}
            for phase in thresholds.REPARSE_PHASES})


def test_the_seed_writes_the_family_onto_an_emptied_document():
    data = _document(reparse=False)
    readings = {
        phase: {"share": "9.0", "bytecodes": "27.2"}
        for phase in thresholds.REPARSE_PHASES}
    seeded = reparse_ratchet.seed(data, readings)
    family = seeded[thresholds.REPARSE_FAMILY]
    assert family["parse_body"]["bytecodes"] == {
        "measured": Decimal("27.2"), "floor": Decimal("28.7")}
    # Strict, by fact: the seeded document needs no marker's tolerance.
    assert thresholds.normalise(seeded, False) == seeded


def test_the_seed_restores_both_metrics_per_phase():
    data = _document(reparse=False)
    readings = {
        phase: {"share": "6.7", "bytecodes": "1.6"}
        for phase in thresholds.REPARSE_PHASES}
    seeded = reparse_ratchet.seed(data, readings)
    for phase in thresholds.REPARSE_PHASES:
        assert set(seeded[thresholds.REPARSE_FAMILY][phase]) == set(
            thresholds.REPARSE_METRICS)
        for metric in thresholds.REPARSE_METRICS:
            record = seeded[thresholds.REPARSE_FAMILY][phase][metric]
            assert record["floor"] == record["measured"] + GAP


def test_a_counts_less_measurement_is_refused(tmp_path):
    measurement = _write_measurement(tmp_path / "m.json", counted=False)
    data, _fresh = reparse_ratchet.load_for_seed(
        _write(tmp_path, _document(reparse=False)))
    with pytest.raises(ValueError, match="not gateable or seedable"):
        reparse_ratchet.seed(
            data, reparse_ratchet._seed_readings(  # pylint: disable=protected-access
                measurement))


def test_the_seed_cli_writes_an_absent_family(tmp_path, capsys):
    document = _write(tmp_path, _document(reparse=False))
    measurement = _write_measurement(tmp_path / "m.json")
    before = document.read_text(encoding="utf-8")
    assert reparse_ratchet.main([
        "--seed", str(measurement),
        "--thresholds", str(document)]) == 0
    assert "seeded the parse_body budgets" in capsys.readouterr().out
    loaded = thresholds.load(document, thresholds.validate.ReseedVerdict(
        False, False))
    # The count the measurement carried is what landed, at the gap above.
    assert (loaded[thresholds.REPARSE_FAMILY]["parse_body"]["bytecodes"]
            == {"measured": Decimal("1.0"), "floor": Decimal("2.5")})
    assert document.read_text(encoding="utf-8") != before


def test_the_seed_cli_refuses_a_recorded_family(tmp_path, capsys):
    document = _write(tmp_path, _document())
    measurement = _write_measurement(tmp_path / "m.json")
    before = document.read_text(encoding="utf-8")
    assert reparse_ratchet.main([
        "--seed", str(measurement),
        "--thresholds", str(document)]) == 1
    assert "already recorded" in capsys.readouterr().err
    assert document.read_text(encoding="utf-8") == before


def test_the_modes_are_exclusive_and_one_is_required():
    with pytest.raises(SystemExit):
        reparse_ratchet.main(["--thresholds", "/dev/null"])
    with pytest.raises(SystemExit):
        reparse_ratchet.main([
            "--seed", "/dev/null", "--measured-file", "/dev/null"])
