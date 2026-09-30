"""Tests for the CI thresholds direction guard (issue #388).

The ratchet data in .github/ci-thresholds.json only ever moves the way
the bots move it: a coverage floor or measured value never decreases, a
baseline entry is never raised, and an entry is added only to seed a
brand-new member or a brand-new measured family. Every case below is a
move the audit showed passing today; each must go red (or, for the
allowed moves, stay green).
"""
from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path

from tests import git_meta

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


def _guard():
    return _load("thresholds_guard")


def _document(baseline=None, suppression=None):
    return {
        "schema_version": 1,
        "coverage": {
            "python": {"measured": 92.6, "floor": 91.1},
            "javascript": {"measured": 50.0, "floor": 48.5},
        },
        "module_size_baseline": baseline if baseline is not None else {},
        "pylint_suppression_baseline": (
            suppression if suppression is not None else {}),
    }


def _written(tmp_path, doc, name):
    target = tmp_path / name
    _thresholds().write(target, doc)
    return target


def _guard_result(tmp_path, base, head, tracked=None):
    guard = _guard()
    if tracked is not None:
        guard.size_baseline.tracked_sizes = lambda: tracked
    base_path = _written(tmp_path, base, "base.json")
    head_path = _written(tmp_path, head, "head.json")
    return guard.main([
        "--base", str(base_path), "--head", str(head_path)])


def test_committed_document_against_itself_is_clean(tmp_path):
    git_meta.require_own_git_metadata(REPO_ROOT)
    committed = _thresholds().load(THRESHOLDS_PATH)
    assert _guard_result(
        tmp_path, committed, copy.deepcopy(committed),
        tracked={"backend/api.py": 986}) == 0


def test_coverage_lowered_fails(tmp_path):
    # The loader pins floor = measured - 1.5, so a lowering always moves
    # both; the guard refuses the move either way.
    base = _document()
    head = _document()
    head["coverage"]["python"] = {"measured": 90.0, "floor": 88.5}
    assert _guard_result(tmp_path, base, head) == 1


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
    base = _document(baseline={"backend/api.py": 986})
    head = _document(
        baseline={"backend/api.py": 986, "backend/audit_new_big.py": 900})
    assert _guard_result(tmp_path, base, head) == 1


def test_non_core_entry_added_outside_the_measured_set_fails(tmp_path):
    # An entry for a path no ratchet measures is inert — refuse it too.
    base = _document(baseline={})
    head = _document(baseline={"public/index.html": 900})
    assert _guard_result(
        tmp_path, base, head,
        tracked={"src/parser.js": 1024}) == 1


def test_family_seed_for_a_new_measured_family_is_clean(tmp_path):
    # The sanctioned path (#393): a new measured family seeds its entries
    # once, through the loader's writer, in a reviewed gate-definer PR.
    base = _document(baseline={"backend/api.py": 986})
    head = _document(
        baseline={"backend/api.py": 986, "backend/schema.sql": 834})
    assert _guard_result(
        tmp_path, base, head,
        tracked={"backend/api.py": 986, "backend/schema.sql": 834}) == 0


def test_new_member_seeded_is_clean(tmp_path):
    # A brand-new baseline member seeds its entries in the PR that
    # introduces it (#389); the committed-document-matches-tree tests
    # pin each seed truthful. The base predates the member, so its
    # bytes are placed directly — the writer refuses a memberless doc.
    base = _document(baseline={})
    base.pop("pylint_suppression_baseline")
    head = _document(
        baseline={},
        suppression={"backend/ingest_fetch.py": 5})
    guard = _guard()
    base_path = tmp_path / "base.json"
    base_path.write_text(json.dumps(base), encoding="utf-8")
    head_path = _written(tmp_path, head, "head.json")
    assert guard.main([
        "--base", str(base_path), "--head", str(head_path)]) == 0


def test_suppression_entry_added_after_landing_fails(tmp_path):
    # Once the member exists in the base, a new suppression site is a
    # hand-add under a frozen family: reduce the complexity instead.
    base = _document(suppression={"backend/ingest.py": 2})
    head = _document(
        suppression={"backend/ingest.py": 2, "backend/new.py": 1})
    assert _guard_result(tmp_path, base, head) == 1


def test_suppression_entry_raised_fails(tmp_path):
    base = _document(suppression={"backend/ingest.py": 2})
    head = _document(suppression={"backend/ingest.py": 3})
    assert _guard_result(tmp_path, base, head) == 1


def test_bot_shaped_raise_is_clean(tmp_path):
    # The load-bearing compatibility constraint: a synthetic bot commit —
    # ratchet.py's raise, measured and floor moving up together — must
    # keep passing the guard.
    base = _thresholds().load(THRESHOLDS_PATH)
    head = copy.deepcopy(base)
    head["coverage"]["python"] = {"measured": 97.3, "floor": 95.8}
    head["coverage"]["javascript"] = {"measured": 91.7, "floor": 90.2}
    assert _guard_result(tmp_path, base, head) == 0


def test_bot_shaped_tighten_is_clean(tmp_path):
    # The other bot shape: --tighten follows shrunk files down and drops
    # a graduated entry; the suppression tighten does the same for counts.
    base = _thresholds().load(THRESHOLDS_PATH)
    head = copy.deepcopy(base)
    head["module_size_baseline"]["backend/ingest.py"] = 640
    del head["module_size_baseline"]["backend/parse_kimi.py"]
    head["pylint_suppression_baseline"]["backend/ingest_fetch.py"] = 3
    assert _guard_result(tmp_path, base, head) == 0


def test_entry_lowered_or_removed_is_clean(tmp_path):
    base = _document(baseline={"backend/api.py": 986, "src/app.jsx": 1424})
    head = _document(baseline={"backend/api.py": 950})
    assert _guard_result(tmp_path, base, head) == 0


def test_removed_member_fails(tmp_path):
    # The loader refuses a document missing a required member, so a
    # wholesale member deletion goes red before the guard compares;
    # write() would refuse the bytes, so hand-place them.
    base = _document(baseline={"backend/api.py": 986})
    head = _document(baseline={"backend/api.py": 986})
    head.pop("pylint_suppression_baseline")
    base_path = _written(tmp_path, base, "base.json")
    head_path = tmp_path / "head.json"
    head_path.write_text(
        json.dumps(head, indent=2), encoding="utf-8")
    assert _guard().main([
        "--base", str(base_path), "--head", str(head_path)]) == 1


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
