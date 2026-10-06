"""Tests for the suite-cost re-seed marker (issue #502).

SV-CI-RATCHETS sanctions a ``suite_cost`` re-seed and forbids raising a
recorded budget by hand, but the guard refuses an upward move whenever
the base already carries the family, so a HIGHER re-seed cannot land as
one change and the delete-then-seed sequence needs an intermediate
commit whose document carries no budget at all.

The marker is that intermediate step, and every test below pins one of
the three things that must stay true while it exists: what declares a
re-seed (the marker message, read by a bounded walk from HEAD back to
the last family-present commit, so the tolerance spans the whole
delete-then-seed sequence and not one commit of it), what the marker
buys (the family's ABSENCE, and nothing else), and what it must not
buy (a raised budget, an ungated measurement, a crash in the ratchet).
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
CI = REPO_ROOT / "scripts" / "ci"


def _load(name):
    """Import one scripts/ci module by path, as the CI entry points do.

    scripts/ci holds standalone entry points, not an importable
    package, and several of them reach their siblings by name — so a
    module loaded here has to be reachable under that name too.
    """
    spec = importlib.util.spec_from_file_location(name, CI / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# Load order is the import graph: thresholds_guard reaches its siblings
# by name, so each one has to exist under that name before the next is
# executed — and a module loaded twice would be two objects, with the
# loader holding the first one.
reseed = _load("reseed")
thresholds = _load("thresholds")
suite_phases = _load("suite_phases")
suite_report = _load("suite_report")
suite_ratchet = _load("suite_ratchet")
size_baseline = _load("size_baseline")
suppression_baseline = _load("suppression_baseline")
ratchet = _load("ratchet")
reparse_ratchet = _load("reparse_ratchet")
guard = _load("thresholds_guard")

MARKER = reseed.MARKER
GAP = Decimal("1.5")
COUNTS = {"collection": "14.2", "run": "339.6", "residual": "6.3"}

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


def _commit_thresholds(repo: Path, family: bool,
                       message: str = "record thresholds") -> None:
    """Commit the document the family-present bound reads, with or
    without the suite-cost family.

    Minimal bytes on purpose: the walk asks only whether the committed
    document CARRIES the family — presence, never shape — so the
    synthetic document carries the key and nothing else.
    """
    document = repo / ".github" / "ci-thresholds.json"
    document.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        {"suite_cost": {"run": {}}} if family else {})
    document.write_text(json.dumps(payload), encoding="utf-8")
    _git(repo, "add", ".github/ci-thresholds.json")
    _git(repo, "commit", "-q", "-m", message)


def _document(suite_cost=True, measured="95.6", floor="94.1"):
    """A valid document, with or without the suite-cost family."""
    data = {
        "schema_version": 1,
        "coverage": {
            "python": {"measured": Decimal(measured),
                       "floor": Decimal(floor)},
            "javascript": {"measured": Decimal("90.4"),
                           "floor": Decimal("88.9")},
        },
        "reparse": {
            phase: {metric: {"measured": Decimal("10.0"),
                             "floor": Decimal("11.5")}
                    for metric in thresholds.REPARSE_METRICS}
            for phase in thresholds.REPARSE_PHASES
        },
        "module_size_baseline": {},
        "pylint_suppression_baseline": {},
    }
    if suite_cost:
        data[thresholds.SUITE_COST_FAMILY] = {
            phase: {"measured": Decimal(value),
                    "floor": Decimal(value) + GAP}
            for phase, value in COUNTS.items()
        }
    return data


@pytest.fixture(name="marker_tree")
def _marker_tree(tmp_path, monkeypatch):
    """A tree whose HEAD declares the re-seed, as the ONE authority every
    side of a test reads.

    A loader's default verdict comes from the checkout it runs in, so a
    test that authored its document under one verdict and read it back
    under the ambient one is green only on the commit that happens to
    carry the marker. Both ends must read the same declared authority.
    """
    repo = _repository(tmp_path / "marker", f"delete {MARKER}")
    # Bind the name thresholds.verdict() IMPORTS, not just this module's
    # own binding: another test file loads its own 'reseed' at
    # collection time, and a patch on a module nothing will import is a
    # patch that silently reads the real checkout instead.
    monkeypatch.setitem(sys.modules, "reseed", reseed)
    monkeypatch.setattr(reseed, "ROOT", repo)
    reseed.clear_cache()
    yield repo
    reseed.clear_cache()


@pytest.fixture(name="plain_tree")
def _plain_tree(tmp_path, monkeypatch):
    """A tree that declares nothing, for the fail-closed direction."""
    repo = _repository(tmp_path / "plain", "feat: something else")
    monkeypatch.setitem(sys.modules, "reseed", reseed)
    monkeypatch.setattr(reseed, "ROOT", repo)
    reseed.clear_cache()
    yield repo
    reseed.clear_cache()


def subprocess_committed_bytes():
    """The COMMITTED document's bytes, read from git rather than disk."""
    return subprocess.run(
        ["git", "-C", str(REPO_ROOT), "cat-file", "blob",
         "HEAD:.github/ci-thresholds.json"],
        capture_output=True, check=True).stdout


def _counts():
    """A runner measurement's per-phase counts, as the ratchet reads
    them: exact Decimals out of the written file."""
    return {phase: Decimal(value) for phase, value in COUNTS.items()}


def _base(tmp_path: Path):
    """The base document and its established members, the guard's way.

    The guard's own base loader, deliberately: a hand-rolled stand-in
    would re-derive what counts as established and could drift from the
    rule under test.
    """
    path = tmp_path / "base.json"
    thresholds.write(path, thresholds.normalise(_document(), False), False)
    # pylint: disable-next=protected-access
    return guard._load_base(path)


def _measurement_file(path: Path, counts=None, instrument="instruction_count",
                      hash_seed="0") -> Path:
    counts = counts or COUNTS
    phases = {
        phase: {"million_instructions": value, "process_time_s": "1.000"}
        for phase, value in counts.items()
    }
    total = str(sum(Decimal(value) for value in counts.values()))
    path.write_text(json.dumps({
        "instrument": instrument, "unit": thresholds.SUITE_COST_UNIT,
        "hash_seed": hash_seed, "tests": 325, "fixture": "f.txt",
        "interpreter": "3.13.14", "phases": phases,
        "total_million_instructions": total,
    }), encoding="utf-8")
    return path


# --- what declares a re-seed --------------------------------------------

def test_the_marker_is_the_documented_spelling():
    # A spelling change is a doctrine change: the marker is documented
    # as this token, and every commit that depends on it spells it so.
    # The family literal is pinned with it because reseed spells its
    # own copy — importing thresholds for the constant would put the
    # git-reading module on every loader consumer's import graph.
    assert MARKER == '[suite-cost-re-seed]'
    # pylint: disable-next=protected-access
    assert reseed.SUITE_COST_FAMILY == thresholds.SUITE_COST_FAMILY


def test_a_plain_head_declares_nothing(tmp_path):
    repo = _repository(tmp_path / "r", "feat: something else")
    assert reseed.in_flight(repo) is False


def test_a_marker_in_the_head_subject_declares_a_reseed(tmp_path):
    repo = _repository(tmp_path / "r", f"feat: drop it {MARKER}")
    assert reseed.in_flight(repo) is True


def test_a_marker_anywhere_in_the_message_declares_a_reseed(tmp_path):
    repo = _repository(
        tmp_path / "r",
        "feat: drop the stale suite_cost budget\n\n"
        f"Body: {MARKER} -- the seed lands in the next change.\n")
    assert reseed.in_flight(repo) is True


def test_the_head_behind_a_pull_request_merge_declares_a_reseed(tmp_path):
    # A pull-request run checks out the MERGE commit, whose own message
    # is GitHub's and carries no marker; the pull request's head, which
    # is what declared it, hangs off it as the second parent.
    base = _repository(tmp_path / "base", "base")
    _git(base, "checkout", "-q", "-b", "feature")
    _git(base, "commit", "-q", "--allow-empty", "-m", f"delete {MARKER}")
    _git(base, "checkout", "-q", "main")
    _git(base, "merge", "-q", "--no-ff", "feature",
         "-m", "Merge pull request #1 from a/feature")
    reseed.clear_cache()
    assert reseed.in_flight(base) is True


def test_a_marker_beneath_the_pr_head_declares_through_the_merge(tmp_path):
    # The same shape with a pull-request branch that grew after the
    # delete: the marker is no longer the merge's second parent itself,
    # so a read scoped to HEAD and HEAD^2 misses it (its own HEAD^2 is
    # the follow-up commit). The walk descends into the declared
    # lineage and reads it there.
    base = _repository(tmp_path / "base", "base")
    _git(base, "checkout", "-q", "-b", "feature")
    _git(base, "commit", "-q", "--allow-empty", "-m", f"delete {MARKER}")
    _git(base, "commit", "-q", "--allow-empty", "-m", "address review")
    _git(base, "checkout", "-q", "main")
    _git(base, "merge", "-q", "--no-ff", "feature",
         "-m", "Merge pull request #2 from a/feature")
    reseed.clear_cache()
    assert reseed.in_flight(base) is True


def test_a_commit_on_top_of_the_marker_still_declares_it(tmp_path):
    # The #509 shape: the hourly pricing bot landed on the delete
    # before the seed, and a tolerance scoped to HEAD/HEAD^2 lasted
    # exactly one commit — the bot commit was HEAD with no marker and
    # HEAD^2 resolved to nothing. The walk reads back past it, so the
    # window stays open until the family-present bound or the visit
    # cap, whichever comes first.
    repo = _repository(tmp_path / "r", f"delete {MARKER}")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "bot: refresh rates")
    reseed.clear_cache()
    assert reseed.in_flight(repo) is True


def test_the_walk_reads_a_marker_at_its_tenth_visit(tmp_path):
    # The cap's inner edge: the declaring commit is the tenth commit the
    # walk reads, exactly at the bound, and is still read.
    repo = _repository(tmp_path / "r", f"deep {MARKER}")
    for number in range(9):
        _git(repo, "commit", "-q", "--allow-empty", "-m", f"bot {number}")
    reseed.clear_cache()
    assert reseed.in_flight(repo) is True


def test_the_walk_fails_closed_past_its_cap(tmp_path):
    # The cap's outer edge: the declaring commit is the ELEVENTH commit,
    # one past the bound, and nothing below it is read. Fail closed —
    # the tree is gated exactly as a tree with no marker at all.
    repo = _repository(tmp_path / "r", f"deep {MARKER}")
    for number in range(10):
        _git(repo, "commit", "-q", "--allow-empty", "-m", f"bot {number}")
    reseed.clear_cache()
    assert reseed.in_flight(repo) is False


def test_a_marker_beyond_the_family_present_bound_declares_nothing(
        tmp_path):
    # The bound is family presence, not the cap: once the seed has
    # restored the family, the window is closed, and a LATER
    # family-absent commit (a hand deletion, no marker) is not exempt —
    # the declaring commit lies below the bound and is never read.
    # Each commit here carries the real document, so the bound is
    # found by content.
    repo = _repository(tmp_path / "r", "seed the base")
    _commit_thresholds(repo, family=True)
    _git(repo, "commit", "-q", "--allow-empty", "-m", f"delete {MARKER}")
    _commit_thresholds(repo, family=False)
    _git(repo, "commit", "-q", "--allow-empty", "-m", "seed the family")
    _commit_thresholds(repo, family=True)
    _git(repo, "commit", "-q", "--allow-empty",
         "-m", "delete again, no marker")
    _commit_thresholds(repo, family=False)
    reseed.clear_cache()
    assert reseed.in_flight(repo) is False


def test_the_bound_closes_on_a_present_family_at_the_head_itself(
        tmp_path):
    # The bound, checked at the first visit: a tree whose own committed
    # document carries the family stops the walk immediately, whatever
    # declares below it — the seed commit itself is gated strictly, and
    # strictly is exactly what a family-present document satisfies.
    repo = _repository(tmp_path / "r", "seed the base")
    _commit_thresholds(repo, family=True)
    _git(repo, "commit", "-q", "--allow-empty", "-m", f"delete {MARKER}")
    _commit_thresholds(repo, family=False)
    _git(repo, "commit", "-q", "--allow-empty", "-m", "seed the family")
    _commit_thresholds(repo, family=True)
    reseed.clear_cache()
    assert reseed.in_flight(repo) is False


def test_a_marker_on_a_family_present_commit_opens_nothing(tmp_path):
    # The bound outranks the marker, whatever the message says. The
    # marker sits here on the very commit that restores the family — an
    # off-doctrine placement (its documented home is the family-ABSENT
    # delete) — and the family-absent commit above it is a hand
    # deletion. The bound closes the window at that commit, so the
    # misplaced marker below it is never consulted.
    repo = _repository(tmp_path / "r", "seed the base")
    _commit_thresholds(repo, family=True)
    _commit_thresholds(repo, family=False, message="delete, no marker")
    _commit_thresholds(repo, family=True, message=f"restore {MARKER}")
    _commit_thresholds(repo, family=False, message="hand delete, no marker")
    reseed.clear_cache()
    assert reseed.in_flight(repo) is False


def test_a_tree_without_git_metadata_fails_closed(tmp_path):
    # No git, no HEAD, no marker: the tree is gated exactly as it is
    # today. Fails CLOSED, never open.
    plain = tmp_path / "plain"
    plain.mkdir()
    assert reseed.in_flight(plain) is False


def test_the_probe_is_cached_until_it_is_cleared(tmp_path):
    # The loader asks on every read and the suite reads it hundreds of
    # times, so the probe is cached per tree; a caller that changes the
    # tree underneath itself clears it.
    repo = _repository(tmp_path / "r", "feat: nothing yet")
    assert reseed.in_flight(repo) is False
    _git(repo, "commit", "-q", "--allow-empty", "-m", f"delete {MARKER}")
    assert reseed.in_flight(repo) is False, "the probe should have cached"
    reseed.clear_cache()
    assert reseed.in_flight(repo) is True


# --- what the marker buys: the absence, and only the absence ------------

def test_an_absent_family_is_refused_without_the_marker():
    with pytest.raises(ValueError, match="missing field: suite_cost"):
        thresholds.normalise(_document(suite_cost=False), False)


def test_the_marker_admits_an_absent_family():
    # The family stays ABSENT in the result rather than normalising to
    # an empty mapping, so the document's bytes round-trip unchanged.
    loaded = thresholds.normalise(_document(suite_cost=False), True)
    assert thresholds.SUITE_COST_FAMILY not in loaded
    assert thresholds.suite_cost(loaded, True) == {}


def test_an_empty_family_is_refused_with_and_without_the_marker():
    # The decoy the widening must not admit: `"suite_cost": {}` is the
    # family PRESENT and malformed, and a loader that tested for
    # emptiness rather than presence would normalise it to the same
    # absent family only the marker authorises — so a hand-deleted
    # budget would reach the no-budget state on any tree, with the
    # direction guard and the suite-cost gate both reading it as legal.
    # Both verdicts passed EXPLICITLY, because the widened class must
    # not be reachable without the marker that widens it — and because
    # a verdict read from the ambient tree would make this control
    # commit-dependent, which is the defect it is a decoy for.
    for declared in (False, True):
        data = _document()
        data[thresholds.SUITE_COST_FAMILY] = {}
        with pytest.raises(ValueError, match="missing suite cost"):
            thresholds.normalise(data, declared)


def test_an_empty_family_never_reaches_the_gate_or_the_guard(
        tmp_path, plain_tree, capsys):
    # The same shape, one level up: the guard and the gate must both
    # refuse it on a tree that declares nothing, exactly as master did.
    document = tmp_path / "ci-thresholds.json"
    thresholds.write(document, thresholds.normalise(_document(), False), False)
    document.write_text(json.dumps(
        {**_document(), thresholds.SUITE_COST_FAMILY: {}}, default=float),
        encoding="utf-8")
    # check() raises on a document it cannot read (suite_bench's main
    # turns that into the step's nonzero); the guard turns it into its
    # own nonzero. Neither may reach the pass-through.
    with pytest.raises(ValueError, match="missing suite cost phase"):
        suite_report.check(_measurement_file(tmp_path / "m.json"), document)
    assert guard.main(["--base", str(_write_base(tmp_path)),
                       "--head", str(document)]) == 1
    assert "missing suite cost phase" in capsys.readouterr().err


def _write_base(tmp_path: Path) -> Path:
    path = tmp_path / "base.json"
    thresholds.write(path, thresholds.normalise(_document(), False), False)
    return path


def test_the_marker_relaxes_nothing_else(tmp_path):
    # The tolerance is one field's ABSENCE. A marker is not a licence:
    # an unknown key, a missing other family, and a family that is
    # present but malformed are all still refusals.
    unknown = _document(suite_cost=False)
    unknown["surprise"] = "value"
    with pytest.raises(ValueError, match="unknown field: surprise"):
        thresholds.normalise(unknown, True)

    no_reparse = _document(suite_cost=False)
    del no_reparse["reparse"]
    with pytest.raises(ValueError, match="missing field: reparse"):
        thresholds.normalise(no_reparse, True)

    malformed = _document()
    malformed[thresholds.SUITE_COST_FAMILY]["run"]["floor"] = Decimal("1.0")
    with pytest.raises(ValueError, match="must be above measured"):
        thresholds.normalise(malformed, True)


def test_load_reads_the_marker_from_the_tree_it_asks_about(
        tmp_path, monkeypatch, plain_tree):
    # The verdict is the TREE's, not the caller's: load() asks the
    # repository the CI scripts live in, so the same bytes are refused
    # on a tree that declares nothing and read on one that does. The
    # authority is switched by the fixture's own patch, which is what
    # keeps the two halves of this control on the same declared tree.
    document = tmp_path / "ci-thresholds.json"
    document.write_text(json.dumps(
        _document(suite_cost=False), default=float), encoding="utf-8")

    with pytest.raises(ValueError, match="missing field: suite_cost"):
        thresholds.load(document)

    repo = _repository(tmp_path / "r", f"delete {MARKER}")
    monkeypatch.setitem(sys.modules, "reseed", reseed)
    monkeypatch.setattr(reseed, "ROOT", repo)
    reseed.clear_cache()
    assert thresholds.suite_cost(thresholds.load(document), True) == {}


def test_a_caller_can_refuse_the_tolerance_explicitly():
    # `reseed_in_flight=False` is the strict door: a caller that means
    # to pin the loader's own rules gets them whatever the tree says.
    with pytest.raises(ValueError, match="missing field: suite_cost"):
        thresholds.normalise(_document(suite_cost=False), False)


def test_normalise_refuses_a_verdict_of_none():
    # No default, and None is not one: a caller that forgets the verdict
    # gets a refusal naming the one named way to ask, rather than a
    # silent strict read — the shape that reddened the master-only
    # ratchet step one call below the loader.
    with pytest.raises(ValueError, match="thresholds.verdict"):
        thresholds.normalise(_document(), None)


def test_an_absent_family_round_trips_through_write(tmp_path, marker_tree):
    # The ratchets write the document they read. A re-seed commit's
    # document must survive that round trip, or the master-push bot
    # fails on a state the marker made legal.
    target = tmp_path / "ci-thresholds.json"
    thresholds.write(target, thresholds.normalise(_document(False), True))
    assert "suite_cost" not in json.loads(
        target.read_text(encoding="utf-8"))
    assert thresholds.suite_cost(
        thresholds.load(target, None), True) == {}
    reseed.clear_cache()


def test_the_master_ratchet_step_runs_on_a_re_seed_tip(tmp_path,
                                                       marker_tree):
    """`tests.yml` runs seven ratchet commands on EVERY master push.

    Between this commit's merge and the seed commit's, this document is
    the tip's, so that step runs against a family-absent document. A
    suite cost budget mid-re-seed is no reason to refuse to raise a
    coverage calibration or tighten a reparse phase, and it is exactly
    the reason the two ratchets' helpers must read the same verdict the
    loader did: reading strict one call deeper reddened this step on
    every push (issue #502's review round 2).

    Each command runs against a COPY of the committed bytes: the
    verdict under test is the tree's, while the writes stay out of the
    checkout.
    """
    committed = REPO_ROOT / ".github" / "ci-thresholds.json"
    git_meta.require_own_git_metadata(REPO_ROOT)
    target = tmp_path / "ci-thresholds.json"
    target.write_bytes(subprocess_committed_bytes())
    before = target.read_text(encoding="utf-8")
    doc = thresholds.load(target)

    assert ratchet.main(["--measured", "95.7", "--thresholds",
                         str(target)]) == 0
    assert ratchet.main(["--language", "javascript", "--measured", "90.5",
                         "--thresholds", str(target)]) == 0
    # No raise and no tighten is due: both must leave the file alone.
    assert target.read_text(encoding="utf-8") == before

    readings = {
        phase: {metric: str(record["measured"])
                for metric, record in doc["reparse"][phase].items()}
        for phase in thresholds.REPARSE_PHASES}
    assert reparse_ratchet.update(doc, readings) is None

    assert size_baseline.main(["--tighten", "--thresholds",
                               str(target)]) == 0
    assert suppression_baseline.main(["--tighten", "--thresholds",
                                      str(target)]) == 0
    assert target.read_text(encoding="utf-8") == before
    assert committed.exists()


def test_the_coverage_ratchet_would_raise_on_a_re_seed_tip(tmp_path,
                                                           marker_tree):
    # The control above proves the step does not crash; this proves the
    # families are still the ratchets' to move. Reading strict one call
    # deeper would refuse before it ever got here, so the raise is the
    # only thing that separates "runs" from "runs".
    target = tmp_path / "ci-thresholds.json"
    target.write_bytes(subprocess_committed_bytes())
    assert ratchet.main(["--measured", "99.9", "--thresholds",
                         str(target)]) == 0
    assert "99.9" in target.read_text(encoding="utf-8")


# --- what the marker must not buy ---------------------------------------

def test_the_guard_passes_the_sanctioned_delete(tmp_path):
    # The intermediate commit REMOVES the family while the base still
    # carries it. That is the one move the guard must read as legal,
    # and it is legal only because the loader admits the document.
    base, established = _base(tmp_path)
    head = thresholds.normalise(_document(suite_cost=False), True)
    assert guard.forbidden_moves(base, head, established) == []


def test_the_marker_does_not_legalise_an_upward_move(tmp_path):
    # The point of the two-commit sequence: the marker buys the
    # absence, and the guard's refusal of a RAISED budget is untouched.
    base, established = _base(tmp_path)
    higher = _document()
    higher[thresholds.SUITE_COST_FAMILY]["run"] = {
        "measured": Decimal("400.0"), "floor": Decimal("401.5")}
    head = thresholds.normalise(higher, True)
    moves = guard.forbidden_moves(base, head, established)
    assert any("suite_cost.run" in move for move in moves), moves


def test_the_guard_still_refuses_a_lowered_coverage_value(tmp_path):
    # A marker commit is a normal gate-definer commit in every other
    # respect.
    base, established = _base(tmp_path)
    head = thresholds.normalise(
        _document(measured="80.0", floor="78.5"), False)
    moves = guard.forbidden_moves(base, head, established)
    assert any("coverage.python" in move for move in moves), moves


def test_the_gate_passes_through_with_no_budget_and_says_so(
        tmp_path, marker_tree, capsys):
    # The suite-cost gate on a re-seed commit: there is nothing to
    # compare the measurement against, and the gate must say THAT
    # rather than claim the run was within budget.
    document = tmp_path / "ci-thresholds.json"
    thresholds.write(document,
                     thresholds.normalise(_document(False), True))
    code = suite_report.check(
        _measurement_file(tmp_path / "m.json"), document)
    assert code == 0
    assert suite_report.NO_BUDGET_LINE in capsys.readouterr().out


def test_the_gate_still_refuses_a_counts_less_measurement_with_no_budget(
        tmp_path, marker_tree, capsys):
    # The pass-through is about the BUDGET, not about the measurement.
    # Telemetry is not a pass, whatever is (or is not) recorded.
    document = tmp_path / "ci-thresholds.json"
    thresholds.write(document,
                     thresholds.normalise(_document(False), True))
    measurement = _measurement_file(tmp_path / "m.json")
    payload = json.loads(measurement.read_text(encoding="utf-8"))
    payload["instrument"] = "process_time"
    for record in payload["phases"].values():
        record["million_instructions"] = None
    measurement.write_text(json.dumps(payload), encoding="utf-8")
    assert suite_report.check(measurement, document) == 1
    assert "no instruction count" in capsys.readouterr().err


def test_the_gate_still_fails_an_over_budget_phase_when_budgets_exist(
        tmp_path, plain_tree):
    # Removing the pass-through cannot smuggle in a disabled gate: with
    # a family recorded, the gate fails exactly as it always did.
    document = tmp_path / "ci-thresholds.json"
    thresholds.write(document, thresholds.normalise(_document(), False), False)
    over = {phase: str(Decimal(value) + Decimal("40.0"))
            for phase, value in COUNTS.items()}
    code = suite_report.check(
        _measurement_file(tmp_path / "m.json", counts=over), document)
    assert code == 1


def test_the_step_summary_does_not_claim_a_pass_it_did_not_get(
        tmp_path, marker_tree):
    # 'within budget' on a re-seed commit is a false statement in the
    # run's own summary.
    document = tmp_path / "ci-thresholds.json"
    thresholds.write(document,
                     thresholds.normalise(_document(False), True))
    assert 'within budget' not in suite_report.gate_verdict(document, 0)
    assert suite_report.NO_BUDGET_LINE in suite_report.gate_verdict(
        document, 0)
    thresholds.write(document, thresholds.normalise(_document(), False), False)
    assert suite_report.gate_verdict(document, 0) == '**within budget**'
    assert suite_report.gate_verdict(document, 1) == '**OVER BUDGET**'


def test_tighten_with_no_budget_is_a_declared_no_op(
        tmp_path, marker_tree, capsys):
    # The master-push bot runs this on every push, including the tip
    # that carries the marker. Tightening an absent family is nothing
    # to do, not a crash.
    document = tmp_path / "ci-thresholds.json"
    thresholds.write(document,
                     thresholds.normalise(_document(False), True))
    measurement = _measurement_file(tmp_path / "m.json")
    before = document.read_text(encoding="utf-8")
    assert suite_ratchet.main([
        "--tighten", str(measurement), "--thresholds", str(document)]) == 0
    assert document.read_text(encoding="utf-8") == before
    assert "nothing to tighten" in capsys.readouterr().out


def test_the_seed_refuses_a_family_that_is_still_recorded():
    # The seed is the SECOND commit and lands on the document the first
    # one emptied. Overwriting a recorded budget stays refused, marker
    # or no marker: that is a hand-raise's doorway.
    with pytest.raises(ValueError, match="already recorded"):
        suite_ratchet.seed(thresholds.normalise(_document(), False),
                           _counts())


def test_the_seed_writes_the_family_onto_an_emptied_document(tmp_path):
    # The landing half of the sequence, on the exact document the
    # marker made legal.
    data = _document(suite_cost=False)
    seeded = suite_ratchet.seed(data, _counts())
    assert seeded[thresholds.SUITE_COST_FAMILY]["run"] == {
        "measured": Decimal("339.6"), "floor": Decimal("341.1")}
    assert thresholds.normalise(seeded, False)[
        thresholds.SUITE_COST_FAMILY] == seeded[
            thresholds.SUITE_COST_FAMILY]
