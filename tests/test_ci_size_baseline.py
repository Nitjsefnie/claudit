"""Tests for the module-size baseline ratchet.

A recorded number is never raised by hand and no entry is ever added by
hand: growth is fixed by moving code into a new module. The cases below
pin the ceiling rule, the four violation classes, the tighten direction
(only ever down or gone) and the committed document's agreement with the
actual tree.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "ci"))

REPO_ROOT = Path(__file__).resolve().parents[1]
THRESHOLDS_PATH = REPO_ROOT / ".github" / "ci-thresholds.json"


def _load(name):
    """Import a scripts/ci module by path.

    scripts/ci is not a package and deliberately has no __init__.py — it
    holds standalone CI entry points, not an importable library. The
    directory itself goes on sys.path first, so the module's own
    ``importlib`` import of its sibling resolves.
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


def _size_baseline():
    return _load("size_baseline")


def _document(baseline=None):
    return {
        "schema_version": 1,
        "coverage": {
            "python": {
                "measured": 92.6,
                "floor": 91.1,
            },
        },
        "module_size_baseline": baseline or {},
    }


def _written(tmp_path, baseline=None):
    thresholds = _thresholds()
    target = tmp_path / "ci-thresholds.json"
    doc = _document(baseline=baseline)
    thresholds.write(target, doc)
    return target


def test_ceiling_rule():
    size_baseline = _size_baseline()
    assert size_baseline.ceiling_for("tests/test_x.py") == 700
    assert size_baseline.ceiling_for("backend/x.py") == 500
    assert size_baseline.ceiling_for("scripts/ci/x.py") == 500


def test_violation_classes():
    size_baseline = _size_baseline()
    sizes = {
        "backend/grown.py": 550,
        "backend/over.py": 601,
        "tests/graduated.py": 400,
    }
    baseline = {
        "backend/grown.py": 500,
        "scripts/ci/gone.py": 480,
        "tests/graduated.py": 900,
    }
    found = size_baseline.violations(sizes, baseline)
    assert found["grown"] == {"backend/grown.py": (550, 500)}
    assert found["over"] == {"backend/over.py": (601, 500)}
    assert found["missing"] == ["scripts/ci/gone.py"]
    assert found["graduated"] == {"tests/graduated.py": (400, 700)}


def test_violations_clean_tree_has_no_entries():
    size_baseline = _size_baseline()
    sizes = {"backend/small.py": 499, "tests/small.py": 699}
    assert not any(size_baseline.violations(sizes, {}).values())


def test_tightened_lowers_shrunk_entry():
    size_baseline = _size_baseline()
    updated = size_baseline.tightened(
        {"backend/grown.py": 900}, {"backend/grown.py": 750})
    assert updated == {"backend/grown.py": 750}


def test_tightened_drops_graduated_entry():
    size_baseline = _size_baseline()
    updated = size_baseline.tightened(
        {"tests/graduated.py": 900}, {"tests/graduated.py": 400})
    assert updated == {}


def test_tightened_keeps_growth_untouched():
    size_baseline = _size_baseline()
    updated = size_baseline.tightened(
        {"backend/grown.py": 500}, {"backend/grown.py": 550})
    assert updated is None


def test_tightened_noop_is_none():
    size_baseline = _size_baseline()
    updated = size_baseline.tightened(
        {"backend/same.py": 900}, {"backend/same.py": 900})
    assert updated is None


def test_main_check_clean(tmp_path, capsys, monkeypatch):
    size_baseline = _size_baseline()
    target = _written(tmp_path)
    monkeypatch.setattr(size_baseline, "tracked_sizes",
                        lambda: {"backend/small.py": 400})
    assert size_baseline.main(["--thresholds", str(target)]) == 0
    out = capsys.readouterr().out
    assert "within the size policy" in out


def test_main_check_fails_on_growth(tmp_path, capsys, monkeypatch):
    size_baseline = _size_baseline()
    target = _written(tmp_path, baseline={"backend/api.py": 500})
    monkeypatch.setattr(size_baseline, "tracked_sizes",
                        lambda: {"backend/api.py": 550})
    assert size_baseline.main(["--thresholds", str(target)]) == 1
    captured = capsys.readouterr()
    assert "grown" in captured.err
    assert "never raised by hand" in captured.err


def test_main_tighten_rewrites(tmp_path, capsys, monkeypatch):
    thresholds = _thresholds()
    size_baseline = _size_baseline()
    target = _written(tmp_path, baseline={
        "backend/shrunk.py": 900,
        "backend/graduated.py": 900,
    })
    monkeypatch.setattr(size_baseline, "tracked_sizes", lambda: {
        "backend/shrunk.py": 750,
        "backend/graduated.py": 400,
    })
    assert size_baseline.main(
        ["--tighten", "--thresholds", str(target)]) == 0
    doc = thresholds.load(target)
    baseline = doc["module_size_baseline"]
    assert baseline == {"backend/shrunk.py": 750}


def test_main_tighten_noop_explains(tmp_path, capsys, monkeypatch):
    size_baseline = _size_baseline()
    target = _written(tmp_path, baseline={"backend/api.py": 984})
    monkeypatch.setattr(size_baseline, "tracked_sizes",
                        lambda: {"backend/api.py": 984})
    before = target.read_text(encoding="utf-8")
    assert size_baseline.main(
        ["--tighten", "--thresholds", str(target)]) == 0
    assert target.read_text(encoding="utf-8") == before
    assert "no module shrank" in capsys.readouterr().out


def test_tracked_sizes_scope_and_counts():
    size_baseline = _size_baseline()
    sizes = size_baseline.tracked_sizes()
    assert sizes, "tracked_sizes found no modules"
    for rel in sizes:
        assert rel.startswith(("backend/", "scripts/", "tests/"))
    known = REPO_ROOT / "backend" / "api.py"
    assert sizes["backend/api.py"] == len(
        known.read_text(encoding="utf-8").splitlines())


def test_committed_document_matches_tree():
    thresholds = _thresholds()
    size_baseline = _size_baseline()
    doc = thresholds.load(THRESHOLDS_PATH)
    baseline = doc["module_size_baseline"]
    sizes = size_baseline.tracked_sizes()
    for rel, recorded in baseline.items():
        assert sizes[rel] == recorded, f"stale entry for {rel}"
    found = size_baseline.violations(sizes, baseline)
    assert not found["grown"], found["grown"]
    assert not found["over"], found["over"]
    assert not found["missing"], found["missing"]
    assert not found["graduated"], found["graduated"]


def test_committed_baseline_seeds_only_over_ceiling_files():
    size_baseline = _size_baseline()
    thresholds = _thresholds()
    doc = thresholds.load(THRESHOLDS_PATH)
    for rel, recorded in doc["module_size_baseline"].items():
        assert recorded > size_baseline.ceiling_for(rel)
