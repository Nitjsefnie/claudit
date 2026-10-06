"""Tests for the reparse family's own re-seed marker (issue #698).

The suite-cost re-seed's mechanics are pinned in ``test_ci_reseed.py``;
this file pins the SECOND marker, ``[reparse-re-seed]``, on the same
shape: what declares a reparse re-seed (its own marker, read by the
same bounded walk, its own family-present bound and visit cap), what
the marker buys (the reparse family's ABSENCE, and nothing else), and
what it must not buy — a raised reparse budget, another family's
absence, an ungated measurement. The walks of the two markers are
independent: a suite marker opens no reparse window and vice versa.
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
CI = REPO_ROOT / "scripts" / "ci"


def _load(name):
    """Import one scripts/ci module by path, as the CI entry points do.

    scripts/ci holds standalone entry points, not an importable
    package; the directory goes on sys.path first so a module's own
    importlib imports resolve.
    """
    if str(CI) not in sys.path:
        sys.path.insert(0, str(CI))
    spec = importlib.util.spec_from_file_location(name, CI / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# Load order is the import graph: guard reaches its siblings by name,
# so each has to exist under that name before it is executed.
reseed = _load("reseed")
thresholds = _load("thresholds")
size_baseline = _load("size_baseline")
reparse_report = _load("reparse_report")
reparse_ratchet = _load("reparse_ratchet")
guard = _load("thresholds_guard")
validate = thresholds.validate

MARKER = reseed.REPARSE_MARKER
GAP = Decimal("1.5")

_IDENTITY = ('-c', 'user.email=t@example.invalid', '-c', 'user.name=T',
             '-c', 'commit.gpgsign=false', '-c', 'init.defaultBranch=main')


def _git(repo, *args):
    subprocess.run(['git', '-C', str(repo), *_IDENTITY, *args],
                   check=True, capture_output=True, timeout=60)


def _repository(path: Path, message: str) -> Path:
    """A one-commit repository whose HEAD carries ``message``."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    (path / "tracked.txt").write_text("content\n", encoding="utf-8")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-q", "-m", message)
    reseed.clear_cache()
    return path


def _commit_thresholds(repo: Path, suite=True, reparse=True,
                       message: str = "record thresholds") -> None:
    """Commit a synthetic document naming which families it carries.

    Minimal bytes on purpose: the walk asks whether the committed
    document CARRIES a family — presence, never shape.
    """
    payload: dict = {}
    if suite:
        payload["suite_cost"] = {"run": {}}
    if reparse:
        payload["reparse"] = {"parse_body": {}}
    document = repo / ".github" / "ci-thresholds.json"
    document.parent.mkdir(parents=True, exist_ok=True)
    document.write_text(json.dumps(payload), encoding="utf-8")
    _git(repo, "add", ".github/ci-thresholds.json")
    _git(repo, "commit", "-q", "-m", message)


def _document(suite_cost=True, reparse=True):
    """A valid document, with or without either re-seedable family."""
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
    }
    if reparse:
        data[thresholds.REPARSE_FAMILY] = {
            phase: {metric: {"measured": Decimal("10.0"),
                             "floor": Decimal("11.5")}
                    for metric in thresholds.REPARSE_METRICS}
            for phase in thresholds.REPARSE_PHASES
        }
    if suite_cost:
        data[thresholds.SUITE_COST_FAMILY] = {
            phase: {"measured": Decimal("10.0"),
                    "floor": Decimal("11.5")}
            for phase in thresholds.SUITE_COST_PHASES
        }
    return data


def _write_strict(tmp_path: Path, name="base.json") -> Path:
    path = tmp_path / name
    thresholds.write(path, thresholds.normalise(_document(), False), False)
    return path


def _write_head(tmp_path: Path, **families) -> Path:
    """A head document written under the verdict that admits its shape:
    a family the document omits gets its admission flag set — the way
    the delete commit's own tree declared it."""
    absent = {"suite_cost": not families.get("suite_cost", True),
              "reparse": not families.get("reparse", True)}
    verdict = validate.ReseedVerdict(**absent)
    payload = thresholds.normalise(_document(**families), verdict)
    path = tmp_path / "head.json"
    path.write_text(
        json.dumps(payload, default=float, sort_keys=True, indent=2),
        encoding="utf-8")
    return path


def _measurement_file(path: Path, counted=True) -> Path:
    """A synthetic gateable measurement, both instruments, every phase."""
    measurement = reparse_report.Measurement(
        cpu_s=1.0, files=4, passes=10, records=9,
        phase_cpu_s={name: 0.25 for name in thresholds.REPARSE_PHASES},
        shares={name: Decimal("9.0") for name in thresholds.REPARSE_PHASES},
        instruction_counts={
            'available': counted,
            'reason': '' if counted else 'no INSTRUCTION event',
            'total_bytecodes': 400, 'overhead_per_call': 0,
            'phase_bytecodes': {}},
        instruction_per_file=(
            {name: Decimal("1.0") for name in thresholds.REPARSE_PHASES}
            if counted else None),
        instruction_note='' if counted else 'no INSTRUCTION event',
        perf_per_file=None, perf_note='')
    reparse_report.write_measurement(path, measurement)
    return path


@pytest.fixture(name="reparse_marker_tree")
def _reparse_marker_tree(tmp_path, monkeypatch):
    """A tree whose HEAD lineage declares the reparse re-seed: the ONE
    authority every side of a test reads, loader and ratchet alike.

    The delete's own document carries the suite-cost family and no
    reparse one — the sanctioned delete's exact shape.
    """
    repo = _repository(tmp_path / "reseed", f"delete {MARKER}")
    _commit_thresholds(repo, suite=True, reparse=False)
    monkeypatch.setitem(sys.modules, "reseed", reseed)
    monkeypatch.setattr(reseed, "ROOT", repo)
    reseed.clear_cache()
    yield repo
    reseed.clear_cache()


def _verdict(**families):
    return validate.ReseedVerdict(
        suite_cost=families.get("suite_cost", False),
        reparse=families.get("reparse", False))


# --- what declares a reparse re-seed ------------------------------------

def test_the_reparse_marker_is_the_documented_spelling():
    # A spelling change is a doctrine change; the family pairing is
    # spelled in reseed (importing thresholds there would put the
    # git-reading module on every loader consumer's import graph), so
    # the copies are pinned together.
    assert MARKER == '[reparse-re-seed]'
    # pylint: disable-next=protected-access
    assert reseed.MARKER_FOR[reseed.REPARSE_FAMILY] == MARKER
    assert reseed.REPARSE_FAMILY == thresholds.REPARSE_FAMILY
    assert reseed.SUITE_COST_FAMILY == thresholds.SUITE_COST_FAMILY
    # pylint: disable-next=protected-access
    assert reseed.MARKER == '[suite-cost-re-seed]'


def test_a_reparse_marker_declares_only_the_reparse_window(tmp_path):
    # The two walks are independent: the reparse marker's commit opens
    # the reparse family's window and the suite-cost family's walk on
    # the same tree reads closed — its own family is present there,
    # which closes the window at the first visit.
    repo = _repository(tmp_path / "r", f"delete {MARKER}")
    _commit_thresholds(repo, suite=True, reparse=False)
    assert reseed.in_flight(repo) is False
    assert reseed.in_flight(repo, family=reseed.REPARSE_FAMILY) is True


def test_the_suite_marker_declares_nothing_for_the_reparse_family(
        tmp_path):
    # The cross direction: a suite-cost delete's marker must not exempt
    # a reparse absence, so a reparse walk over a suite-marker tree
    # reads closed even though the reparse family is gone there.
    repo = _repository(tmp_path / "r", f"delete {reseed.MARKER}")
    _commit_thresholds(repo, suite=False, reparse=False)
    assert reseed.in_flight(repo) is True
    assert reseed.in_flight(repo, family=reseed.REPARSE_FAMILY) is False


def test_a_reparse_marker_beneath_bot_commits_still_declares(tmp_path):
    # The #509 shape, on the second marker: bot commits landing on the
    # delete before the seed must not outlast the window.
    repo = _repository(tmp_path / "r", f"delete {MARKER}")
    _commit_thresholds(repo, suite=True, reparse=False)
    _git(repo, "commit", "-q", "--allow-empty", "-m", "bot: refresh rates")
    reseed.clear_cache()
    assert reseed.in_flight(repo, family=reseed.REPARSE_FAMILY) is True


def test_a_reparse_seed_restores_the_strict_gate(tmp_path):
    # The seed commit restores the family, and the window closes on the
    # family-present bound: a later reparse-absent document is gated
    # exactly as a hand deletion.
    repo = _repository(tmp_path / "r", f"delete {MARKER}")
    _commit_thresholds(repo, suite=True, reparse=False)
    _commit_thresholds(repo, suite=True, reparse=True, message="seed")
    reseed.clear_cache()
    assert reseed.in_flight(repo, family=reseed.REPARSE_FAMILY) is False


def test_the_verdict_names_the_two_families():
    # The verdict type's fields ARE the re-seedable families, in the
    # order the loader admits them; reseed answers one probe per field.
    assert validate.ReseedVerdict._fields == (  # pylint: disable=protected-access
        reseed.SUITE_COST_FAMILY, reseed.REPARSE_FAMILY)
    verdict = _verdict(suite_cost=True, reparse=False)
    assert thresholds.verdict(verdict) is verdict


def test_admitted_maps_the_legacy_bool_to_the_first_family():
    # The legacy single-family bool — the shape every caller predating
    # the second marker spells — maps True to the suite-cost family
    # alone and False to strict; anything else is a forgotten verdict.
    families = (thresholds.SUITE_COST_FAMILY, thresholds.REPARSE_FAMILY)
    assert validate.admitted(True, families) == {
        thresholds.SUITE_COST_FAMILY: True,
        thresholds.REPARSE_FAMILY: False}
    assert validate.admitted(False, families) == {
        thresholds.SUITE_COST_FAMILY: False,
        thresholds.REPARSE_FAMILY: False}
    with pytest.raises(ValueError, match="thresholds.verdict"):
        validate.admitted(None, families)


# --- what the marker buys: the family's absence, and only that ---------

def test_the_reparse_marker_admits_an_absent_reparse_family():
    # The family stays ABSENT in the result rather than normalising to
    # an empty mapping, so the document's bytes round-trip unchanged.
    loaded = thresholds.normalise(
        _document(reparse=False), _verdict(reparse=True))
    assert thresholds.REPARSE_FAMILY not in loaded
    assert thresholds.reparse(loaded, _verdict(reparse=True)) == {}


def test_a_reparse_absence_needs_its_own_marker():
    # A suite marker's window, a strict door and the legacy suite bool
    # all refuse a reparse-absent document: the tolerance is the own
    # marker's, never the other family's.
    for verdict in (_verdict(), _verdict(suite_cost=True),
                    True):
        with pytest.raises(ValueError, match="missing field: reparse"):
            thresholds.normalise(_document(reparse=False), verdict)


def test_the_reparse_marker_admits_no_suite_absence():
    with pytest.raises(ValueError, match="missing field: suite_cost"):
        thresholds.normalise(
            _document(suite_cost=False), _verdict(reparse=True))


def test_both_families_absent_needs_both_markers():
    # With only the suite marker's window, the still-required reparse
    # family is what the refusal names.
    with pytest.raises(ValueError, match="missing field: reparse"):
        thresholds.normalise(_document(suite_cost=False, reparse=False),
                             _verdict(suite_cost=True))
    loaded = thresholds.normalise(
        _document(suite_cost=False, reparse=False),
        _verdict(suite_cost=True, reparse=True))
    assert thresholds.SUITE_COST_FAMILY not in loaded
    assert thresholds.REPARSE_FAMILY not in loaded


def test_an_empty_reparse_family_is_refused_with_and_without_the_marker():
    # The suite decoy's reparse twin: `"reparse": {}` is the family
    # PRESENT and malformed, and a loader testing emptiness rather than
    # presence would normalise it to the absent family only the marker
    # authorises. Both verdicts passed EXPLICITLY: the widened class
    # must not be reachable without the marker that widens it.
    for verdict in (_verdict(), _verdict(reparse=True)):
        data = _document()
        data[thresholds.REPARSE_FAMILY] = {}
        with pytest.raises(ValueError, match="missing reparse CPU phase"):
            thresholds.normalise(data, verdict)


def test_the_marker_relaxes_nothing_else():
    # One family's absence. An unknown key and a malformed OTHER family
    # are still refusals on a reparse-window tree.
    unknown = _document(reparse=False)
    unknown["surprise"] = "value"
    with pytest.raises(ValueError, match="unknown field: surprise"):
        thresholds.normalise(unknown, _verdict(reparse=True))

    malformed = _document(reparse=False)
    malformed[thresholds.SUITE_COST_FAMILY]["run"]["floor"] = Decimal("1.0")
    with pytest.raises(ValueError, match="must be above measured"):
        thresholds.normalise(malformed, _verdict(reparse=True))


def test_normalise_refuses_a_non_verdict():
    with pytest.raises(ValueError, match="thresholds.verdict"):
        thresholds.normalise(_document(), None)
    with pytest.raises(ValueError, match="thresholds.verdict"):
        thresholds.normalise(_document(), "yes")


def test_a_reparse_absent_document_round_trips_through_write(
        tmp_path, reparse_marker_tree):
    # The ratchets write the document they read. A re-seed commit's
    # document must survive that round trip, or the master-push bot
    # fails on a state the marker made legal.
    target = tmp_path / "ci-thresholds.json"
    verdict = _verdict(reparse=True)
    thresholds.write(target, thresholds.normalise(
        _document(reparse=False), verdict), verdict)
    assert "reparse" not in json.loads(target.read_text(encoding="utf-8"))
    assert thresholds.reparse(thresholds.load(target), None) == {}


def test_the_window_relaxes_no_other_direction(
        tmp_path, reparse_marker_tree):
    # The marker buys the reparse family's absence — not a licence
    # elsewhere. A lowered coverage floor inside the window is still a
    # forbidden move.
    base, established = _base(tmp_path)
    lowered = _document(reparse=False)
    lowered["coverage"]["python"] = {"measured": Decimal("80.0"),
                                     "floor": Decimal("78.5")}
    head = thresholds.normalise(lowered, _verdict(reparse=True))
    moves = guard.forbidden_moves(base, head, established)
    assert any("coverage.python" in move for move in moves), moves


# --- the gate and the ratchet inside the window -------------------------

def test_the_reparse_gate_passes_through_with_no_budget_and_says_so(
        tmp_path, reparse_marker_tree, capsys):
    # The reparse gate on a re-seed commit: there is nothing to compare
    # the measurement against, and the gate must say THAT rather than
    # claim the run was within budget.
    document = _write_head(tmp_path, reparse=False)
    code = reparse_report.check(
        _measurement_file(tmp_path / "m.json"), document)
    assert code == 0
    assert reparse_report.NO_BUDGET_LINE in capsys.readouterr().out


def test_the_reparse_gate_still_fails_an_unmeasured_count_mid_window(
        tmp_path, reparse_marker_tree, capsys):
    # The pass-through is about the BUDGET, not about the measurement:
    # an absent count is an absent gate even where nothing compares it.
    document = _write_head(tmp_path, reparse=False)
    code = reparse_report.check(
        _measurement_file(tmp_path / "m.json", counted=False), document)
    assert code == 1
    err = capsys.readouterr().err
    assert "NOT MEASURED" in err
    assert "no INSTRUCTION event" in err


def test_the_reparse_gate_still_fails_an_over_budget_phase_when_budgets_exist(
        tmp_path, reparse_marker_tree, capsys):
    # Removing the pass-through cannot smuggle in a disabled gate: with
    # a family recorded, the gate fails exactly as it always did. The
    # document is written on the tree that declares the window, but the
    # family IS present, so the gate holds it to its budgets.
    document = _write_head(tmp_path)
    path = _measurement_file(tmp_path / "m.json")
    measurement = json.loads(path.read_text(encoding="utf-8"))
    measurement["phases"]["parse_body"]["bytecode_hundreds_per_file"] = "40.0"
    path.write_text(json.dumps(measurement), encoding="utf-8")
    assert reparse_report.check(path, document) == 1
    assert "parse_body: 40.0" in capsys.readouterr().err


def test_tighten_mid_window_is_a_declared_no_op(
        tmp_path, reparse_marker_tree, capsys):
    # The master-push bot runs this on every push, including the tip
    # that carries the marker. Tightening an absent family is nothing
    # to do, not a crash.
    document = _write_head(tmp_path, reparse=False)
    measurement = _measurement_file(tmp_path / "m.json")
    before = document.read_text(encoding="utf-8")
    assert reparse_ratchet.main([
        "--measured-file", str(measurement),
        "--thresholds", str(document)]) == 0
    assert document.read_text(encoding="utf-8") == before
    assert "nothing to tighten" in capsys.readouterr().out


def test_update_returns_none_mid_window(tmp_path, reparse_marker_tree):
    # The ratchet's helper on the window's document: no budget, no
    # moves, and no crash — the CLI says the rest.
    document = _head_data(reparse=False)
    readings = {
        phase: {"share": "9.0", "bytecodes": "1.0"}
        for phase in thresholds.REPARSE_PHASES}
    assert reparse_ratchet.update(document, readings) is None


def _head_data(**families):
    """The normalised head as a Python document, written under the
    verdict that admits the families it omits."""
    absent = {"suite_cost": not families.get("suite_cost", True),
              "reparse": not families.get("reparse", True)}
    return thresholds.normalise(
        _document(**families), validate.ReseedVerdict(**absent))


def _base(tmp_path: Path):
    """The base document and its established members, the guard's way."""
    # pylint: disable-next=protected-access
    return guard._load_base(_write_strict(tmp_path))


@pytest.fixture(name="strict_tree")
def _strict_tree(tmp_path, monkeypatch):
    """A tree that declares nothing, for the fail-closed direction."""
    repo = _repository(tmp_path / "strict", "base")
    _commit_thresholds(repo, suite=True, reparse=True)
    monkeypatch.setitem(sys.modules, "reseed", reseed)
    monkeypatch.setattr(reseed, "ROOT", repo)
    reseed.clear_cache()
    yield repo
    reseed.clear_cache()


def test_main_reads_the_window_off_the_tree_it_gates(
        tmp_path, reparse_marker_tree, strict_tree, monkeypatch, capsys):
    # The verdict is the TREE's, not the caller's: the same head bytes
    # are read where the reparse marker's lineage declares and refused
    # on a tree that declares nothing. Both trees are synthetic, so the
    # control does not depend on the checkout's own ancestry.
    head = tmp_path / "head-no-reparse.json"
    head.write_text(
        json.dumps(_document(reparse=False), default=float, indent=2),
        encoding="utf-8")
    base = _write_strict(tmp_path)
    # Both tree fixtures re-bind ROOT; the walk's authority is bound
    # explicitly at each half, in the order the test reads it.
    monkeypatch.setattr(reseed, "ROOT", reparse_marker_tree)
    reseed.clear_cache()
    assert guard.main(["--base", str(base), "--head", str(head)]) == 0
    capsys.readouterr()
    monkeypatch.setattr(reseed, "ROOT", strict_tree)
    reseed.clear_cache()
    assert guard.main(["--base", str(base), "--head", str(head)]) == 1


# --- the guard inside the window ----------------------------------------

def test_the_guard_passes_the_sanctioned_reparse_delete(tmp_path):
    # The intermediate commit REMOVES the reparse family while the base
    # still carries it — the one move the guard must read as legal, and
    # only because the loader admits the document.
    base, established = _base(tmp_path)
    head = thresholds.normalise(_document(reparse=False),
                                _verdict(reparse=True))
    assert guard.forbidden_moves(base, head, established) == []


def test_the_reparse_marker_does_not_legalise_an_upward_move(tmp_path):
    # The point of the two-commit sequence: the marker buys the
    # absence, and the guard's refusal of a RAISED reparse budget is
    # untouched.
    base, established = _base(tmp_path)
    raised = _document()
    raised["reparse"]["parse_body"]["bytecodes"] = {
        "measured": Decimal("60.0"), "floor": Decimal("61.5")}
    head = thresholds.normalise(raised, _verdict(reparse=True))
    moves = guard.forbidden_moves(base, head, established)
    assert any("reparse.parse_body" in move for move in moves), moves


def test_the_reparse_remedy_names_the_reseed_path():
    assert "own-marker re-seed path" in guard.REPARSE_REMEDY


# --- the walk's fail-closed edges, on the second family ----------------

def test_the_reparse_walk_fails_closed_past_its_cap(tmp_path):
    # The cap's outer edge, on the reparse walk: the declaring commit is
    # the ELEVENTH, one past the bound, and nothing below it is read.
    repo = _repository(tmp_path / "r", f"delete {MARKER}")
    _commit_thresholds(repo, suite=True, reparse=False)
    for number in range(10):
        _git(repo, "commit", "-q", "--allow-empty", "-m", f"bot {number}")
    reseed.clear_cache()
    assert reseed.in_flight(repo, family=reseed.REPARSE_FAMILY) is False


def test_the_reparse_walk_with_no_thresholds_file(tmp_path):
    # No committed document anywhere in the span: nothing can close the
    # window by content, so only the visit cap closes it — a declaring
    # commit inside the cap declares, because a repo that has never
    # carried the file has no family-present bound; a history with no
    # marker reads closed, exactly as any undeclaring tree.
    repo = _repository(tmp_path / "r", f"delete {MARKER}")
    reseed.clear_cache()
    assert reseed.in_flight(repo, family=reseed.REPARSE_FAMILY) is True
    plain = _repository(tmp_path / "plain", "base")
    reseed.clear_cache()
    assert reseed.in_flight(plain, family=reseed.REPARSE_FAMILY) is False


def test_the_reparse_gate_refuses_an_absent_document_outside_a_window(
        tmp_path, strict_tree):
    # The fail-closed door the window opens: the same reparse-absent
    # document over a tree that declares nothing is a refusal at load,
    # and the gate propagates it instead of reading it as a pass.
    document = tmp_path / "ci-thresholds.json"
    document.write_text(
        json.dumps(_document(reparse=False), default=float, indent=2),
        encoding="utf-8")
    with pytest.raises(ValueError, match="missing field: reparse"):
        reparse_report.check(
            _measurement_file(tmp_path / "m.json"), document)
