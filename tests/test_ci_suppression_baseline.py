"""Tests for the inline pylint complexity-suppression baseline ratchet.

Production Python gains a second only-shrinks baseline: the per-file
count of inline ``pylint: disable``/``disable-next`` comments whose check
list names a complexity check (any ``too-many-…``), under ``backend/``
and ``scripts/`` — ``tests/`` stays outside (issue #389). The cases pin
the counted shape, the four violation classes, the tighten direction
(only ever down or gone) and the committed document's agreement with the
actual tree.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from tests import git_meta

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "ci"))

REPO_ROOT = Path(__file__).resolve().parents[1]
THRESHOLDS_PATH = REPO_ROOT / ".github" / "ci-thresholds.json"


def _load(name):
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


def _thresholds():
    return _load("thresholds")


def _suppression_baseline():
    return _load("suppression_baseline")


def _document(baseline=None):
    return {
        "schema_version": 1,
        "coverage": {
            "python": {
                "measured": 92.6,
                "floor": 91.1,
            },
            "javascript": {
                "measured": 50.0,
                "floor": 48.5,
            },
        },
        "module_size_baseline": {},
        "pylint_suppression_baseline": baseline or {},
    }


def _written(tmp_path, baseline=None):
    thresholds = _thresholds()
    target = tmp_path / "ci-thresholds.json"
    thresholds.write(target, _document(baseline=baseline))
    return target


def test_counts_disable_and_disable_next():
    suppression = _suppression_baseline()
    assert suppression.suppression_lines(
        "# pylint: disable=too-many-locals\n") == 1
    assert suppression.suppression_lines(
        "# pylint: disable-next=too-many-statements\n") == 1


def test_counts_a_complexity_check_in_a_longer_list():
    suppression = _suppression_baseline()
    assert suppression.suppression_lines(
        "def f():  # pylint: disable=unused-argument,too-many-locals\n"
    ) == 1


def test_counts_each_suppression_line_once():
    suppression = _suppression_baseline()
    assert suppression.suppression_lines(
        "# pylint: disable=too-many-locals,too-many-branches,"
        "too-many-statements\n"
        "# pylint: disable=too-many-arguments\n") == 2


def test_non_complexity_disables_not_counted():
    suppression = _suppression_baseline()
    assert suppression.suppression_lines(
        "# pylint: disable=line-too-long\n"
        "# pylint: disable-next=too-few-public-methods\n"
        "# pylint: disable=too-few-public-methods\n") == 0


def test_too_few_is_not_too_many():
    suppression = _suppression_baseline()
    assert suppression.suppression_lines(
        "# pylint: disable=too-few-public-methods\n") == 0


def test_plain_text_is_not_a_suppression():
    suppression = _suppression_baseline()
    assert suppression.suppression_lines(
        "The pylint: disable=too-many-locals comment above this text\n"
        "mentioning disable-next=too-many-branches inline\n") == 0


def test_plain_text_after_the_comment_marker_is_not_counted():
    suppression = _suppression_baseline()
    assert suppression.suppression_lines(
        "# the pylint: disable=too-many-locals rule below\n") == 0


def test_scope_is_production_python(tmp_path):
    # backend/ and scripts/ Python is counted; tests/ Python is not, and
    # nor is any non-Python file.
    git_meta.require_own_git_metadata(REPO_ROOT)
    suppression = _suppression_baseline()
    counts = suppression.tracked_suppression_counts()
    assert "backend/ingest.py" in counts
    for rel in counts:
        assert rel.startswith(("backend/", "scripts/"))
        assert rel.endswith(".py")
        assert not rel.startswith("tests/")
    assert "tests/test_ci_suppression_baseline.py" not in counts


def test_violation_classes():
    suppression = _suppression_baseline()
    counts = {
        "backend/grown.py": 3,
        "backend/over.py": 2,
        "backend/clean.py": 0,
    }
    baseline = {
        "backend/grown.py": 2,
        "scripts/ci/gone.py": 1,
        "backend/graduated.py": 4,
    }
    counts["backend/graduated.py"] = 0
    found = suppression.violations(counts, baseline)
    assert found["grown"] == {"backend/grown.py": (3, 2)}
    assert found["over"] == {"backend/over.py": (2, 1)}
    assert found["missing"] == ["scripts/ci/gone.py"]
    assert found["graduated"] == {"backend/graduated.py": 0}


def test_violations_clean_tree_needs_no_entries():
    suppression = _suppression_baseline()
    assert not any(suppression.violations(
        {"backend/small.py": 0}, {}).values())


def test_tightened_lowers_shrunk_entry():
    suppression = _suppression_baseline()
    updated = suppression.tightened(
        {"backend/grown.py": 4}, {"backend/grown.py": 2})
    assert updated == {"backend/grown.py": 2}


def test_tightened_drops_clean_and_gone_entries():
    suppression = _suppression_baseline()
    updated = suppression.tightened(
        {"backend/clean.py": 2, "backend/gone.py": 1},
        {"backend/clean.py": 0})
    assert updated == {}


def test_tightened_keeps_growth_untouched():
    suppression = _suppression_baseline()
    updated = suppression.tightened(
        {"backend/grown.py": 1}, {"backend/grown.py": 3})
    assert updated is None


def test_main_check_clean(tmp_path, capsys, monkeypatch):
    suppression = _suppression_baseline()
    target = _written(tmp_path)
    monkeypatch.setattr(suppression, "tracked_suppression_counts",
                        lambda: {"backend/small.py": 0})
    assert suppression.main(["--thresholds", str(target)]) == 0
    assert "no complexity suppressions outside the baseline" in (
        capsys.readouterr().out)


def test_main_check_fails_on_growth(tmp_path, capsys, monkeypatch):
    suppression = _suppression_baseline()
    target = _written(tmp_path, baseline={"backend/api.py": 1})
    monkeypatch.setattr(suppression, "tracked_suppression_counts",
                        lambda: {"backend/api.py": 2})
    assert suppression.main(["--thresholds", str(target)]) == 1
    captured = capsys.readouterr()
    assert "grown" in captured.err
    assert "never raised or added by hand" in captured.err


def test_main_tighten_rewrites(tmp_path, capsys, monkeypatch):
    thresholds = _thresholds()
    suppression = _suppression_baseline()
    target = _written(tmp_path, baseline={
        "backend/shrunk.py": 4,
        "backend/graduated.py": 4,
    })
    monkeypatch.setattr(suppression, "tracked_suppression_counts", lambda: {
        "backend/shrunk.py": 2,
        "backend/graduated.py": 0,
    })
    assert suppression.main(
        ["--tighten", "--thresholds", str(target)]) == 0
    doc = thresholds.load(target)
    assert doc["pylint_suppression_baseline"] == {"backend/shrunk.py": 2}


def test_main_tighten_noop_explains(tmp_path, capsys, monkeypatch):
    suppression = _suppression_baseline()
    target = _written(tmp_path, baseline={"backend/api.py": 2})
    monkeypatch.setattr(suppression, "tracked_suppression_counts",
                        lambda: {"backend/api.py": 2})
    before = target.read_text(encoding="utf-8")
    assert suppression.main(
        ["--tighten", "--thresholds", str(target)]) == 0
    assert target.read_text(encoding="utf-8") == before
    assert "no file's count dropped" in capsys.readouterr().out


def test_committed_document_matches_tree():
    # The committed baseline records each production file's CURRENT
    # count: every entry equals the file's real suppression-line count,
    # and no file sits outside the baseline. This is the truth pin the
    # direction guard relies on for seeds.
    git_meta.require_own_git_metadata(REPO_ROOT)
    thresholds = _thresholds()
    suppression = _suppression_baseline()
    doc = thresholds.load(THRESHOLDS_PATH)
    baseline = doc["pylint_suppression_baseline"]
    counts = suppression.tracked_suppression_counts()
    for rel, recorded in baseline.items():
        assert counts.get(rel, 0) == recorded, f"stale entry for {rel}"
    found = suppression.violations(counts, baseline)
    assert not found["grown"], found["grown"]
    assert not found["over"], found["over"]
    assert not found["missing"], found["missing"]
    assert not found["graduated"], found["graduated"]
