"""Tests for the CI thresholds direction guard.

For the ratchet data in.github/ci-thresholds.json, a coverage floor or
measured value never decreases, a baseline entry is never raised,
and an entry is added only to seed a
brand-new member or a brand-new measured family. Cost budget raises
require a feat: or feat(scope): subject in the judged range.
The cases cover forbidden moves and the bots' and features' allowed moves.
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "ci"))

REPO_ROOT = Path(__file__).resolve().parents[1]
THRESHOLDS_PATH = REPO_ROOT / ".github" / "ci-thresholds.json"

SUITE_COST = {
    "collection": {"measured": 12.5, "floor": 14.0},
    "run": {"measured": 304.9, "floor": 306.4},
    "residual": {"measured": 6.3, "floor": 7.8},
}


def _load(name):
    """Reuse each CI module and its import-time tables across cases."""
    return importlib.import_module(name)


@cache
def _ci_modules():
    thresholds = _load("thresholds")
    size_baseline = _load("size_baseline")
    guard = _load("thresholds_guard")
    return thresholds, size_baseline, guard


def _thresholds():
    return _ci_modules()[0]


def _guard():
    return _ci_modules()[2]


@pytest.fixture(scope="module", autouse=True)
def _shared_guard_parser():
    """Build the real CLI grammar once; main still parses every scenario."""
    guard = _guard()
    parser = guard._parser()  # pylint: disable=protected-access
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(guard, "_parser", lambda: parser)
        yield


@pytest.fixture(scope="module", name="committed_thresholds",
                params=["committed", "absent"])
def _committed_thresholds(request):
    if request.param == "absent":
        return _thresholds().normalise({"schema_version": 1})
    return _thresholds().load(THRESHOLDS_PATH)


GAP = Decimal("1.5")


def _reparse(shares=None):
    """A synthetic reparse family: both metrics for every bench phase.

    Spelled as floats, like every other family in this module's
    ``_document``: the guard tests serialise a base with plain
    ``json.dumps`` (the base is trusted bytes that skip the loader), and
    a Decimal there is not serialisable.
    """
    shares = shares or {}
    schema = _thresholds()
    return {
        phase: {
            metric: {
                "measured": float(
                    shares.get(f"{phase}.{metric}", "10.0")),
                "floor": float(
                    shares.get(f"{phase}.{metric}", "10.0")) + float(GAP),
            }
            for metric in schema.REPARSE_METRICS
        }
        for phase in schema.REPARSE_PHASES
    }


@cache
def _document_bytes():
    """An immutable synthetic seed; every caller gets independent objects."""
    return pickle.dumps({
        "schema_version": 1,
        "coverage": {
            "python": {"measured": 92.6, "floor": 91.1},
            "javascript": {"measured": 50.0, "floor": 48.5},
        },
        "reparse": _reparse(),
        "module_size_baseline": {},
        "comment_share_baseline": {},
        "pylint_suppression_baseline": {},
        "suite_cost": SUITE_COST,
    }, protocol=5)


def _document(baseline=None, suppression=None, reparse=None, suite=None):
    doc = pickle.loads(_document_bytes())
    for family, value in [("module_size_baseline", baseline),
                          ("pylint_suppression_baseline", suppression),
                          ("reparse", reparse)]:
        if value is not None:
            doc[family] = value
    if suite is not None:
        doc["suite_cost"] = pickle.loads(pickle.dumps(suite, protocol=5))
    return doc


def _written(tmp_path, doc, name):
    target = tmp_path / name
    target.write_text(json.dumps(doc, default=float), encoding="utf-8")
    return target


def _guard_result(tmp_path, base, head, tracked=None):
    guard = _guard()
    with pytest.MonkeyPatch.context() as patch:
        if tracked is not None:
            patch.setattr(guard.size_baseline, "tracked_sizes", lambda: tracked)
        base_path = _written(tmp_path, base, "base.json")
        head_path = _written(tmp_path, head, "head.json")
        return guard.main([
            "--base", str(base_path), "--head", str(head_path)])


def test_committed_document_against_itself_is_clean(tmp_path, committed_thresholds):
    git_meta.require_own_git_metadata(REPO_ROOT)
    committed = pickle.loads(pickle.dumps(committed_thresholds, protocol=5))
    assert _guard_result(
        tmp_path, committed, pickle.loads(pickle.dumps(committed, protocol=5)),
        tracked={"backend/api.py": 986}) == 0


def test_coverage_lowered_fails(tmp_path):
    # The loader pins floor = measured - 1.5, so a lowering always moves
    # both; the guard refuses the move either way.
    base = _document()
    head = _document()
    head["coverage"]["python"] = {"measured": 90.0, "floor": 88.5}
    assert _guard_result(tmp_path, base, head) == 1


def test_removed_coverage_entry_fails(tmp_path, capsys):
    for missing in [
        "python",
        "javascript",
        "coverage",
    ]:
        base = _document()
        head = _document()
        if missing == "coverage":
            del head["coverage"]
        else:
            del head["coverage"][missing]

        assert _guard_result(tmp_path, base, head) == 1
        assert "removed without a reason: coverage." in capsys.readouterr().err


def test_coverage_raised_is_clean(tmp_path):
    base = _document()
    head = _document()
    head["coverage"]["python"]["measured"] = 97.3
    head["coverage"]["python"]["floor"] = 95.8
    assert _guard_result(tmp_path, base, head) == 0


def test_size_entry_raised_fails(tmp_path):
    base = _document(baseline={"backend/api.py": 986})
    head = _document(baseline={"backend/api.py": 1486})
    assert _guard_result(tmp_path, base, head) == 1


def test_core_family_entry_added_fails(tmp_path):
    # The audit's exploit shape: a new 900-line backend file seeds itself
    # a 900-line entry. The original families are frozen at the guard's
    # landing — an addition under them is a hand-add and always fails.
    # The added path is IN the measured set on purpose: the core rule
    # must be the only thing that can go red here — a fixture missing
    # the tree would let the "added, unmeasured" backstop mask its
    # deletion green.
    base = _document(baseline={"backend/api.py": 986})
    head = _document(
        baseline={"backend/api.py": 986, "backend/audit_new_big.py": 900})
    assert _guard_result(
        tmp_path, base, head,
        tracked={"backend/api.py": 986,
                 "backend/audit_new_big.py": 900}) == 1


def test_non_core_entry_added_outside_the_measured_set_fails(tmp_path):
    # An entry for a path no ratchet measures is inert — refuse it too.
    base = _document(baseline={})
    head = _document(baseline={"public/index.html": 900})
    assert _guard_result(
        tmp_path, base, head,
        tracked={"src/parser.js": 1024}) == 1


def test_family_seed_for_a_new_measured_family_is_clean(tmp_path):
    # The sanctioned path: a new measured family seeds its entries
    # once, through the loader's writer, in a reviewed gate-definer PR.
    base = _document(baseline={"backend/api.py": 986})
    head = _document(
        baseline={"backend/api.py": 986, "backend/schema.sql": 834})
    assert _guard_result(
        tmp_path, base, head,
        tracked={"backend/api.py": 986, "backend/schema.sql": 834}) == 0


def test_new_member_seeded_is_clean(tmp_path):
    # A brand-new baseline member seeds its entries in the PR that
    # introduces it; the committed-document-matches-tree tests
    # pin each seed truthful. The base predates the member, so its
    # bytes are placed directly — the writer refuses a memberless doc.
    base = _document(baseline={})
    base.pop("pylint_suppression_baseline")
    head = _document(
        baseline={},
        suppression={"backend/ingest_fetch.py": 5})
    guard = _guard()
    base_path = tmp_path / "base.json"
    base_path.write_text(json.dumps(base, default=float), encoding="utf-8")
    head_path = _written(tmp_path, head, "head.json")
    assert guard.main([
        "--base", str(base_path), "--head", str(head_path)]) == 0


def test_suppression_entry_added_after_landing_fails(tmp_path):
    # Once the member exists in the base, a new suppression site is a
    # hand-add under a frozen family: reduce the complexity instead.
    # The added path sits in the measured set so the frozen-core rule,
    # not the unmeasured backstop, is what fails this case.
    base = _document(suppression={"backend/ingest.py": 2})
    head = _document(
        suppression={"backend/ingest.py": 2, "backend/new.py": 1})
    assert _guard_result(
        tmp_path, base, head,
        tracked={"backend/ingest.py": 675, "backend/new.py": 100}) == 1


def test_suppression_entry_raised_fails(tmp_path):
    base = _document(suppression={"backend/ingest.py": 2})
    head = _document(suppression={"backend/ingest.py": 3})
    assert _guard_result(tmp_path, base, head) == 1


def test_bot_shaped_raise_is_clean(tmp_path):
    # The load-bearing compatibility constraint: a synthetic bot commit —
    # ratchet.py's raise, measured and floor moving up together — must
    # keep passing the guard. A complete synthetic document supplies the
    # calibrations independently of which languages are recorded today.
    base = _document()
    head = pickle.loads(pickle.dumps(base, protocol=5))
    for lang in ("python", "javascript"):
        measured = min(
            round(float(base["coverage"][lang]["measured"]) + 0.5, 1),
            100.0)
        # A raise must stay inside the loader's domain (values above 100.0
        # are refused) and stay a raise: the assertion rejects a vacuous
        # no-op if the synthetic measurement reaches the ceiling.
        assert measured > float(base["coverage"][lang]["measured"])
        head["coverage"][lang] = {
            "measured": measured,
            "floor": round(measured - float(GAP), 1),
        }
    assert _guard_result(tmp_path, base, head) == 0


def test_bot_shaped_tighten_is_clean(tmp_path):
    # The other bot shape: --tighten follows shrunk files down and drops
    # a graduated entry; the suppression tighten does the same for counts.
    # Synthetic entries keep these moves exercised when the committed
    # families have no entries or are absent entirely.
    base = _document(
        baseline={"backend/shrunk.py": 900, "tests/graduated.py": 900},
        suppression={"backend/shrunk.py": 2})
    head = pickle.loads(pickle.dumps(base, protocol=5))
    head["module_size_baseline"]["backend/shrunk.py"] -= 1
    del head["module_size_baseline"]["tests/graduated.py"]
    head["pylint_suppression_baseline"]["backend/shrunk.py"] -= 1
    assert _guard_result(tmp_path, base, head) == 0


def test_entry_lowered_is_clean(tmp_path):
    base = _document(baseline={"backend/api.py": 986, "src/app.jsx": 1424})
    head = _document(baseline={"backend/api.py": 950, "src/app.jsx": 1424})
    assert _guard_result(tmp_path, base, head) == 0


def test_empty_family_removed_is_clean(tmp_path):
    # An empty family carries no entries to remove.
    base = _document(baseline={"backend/api.py": 986})
    head = _document(baseline={"backend/api.py": 986})
    head.pop("pylint_suppression_baseline")
    base_path = _written(tmp_path, base, "base.json")
    head_path = tmp_path / "head.json"
    head_path.write_text(
        json.dumps(head, indent=2, default=float), encoding="utf-8")
    assert _guard().main([
        "--base", str(base_path), "--head", str(head_path)]) == 0


def test_garbage_head_document_fails(tmp_path):
    base_path = _written(tmp_path, _document(), "base.json")
    head_path = tmp_path / "head.json"
    head_path.write_text("{not json", encoding="utf-8")
    assert _guard().main([
        "--base", str(base_path), "--head", str(head_path)]) == 1


def test_main_prints_each_forbidden_move(tmp_path, capsys):
    base = _document(baseline={"backend/api.py": 986})
    head = _document(baseline={"backend/api.py": 1486})
    head["coverage"]["python"] = {"measured": 90.0, "floor": 88.5}
    assert _guard_result(tmp_path, base, head) == 1
    captured = capsys.readouterr()
    assert "coverage.python.floor" in captured.err
    assert "coverage.python.measured" in captured.err
    assert "module_size_baseline.backend/api.py" in captured.err
    assert "never lowered by hand" in captured.err
    assert "never raised or added by hand" in captured.err


def test_suite_cost_tightened_is_clean(tmp_path):
    for family, path, measured, floor in [
        ("suite_cost", ("run",), 300.0, 301.5),
        ("reparse", ("sniff", "share"), 4.0, 5.5),
        ("reparse", ("parse_body", "bytecodes"), 4.0, 5.5),
        ("reparse", ("parse_body", "share"), 4.0, 5.5),
    ]:
        # The bot shape: a cheaper run moves both fields of one phase down
        # and leaves the other phases exactly where they were.
        base = _document()
        head = _document()
        parent = head[family]
        for key in path[:-1]:
            parent = parent[key]
        parent[path[-1]] = {"measured": measured, "floor": floor}
        assert _guard_result(tmp_path, base, head) == 0


def test_suite_cost_introduced_against_a_predating_base_is_clean(tmp_path):
    # A base predating the family carries no suite_cost record: the
    # change introducing one is its seed, not a move (the shape).
    base = _document()
    del base["suite_cost"]
    base_path = tmp_path / "base.json"
    base_path.write_text(json.dumps(base), encoding="utf-8")
    head_path = _written(tmp_path, _document(), "head.json")
    assert _guard().main([
        "--base", str(base_path), "--head", str(head_path)]) == 0


def test_reparse_instruction_count_raised_fails(tmp_path):
    # The count is the instrument that catches a uniform slowdown, so a
    # count budget raised without judged features would buy away exactly
    # the regression the bench exists for.
    base = _document()
    head = _document(reparse=_reparse({"parse_body.bytecodes": "60.0"}))
    assert _guard_result(tmp_path, base, head) == 1


def test_reparse_family_unchanged_is_clean(tmp_path):
    base = _document()
    assert _guard_result(tmp_path, base, _document()) == 0


def test_reparse_family_seed_is_clean(tmp_path):
    for overrides in [
        {},
        {"sniff.share": "60.0", "parse_body.share": "30.0"},
    ]:
        # The one-time seed: a base that predates the family carries no
        # record, so the change that introduces one is not a move at all.
        # Place the base's bytes directly to model a document predating the
        # reparse family.
        base = _document()
        base.pop("reparse")
        base_path = tmp_path / "base.json"
        base_path.write_text(json.dumps(base, default=float), encoding="utf-8")
        head_path = _written(
            tmp_path, _document(reparse=_reparse(overrides)), "head.json")
        assert _guard().main([
            "--base", str(base_path), "--head", str(head_path)]) == 0


def test_suite_cost_added_to_established_base_fails(tmp_path):
    # Once the base carries the family, a phase appearing in the head
    # that the base lacks is not a seed — but the loader refuses an
    # unknown phase outright, so this shape goes red at load.
    base = _document()
    head = _document()
    head["suite_cost"]["warmup"] = {"measured": 1.0, "floor": 2.5}
    base_path = _written(tmp_path, base, "base.json")
    head_path = tmp_path / "head.json"
    head_path.write_text(json.dumps(head, indent=2), encoding="utf-8")
    assert _guard().main([
        "--base", str(base_path), "--head", str(head_path)]) == 1


def test_main_prints_the_suite_cost_remedy(tmp_path, capsys):
    base = _document()
    head = _document()
    head["suite_cost"]["run"] = {"measured": 310.0, "floor": 311.5}
    assert _guard_result(tmp_path, base, head) == 1
    captured = capsys.readouterr()
    assert "suite_cost.run.measured" in captured.err
    assert "suite cost budget is never raised by hand" in captured.err


def test_guard_rejects_a_decimal_spelled_head_record(tmp_path):
    # The head is loaded through the loader (strict bytes); the base is
    # trusted, but its numbers still arrive as Decimals: a suite record
    # spelled with two decimal places in the base is refused by
    # normalise, which is what names the path.
    base = _document()
    base["suite_cost"]["run"] = {"measured": "304.90", "floor": "306.4"}
    head_path = _written(tmp_path, _document(), "head.json")
    base_path = tmp_path / "base.json"
    base_path.write_text(json.dumps(base), encoding="utf-8")
    assert _guard().main([
        "--base", str(base_path), "--head", str(head_path)]) == 1


def test_reparse_remedy_is_printed_for_a_reparse_move(tmp_path, capsys):
    base = _document()
    head = _document(reparse=_reparse({"sniff.share": "20.0"}))
    assert _guard_result(tmp_path, base, head) == 1
    err = capsys.readouterr().err
    assert "reparse.sniff.share.measured" in err
    assert "never raised by hand" in err


def _with_identity(suite, lines):
    family = pickle.loads(pickle.dumps(suite, protocol=5))
    family["tests_tree_lines"] = lines
    return family


def test_suite_cost_identity_moved_fails(tmp_path):
    for base_identity, head_identity in [
        (40000, 40250),
        (None, 40000),
        (40000, None),
    ]:
        # The workload identity is the seed's binding to the workload it
        # measured (issue #524). On an established family, a moved
        # identity is a hand-edit and fails.
        base = (_document() if base_identity is None else
                _document(suite=_with_identity(SUITE_COST, base_identity)))
        head = (_document() if head_identity is None else
                _document(suite=_with_identity(SUITE_COST, head_identity)))
        assert _guard_result(tmp_path, base, head) == 1


def test_new_family_seed_carrying_the_identity_is_clean(tmp_path):
    # The base predates the family, so the family and its identity
    # arrive together and are not a move at all.
    base = _document()
    base.pop("suite_cost")
    head = _document(suite=_with_identity(SUITE_COST, 40000))
    # The base is family-absent; its bytes are placed directly, as
    # test_new_member_seeded_is_clean does.
    base_path = tmp_path / "base.json"
    base_path.write_text(json.dumps(base, default=float), encoding="utf-8")
    head_path = _written(tmp_path, head, "head.json")
    assert _guard().main([
        "--base", str(base_path), "--head", str(head_path)]) == 0


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True,
        check=True, timeout=10).stdout.decode().strip()


def _init_judged_tree(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Guard Test")
    _git(root, "config", "user.email", "guard@example.invalid")
    (root / "backend").mkdir()
    for path in ("backend/kept.py", "backend/second.py"):
        (root / path).write_text("pass\n", encoding="utf-8")
        _git(root, "add", "--", path)
    _git(root, "commit", "--allow-empty", "-qm", "feat: outside judged range")
    _git(root, "branch", "pristine")


@pytest.fixture(scope="module", name="judged_base_path")
def _judged_base_path(tmp_path_factory):
    """One scratch tree per module; the helper restores its saved commit."""
    path = tmp_path_factory.mktemp("thresholds-judged")
    _init_judged_tree(path)
    return path


def _judged_guard(tmp_path, monkeypatch, base, head, subjects, revs=None):
    """Reuse the tracked tree, judging only each scenario's new commits."""
    root = tmp_path / "repo"
    if not root.exists():
        _init_judged_tree(tmp_path)
    # Only this disposable fixture tree is reset; its saved commit is the rollback.
    _git(root, "reset", "--hard", "pristine")
    _git(root, "clean", "-fd")
    # Prior scenarios' features and declarations must stay outside this range.
    base_rev = _git(root, "rev-parse", "HEAD")
    for subject in subjects:
        _git(root, "commit", "--allow-empty", "--cleanup=verbatim", "-qm", subject)
    guard = _guard()
    monkeypatch.setattr(guard, "__file__", str(root / "scripts/ci/thresholds_guard.py"))
    args = ["--base", str(_written(tmp_path, base, "base.json")),
            "--head", str(_written(tmp_path, head, "head.json"))]
    args += ["--base-rev", base_rev] if revs is None else revs
    return guard.main(args)


def _raised_document(family="suite_cost", metric="bytecodes"):
    head = _document()
    if family == "suite_cost":
        head[family]["run"] = {"measured": 310.0, "floor": 311.5}
    else:
        head[family]["parse_body"][metric] = {"measured": 20.0, "floor": 21.5}
    return head


def test_feature_range_admits_budget_raise(judged_base_path, monkeypatch):
    for family, metric, subjects in [
        ("suite_cost", "bytecodes", ["feat: add a panel", "fix: polish"]),
        ("reparse", "bytecodes", ["fix: prepare", "feat: parse new events"]),
        ("reparse", "share", ["feat: parse new events"]),
        ("suite_cost", "bytecodes", ["feat(ci): measure a new path"]),
    ]:
        assert _judged_guard(
            judged_base_path, monkeypatch, _document(), _raised_document(family, metric),
            subjects) == 0
        root = judged_base_path / "repo"
        assert (root / "backend/kept.py").read_text(encoding="utf-8") == "pass\n"
        assert not (root / "leaked.txt").exists()
        assert _git(root, "rev-list", "--count", "pristine..HEAD") == str(len(subjects))
        (root / "backend/kept.py").write_text("dirty\n", encoding="utf-8")
        (root / "leaked.txt").write_text("untracked\n", encoding="utf-8")


def test_non_feature_range_refuses_raise_and_names_remedy(judged_base_path, monkeypatch, capsys):
    for subject in [
        "fix: trim work",
        "chore: tidy",
        "perf: optimise",
        "feature: new path",
        "Feat: new path",
        "feat new path",
        " feat: new path",
        "fix: keep one subject\u2028feat: embedded feature",
        "fix: keep one subject\x0bfeat: embedded feature",
    ]:
        assert _judged_guard(
            judged_base_path, monkeypatch, _document(), _raised_document(), [subject]) == 1
        root = judged_base_path / "repo"
        assert (root / "backend/kept.py").read_text(encoding="utf-8") == "pass\n"
        assert not (root / "leaked.txt").exists()
        assert _git(root, "rev-list", "--count", "pristine..HEAD") == "1"
        err = capsys.readouterr().err
        assert "trimming its own tests" in err
        assert "dropping redundant cases" in err
        assert "merging parametrisations" in err
        assert "deleting tests that repeat another test's assertions" in err


def test_unproved_feature_range_refuses_raise(judged_base_path, monkeypatch, capsys):
    for revs in [
        [],
        ["--base-rev", "unknown-revision", "--head-rev", "HEAD"],
        ["--base-rev", "HEAD", "--head-rev", "HEAD"],
    ]:
        assert _judged_guard(
            judged_base_path, monkeypatch, _document(), _raised_document(),
            ["feat: add a panel"], revs) == 1
        assert "trimming its own tests" in capsys.readouterr().err


def test_feature_raise_keeps_other_refusals(judged_base_path, monkeypatch, capsys):
    for other_move in [
        "coverage",
        "identity",
        "baseline",
    ]:
        base = _document()
        head = _raised_document()
        if other_move == "coverage":
            head["coverage"]["python"] = {"measured": 90.0, "floor": 88.5}
            forbidden = "coverage.python.floor"
        elif other_move == "identity":
            base["suite_cost"]["tests_tree_lines"] = 40000
            head["suite_cost"]["tests_tree_lines"] = 40250
            forbidden = "suite_cost.tests_tree_lines"
        else:
            base["module_size_baseline"]["backend/api.py"] = 986
            head["module_size_baseline"]["backend/api.py"] = 1486
            forbidden = "module_size_baseline.backend/api.py"
        assert _judged_guard(
            judged_base_path, monkeypatch, base, head, ["feat: add a panel"],
            ["--base-rev", "HEAD^", "--head-rev", "HEAD"]) == 1
        err = capsys.readouterr().err
        assert f"forbidden move: {forbidden}" in err
        assert "forbidden move: suite_cost.run." not in err
