"""Tests for the suite cost ratchet's data operations.

The bot's only suite-cost moves: a one-time seed through the loader's
writer when the member is absent (refusing to overwrite), and a tighten
that lowers both fields of a phase whose measurement beats the recorded
value by more than the hysteresis, never raising and never touching the
other phases. The synthetic bot path at the bottom seeds, tightens a
cheaper measurement, and proves the direction guard accepts the
downward move and rejects the upward one.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "ci"))

REPO_ROOT = Path(__file__).resolve().parents[1]
THRESHOLDS_PATH = REPO_ROOT / ".github" / "ci-thresholds.json"


def _load(name):
    """Import a scripts/ci module by path.

    scripts/ci is not a package and deliberately has no __init__.py — it
    holds standalone CI entry points, not an importable library. The
    directory itself goes on sys.path first, so the module's importlib
    import of its siblings resolves.
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
    return _load("suite_ratchet")


def _guard():
    return _load("thresholds_guard")


COUNTS = {
    "collection": Decimal("12.5"),
    "run": Decimal("304.9"),
    "residual": Decimal("6.3"),
}

MEASUREMENT = {
    "instrument": "instruction_count",
    "unit": "million_instructions",
    "hash_seed": "0",
    "tests": 255,
    "phases": {
        phase: {"million_instructions": str(count),
                "process_time_s": "1.000"}
        for phase, count in COUNTS.items()
    },
    "total_million_instructions": str(sum(COUNTS.values())),
}


def _document(suite=None):
    return {
        "schema_version": 1,
        "coverage": {
            "python": {"measured": 92.6, "floor": 91.1},
            "javascript": {"measured": 50.0, "floor": 48.5},
        },
        "module_size_baseline": {},
        "pylint_suppression_baseline": {},
        "suite_cost": copy.deepcopy(suite) if suite is not None else None,
        # The loader requires EVERY top-level family, so a synthetic
        # suite_cost document must also carry a valid reparse family. It
        # is inert here — nothing in this module reads it.
        "reparse": {
            phase: {
                metric: {"measured": 10.0, "floor": 11.5}
                for metric in _thresholds().REPARSE_METRICS
            }
            for phase in _thresholds().REPARSE_PHASES
        },
    }


def _seeded_document():
    doc = _document()
    del doc["suite_cost"]
    return doc


def _written(tmp_path, doc, name="ci-thresholds.json"):
    target = tmp_path / name
    if doc.get("suite_cost") is None:
        doc.pop("suite_cost", None)
        target.write_text(json.dumps(doc), encoding="utf-8")
    else:
        _thresholds().write(target, doc)
    return target


def _measurement_file(tmp_path, counts, name="m.json"):
    target = tmp_path / name
    payload = json.loads(json.dumps(MEASUREMENT))
    for phase in ("collection", "run", "residual"):
        payload["phases"][phase]["million_instructions"] = str(
            counts[phase])
    payload["total_million_instructions"] = str(sum(counts.values()))
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


def test_seed_writes_the_family_through_the_loader(tmp_path):
    thresholds = _thresholds()
    ratchet = _ratchet()
    target = _written(tmp_path, _seeded_document())
    assert ratchet.main(["--seed", str(_measurement_file(tmp_path, COUNTS)),
                         "--thresholds", str(target)]) == 0
    doc = thresholds.load(target)
    assert doc["suite_cost"] == {
        phase: {
            "measured": COUNTS[phase],
            "floor": COUNTS[phase] + Decimal("1.5"),
        } for phase in ("collection", "run", "residual")}


def test_seed_refuses_when_the_member_exists(tmp_path, capsys):
    ratchet = _ratchet()
    seeded = _document()
    seeded["suite_cost"] = {
        phase: {"measured": COUNTS[phase],
                "floor": COUNTS[phase] + Decimal("1.5")}
        for phase in ("collection", "run", "residual")}
    target = _written(tmp_path, seeded)
    before = target.read_text(encoding="utf-8")
    assert ratchet.main(["--seed", str(_measurement_file(tmp_path, COUNTS)),
                         "--thresholds", str(target)]) == 1
    assert target.read_text(encoding="utf-8") == before
    assert "already recorded" in capsys.readouterr().err


def test_an_empty_family_is_present_and_refuses_the_seed(tmp_path):
    # The decoy a falsy test admits: `"suite_cost": {}` is the family
    # PRESENT and malformed (the loader refuses its shape — see the
    # empty-family decoy pins in test_ci_reseed.py), and a seed that
    # read emptiness as the absent family would replace it without the
    # marked delete — the hand-raise's doorway (issue #705; PR #704
    # fixed the reparse writer's twin,
    # tests/test_reparse_seed.py's empty-family test).
    ratchet = _ratchet()
    data = _seeded_document()
    data["suite_cost"] = {}
    with pytest.raises(ValueError, match="already recorded"):
        ratchet.seed(data, COUNTS)
    # load_for_seed hands a PRESENT family to the loader, whose own
    # shape refusal fires before any seed could: a different message,
    # the same outcome — no replace without the marked delete.
    decoy = tmp_path / "decoy.json"
    decoy.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="missing suite cost"):
        ratchet.load_for_seed(decoy)


def test_seed_writes_canonical_bytes(tmp_path):
    # The seeded bytes are the loader writer's canonical bytes: the
    # round trip through normalise is lossless, so the committed
    # document always stays byte-identical to what write() publishes.
    thresholds = _thresholds()
    ratchet = _ratchet()
    target = _written(tmp_path, _seeded_document())
    assert ratchet.main(["--seed", str(_measurement_file(tmp_path, COUNTS)),
                         "--thresholds", str(target)]) == 0
    text = target.read_text(encoding="utf-8")
    assert text == json.dumps(
        json.loads(text), indent=2, sort_keys=True) + "\n"
    assert thresholds.load(target) == thresholds.normalise(
        thresholds.load(target), False)


def test_tighten_moves_both_fields_past_the_hysteresis(tmp_path):
    thresholds = _thresholds()
    ratchet = _ratchet()
    seeded = _document()
    seeded["suite_cost"] = {
        phase: {"measured": COUNTS[phase],
                "floor": COUNTS[phase] + Decimal("1.5")}
        for phase in ("collection", "run", "residual")}
    target = _written(tmp_path, seeded)
    cheaper = dict(COUNTS)
    # run: exactly the hysteresis cheaper -- no move. collection: more
    # than the hysteresis cheaper -- both fields move down. residual:
    # unchanged.
    cheaper["run"] = COUNTS["run"] - Decimal("1.5")
    cheaper["collection"] = COUNTS["collection"] - Decimal("1.6")
    assert ratchet.main(
        ["--tighten", str(_measurement_file(tmp_path, cheaper)),
         "--thresholds", str(target)]) == 0
    doc = thresholds.load(target)
    assert doc["suite_cost"]["run"] == seeded["suite_cost"]["run"]
    assert doc["suite_cost"]["residual"] == seeded["suite_cost"]["residual"]
    assert doc["suite_cost"]["collection"] == {
        "measured": COUNTS["collection"] - Decimal("1.6"),
        "floor": COUNTS["collection"] - Decimal("1.6") + Decimal("1.5"),
    }


def test_tighten_never_raises(tmp_path):
    ratchet = _ratchet()
    seeded = _document()
    seeded["suite_cost"] = {
        phase: {"measured": COUNTS[phase],
                "floor": COUNTS[phase] + Decimal("1.5")}
        for phase in ("collection", "run", "residual")}
    target = _written(tmp_path, seeded)
    dearer = {phase: COUNTS[phase] + Decimal("50.0")
              for phase in ("collection", "run", "residual")}
    before = target.read_text(encoding="utf-8")
    assert ratchet.main(
        ["--tighten", str(_measurement_file(tmp_path, dearer)),
         "--thresholds", str(target)]) == 0
    assert target.read_text(encoding="utf-8") == before


def test_tighten_refuses_a_counts_less_measurement(tmp_path):
    ratchet = _ratchet()
    target = _written(tmp_path, _seeded_document())
    payload = json.loads(json.dumps(MEASUREMENT))
    payload["instrument"] = "process_time"
    for record in payload["phases"].values():
        record["million_instructions"] = None
    path = tmp_path / "fallback.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert ratchet.main(["--tighten", str(path),
                         "--thresholds", str(target)]) == 1


def test_the_synthetic_bot_path_seed_then_tighten_then_guard(tmp_path):
    # The bot's whole suite-cost journey in one flow: seed against a
    # document the family predates, tighten with a cheaper run, and
    # hold the result to the direction guard -- downward free, upward
    # forbidden.
    thresholds = _thresholds()
    ratchet = _ratchet()
    guard = _guard()
    target = _written(tmp_path, _seeded_document())
    assert ratchet.main(["--seed", str(_measurement_file(tmp_path, COUNTS)),
                         "--thresholds", str(target)]) == 0
    seeded = thresholds.load(target)

    cheaper = {phase: COUNTS[phase] - Decimal("2.0")
               for phase in ("collection", "run", "residual")}
    assert ratchet.main(
        ["--tighten", str(_measurement_file(tmp_path, cheaper, "c.json")),
         "--thresholds", str(target)]) == 0
    tightened = thresholds.load(target)
    for phase in ("collection", "run", "residual"):
        assert tightened["suite_cost"][phase]["measured"] == cheaper[phase]

    # Guard: base -> tightened (a tighten) is clean; tightened -> base
    # (the reverse, an upward move) is forbidden.
    base_path = tmp_path / "base.json"
    thresholds.write(base_path, seeded)
    head_path = tmp_path / "head.json"
    thresholds.write(head_path, tightened)
    assert guard.main([
        "--base", str(base_path), "--head", str(head_path)]) == 0
    assert guard.main([
        "--base", str(head_path), "--head", str(base_path)]) == 1


def test_seed_then_tighten_on_the_committed_document_round_trips(
        tmp_path, monkeypatch):
    # The committed document, copied to tmp_path and passed through the
    # seed-refusal and tighten paths, keeps canonical bytes: the bot's
    # writes are the loader's writes.
    #
    # The sanctioned re-seed (issue #502) deletes the family for one
    # commit, so this seeds it into the tmp copy when it is absent rather
    # than skipping: the property under test is the round trip, and it
    # holds on the delete commit too.
    thresholds = _thresholds()
    ratchet = _ratchet()
    target = tmp_path / "ci-thresholds.json"
    target.write_bytes(
        subprocess_committed_bytes())
    if thresholds.suite_cost(thresholds.load(target)) == {}:
        # The fallback seed carries the identity, so the positive
        # assertion below holds on the delete-commit shape too (the
        # sibling test at test_seed_carries_the_workload_identity pins
        # the writer half). The tree identity matches the counter the
        # seed consults, so the seed is accepted (issue #598).
        monkeypatch.setattr(ratchet, "_tree_identity", lambda: 41234)
        measurement = _measurement_file(tmp_path, COUNTS)
        payload = json.loads(measurement.read_text(encoding="utf-8"))
        payload["tests_tree_lines"] = 41234
        measurement.write_text(json.dumps(payload), encoding="utf-8")
        assert ratchet.main([
            "--seed", str(measurement),
            "--thresholds", str(target)]) == 0
    doc = thresholds.load(target)
    # The workload identity (issue #524) rides beside the budgets on the
    # committed family — seed-supplied or committed — and the phases are
    # the invariant it must not displace.
    assert thresholds.SUITE_COST_IDENTITY in doc["suite_cost"]
    assert set(doc["suite_cost"]) == {
        "collection", "run", "residual", thresholds.SUITE_COST_IDENTITY}
    # The member is present, so a seed refuses and changes nothing.
    before = target.read_text(encoding="utf-8")
    assert ratchet.main(["--seed", str(_measurement_file(tmp_path, COUNTS)),
                         "--thresholds", str(target)]) == 1
    assert target.read_text(encoding="utf-8") == before
    # A tighten within the hysteresis writes nothing: the measurement
    # is built from the committed values themselves, one step cheaper.
    recorded = thresholds.load(target)["suite_cost"]
    within = {phase: recorded[phase]["measured"] - Decimal("0.1")
              for phase in ("collection", "run", "residual")}
    assert ratchet.main(
        ["--tighten", str(_measurement_file(tmp_path, within, "w.json")),
         "--thresholds", str(target)]) == 0
    assert target.read_text(encoding="utf-8") == before


def subprocess_committed_bytes():
    # Local import: only this helper needs the subprocess module.
    # pylint: disable-next=import-outside-toplevel
    import subprocess
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), "cat-file", "blob",
         "HEAD:.github/ci-thresholds.json"],
        capture_output=True, check=True).stdout


def test_seed_carries_the_workload_identity(tmp_path, monkeypatch):
    # The seed binds the budget to the workload it measured: the
    # measurement's tests_tree_lines enters the committed family
    # (SV-CI-RATCHETS, issue #524). The artifact must also name the tree
    # it lands on (issue #598): this one matches, so it seeds through.
    thresholds = _thresholds()
    ratchet = _ratchet()
    target = _written(tmp_path, _seeded_document())
    measurement = _measurement_file(tmp_path, COUNTS)
    payload = json.loads(measurement.read_text(encoding="utf-8"))
    payload["tests_tree_lines"] = 41234
    measurement.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(ratchet, "_tree_identity", lambda: 41234)
    assert ratchet.main(["--seed", str(measurement),
                         "--thresholds", str(target)]) == 0
    doc = thresholds.load(target)
    assert doc["suite_cost"]["tests_tree_lines"] == 41234


def test_seed_refuses_a_measurement_from_another_tree(
        tmp_path, capsys, monkeypatch):
    # The identity check is the seed half of SV-CI-RATCHETS' binding
    # (issue #598): the sanctioned re-seed measures the FINAL tree, so an
    # artifact whose tests_tree_lines names another tree is refused,
    # naming both counts, and the committed document is untouched.
    ratchet = _ratchet()
    target = _written(tmp_path, _seeded_document())
    before = target.read_text(encoding="utf-8")
    measurement = _measurement_file(tmp_path, COUNTS)
    payload = json.loads(measurement.read_text(encoding="utf-8"))
    payload["tests_tree_lines"] = 41234
    measurement.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(ratchet, "_tree_identity", lambda: 63211)
    assert ratchet.main(["--seed", str(measurement),
                         "--thresholds", str(target)]) == 1
    assert target.read_text(encoding="utf-8") == before
    err = capsys.readouterr().err
    assert "41234" in err and "63211" in err


def test_seed_refuses_a_measurement_above_this_tree(
        tmp_path, capsys, monkeypatch):
    # The mismatch refuses in BOTH directions: the issue's own motivating
    # seed (#571's first attempt) was an artifact measuring MORE lines
    # than the tree it landed on, so the artifact-above-tree side carries
    # its own kill for a widened-acceptance mutant.
    ratchet = _ratchet()
    target = _written(tmp_path, _seeded_document())
    before = target.read_text(encoding="utf-8")
    measurement = _measurement_file(tmp_path, COUNTS)
    payload = json.loads(measurement.read_text(encoding="utf-8"))
    payload["tests_tree_lines"] = 63211
    measurement.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(ratchet, "_tree_identity", lambda: 41234)
    assert ratchet.main(["--seed", str(measurement),
                         "--thresholds", str(target)]) == 1
    assert target.read_text(encoding="utf-8") == before
    err = capsys.readouterr().err
    assert "63211" in err and "41234" in err


def test_seed_from_an_identity_less_measurement_omits_the_field(tmp_path):
    # A measurement that predates the identity seeds a family without
    # one: the gate then behaves exactly as before this field existed.
    thresholds = _thresholds()
    ratchet = _ratchet()
    target = _written(tmp_path, _seeded_document())
    assert ratchet.main(["--seed", str(_measurement_file(tmp_path, COUNTS)),
                         "--thresholds", str(target)]) == 0
    doc = thresholds.load(target)
    assert doc["suite_cost"] == {
        phase: {"measured": COUNTS[phase],
                "floor": COUNTS[phase] + Decimal("1.5")}
        for phase in ("collection", "run", "residual")}


def test_tighten_preserves_the_identity(tmp_path, monkeypatch):
    thresholds = _thresholds()
    ratchet = _ratchet()
    target = _written(tmp_path, _seeded_document())
    measurement = _measurement_file(tmp_path, COUNTS)
    payload = json.loads(measurement.read_text(encoding="utf-8"))
    payload["tests_tree_lines"] = 41234
    measurement.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(ratchet, "_tree_identity", lambda: 41234)
    assert ratchet.main(["--seed", str(measurement),
                         "--thresholds", str(target)]) == 0
    cheaper = {phase: COUNTS[phase] - Decimal("5.0")
               for phase in ("collection", "run", "residual")}
    assert ratchet.main(["--tighten", str(_measurement_file(tmp_path, cheaper)),
                         "--thresholds", str(target)]) == 0
    doc = thresholds.load(target)
    assert doc["suite_cost"]["tests_tree_lines"] == 41234
    assert doc["suite_cost"]["run"] == {
        "measured": Decimal("299.9"), "floor": Decimal("301.4")}


def test_the_committed_identity_is_a_positive_integral_count(tmp_path):
    # The workload identity the seed binds to (SV-CI-RATCHETS, issue
    # #524): optional in the document, a positive integer when present,
    # carried through the canonical round trip; a refusal names it.
    thresholds = _thresholds()
    target = tmp_path / "ci-thresholds.json"
    doc = _document(suite={
        phase: {"measured": COUNTS[phase],
                "floor": COUNTS[phase] + Decimal("1.5")}
        for phase in ("collection", "run", "residual")})
    doc["suite_cost"]["tests_tree_lines"] = 41234
    thresholds.write(target, doc)
    loaded = thresholds.load(target)
    assert loaded["suite_cost"]["tests_tree_lines"] == 41234
    for bad, match in ((0, "positive integer"), (-5, "positive integer"),
                       (4.2, "positive integer"),
                       ("41234", "JSON number"), (True, "JSON number")):
        payload = json.loads(json.dumps(doc, default=float))
        payload["suite_cost"]["tests_tree_lines"] = bad
        target.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(ValueError, match=match):
            thresholds.load(target)


def test_the_suite_cost_reader_returns_phases_without_the_identity():
    """The accessor is the BUDGET reader (issue #571): the workload
    identity rides beside the budgets in the family, and a consumer
    that asks for the budgets must never receive the tree-line count
    as if it were a phase."""
    thresholds = _thresholds()
    doc = _document(suite={
        phase: {"measured": COUNTS[phase],
                "floor": COUNTS[phase] + Decimal("1.5")}
        for phase in ("collection", "run", "residual")})
    doc["suite_cost"]["tests_tree_lines"] = 41234
    phases = thresholds.suite_cost(doc)
    assert set(phases) == set(thresholds.SUITE_COST_PHASES)


def test_tree_identity_is_the_benchs_counter():
    # The seed's tree count IS the bench's (issue #598): the same
    # algorithm over the same anchor — the checkout holding the script,
    # not the --thresholds path or the CWD — so the refusal compares
    # like with like.
    ratchet = _ratchet()
    bench = _load("suite_bench")
    assert ratchet._tree_identity() == bench._tests_tree_lines(  # pylint: disable=protected-access
        REPO_ROOT)
