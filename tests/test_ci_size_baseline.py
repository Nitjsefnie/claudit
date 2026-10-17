"""Tests for the module-size baseline ratchet.

A recorded number is never raised by hand and no entry is ever added by
hand: growth is fixed by moving code into a new module. The cases below
pin the ceiling rule, the four reported statuses, the tighten direction
(only ever down or gone) and the committed document's shrink-tolerant
agreement with the actual tree.
"""
from __future__ import annotations

import importlib.util
import sys
from functools import cache
from pathlib import Path

import pytest

from tests import git_meta

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


@cache
def _ci_modules():
    """Load once at first use, with the same thresholds dependency."""
    thresholds = _load("thresholds")
    size_baseline = _load("size_baseline")
    return thresholds, size_baseline


def _thresholds():
    return _ci_modules()[0]


def _size_baseline():
    return _ci_modules()[1]


@pytest.fixture(scope="module", name="git_metadata")
def _git_metadata():
    git_meta.require_own_git_metadata(REPO_ROOT)


@pytest.fixture(scope="module", name="real_tracked_sizes")
def _real_tracked_sizes(git_metadata):
    # The main tests' function-scoped patches are restored before this runs.
    return _size_baseline().tracked_sizes()


def _document(baseline=None):
    schema = _thresholds()
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
        "reparse": {
            phase: {
                metric: {"measured": 10.0, "floor": 11.5}
                for metric in schema.REPARSE_METRICS
            }
            for phase in schema.REPARSE_PHASES
        },
        "module_size_baseline": baseline or {},
        "comment_share_baseline": {},
        "pylint_suppression_baseline": {},
        "suite_cost": {
            "collection": {"measured": 25.8, "floor": 27.3},
            "run": {"measured": 309.8, "floor": 311.3},
            "residual": {"measured": 6.5, "floor": 8.0},
        },
    }


def _written(tmp_path, baseline=None):
    thresholds = _thresholds()
    target = tmp_path / "ci-thresholds.json"
    doc = _document(baseline=baseline)
    thresholds.write(target, doc)
    return target


@pytest.fixture(params=["committed", "absent"], name="baseline_document")
def _baseline_document(request, tmp_path):
    thresholds = _thresholds()
    if request.param == "committed":
        return thresholds.load(THRESHOLDS_PATH)
    data = _document()
    del data["module_size_baseline"]
    target = tmp_path / "absent.json"
    thresholds.write(target, data)
    return thresholds.load(target)


def test_ceiling_rule():
    size_baseline = _size_baseline()
    assert size_baseline.ceiling_for("tests/test_x.py") == 700
    assert size_baseline.ceiling_for("backend/x.py") == 500
    assert size_baseline.ceiling_for("src/app.jsx") == 500
    assert size_baseline.ceiling_for("src/parser.js") == 500
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


@pytest.mark.parametrize("baseline,sizes,expected", [
    pytest.param({"backend/grown.py": 900}, {"backend/grown.py": 750},
                 {"backend/grown.py": 750}, id="shrunk"),
    pytest.param({"tests/graduated.py": 900}, {"tests/graduated.py": 400},
                 {}, id="graduated"),
    pytest.param({"backend/gone.py": 600}, {}, {}, id="missing"),
    pytest.param({"backend/grown.py": 500}, {"backend/grown.py": 550},
                 None, id="growth"),
    pytest.param({"backend/same.py": 900}, {"backend/same.py": 900},
                 None, id="unchanged"),
])
def test_tightened_lowers_shrunk_entry(baseline, sizes, expected):
    size_baseline = _size_baseline()
    updated = size_baseline.tightened(baseline, sizes)
    if expected is None:
        assert updated is None
    else:
        assert updated == expected


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


def test_main_check_reports_missing_and_graduated_without_failing(
        tmp_path, capsys, monkeypatch):
    size_baseline = _size_baseline()
    target = _written(tmp_path, baseline={
        "backend/missing.py": 600,
        "backend/graduated.py": 900,
    })
    monkeypatch.setattr(size_baseline, "tracked_sizes",
                        lambda: {"backend/graduated.py": 500})
    assert size_baseline.main(["--thresholds", str(target)]) == 0
    captured = capsys.readouterr()
    assert "missing" in captured.err
    assert "graduated" in captured.err
    assert "--tighten" in captured.err


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


def test_tracked_sizes_scope_and_counts(real_tracked_sizes):
    sizes = real_tracked_sizes
    assert sizes, "tracked_sizes found no modules"
    for rel in sizes:
        assert rel.startswith(
            ("backend/", "scripts/", "tests/", "src/", "public/",
             ".github/"))
    known = REPO_ROOT / "backend" / "api.py"
    assert sizes["backend/api.py"] == len(
        known.read_text(encoding="utf-8").splitlines())


def test_tracked_sizes_cover_src_javascript(real_tracked_sizes):
    # JS/JSX uses the production ceiling; pricing.json is data.
    sizes = real_tracked_sizes
    for rel in ("src/parser.js", "src/parser-lanes.js",
                "src/dashboard-charts-extra.jsx", "src/app.jsx",
                "src/views/cache-view.jsx"):
        assert rel in sizes, f"{rel} is outside the size ratchet"
        actual = (REPO_ROOT / rel).read_text(
            encoding="utf-8").splitlines()
        assert sizes[rel] == len(actual)
    assert "src/pricing.json" not in sizes


def test_committed_document_matches_tree(real_tracked_sizes, baseline_document):
    thresholds = _thresholds()
    size_baseline = _size_baseline()
    doc = baseline_document
    baseline = thresholds.module_size_baseline(doc)
    sizes = real_tracked_sizes
    if "module_size_baseline" not in doc:
        assert baseline == {}
        assert size_baseline.violations(
            {"backend/unrecorded.py": 501}, baseline)["over"] == {
                "backend/unrecorded.py": (501, 500)}
        return
    for rel, recorded in baseline.items():
        # Master's --tighten follows a shrink down; a PR need not update
        # thresholds to record the intermediate size.
        assert sizes.get(rel, 0) <= recorded, f"grown entry for {rel}"
    found = size_baseline.violations(sizes, baseline)
    assert not found["grown"], found["grown"]
    assert not found["over"], found["over"]


def test_committed_baseline_seeds_only_over_ceiling_files(baseline_document):
    size_baseline = _size_baseline()
    thresholds = _thresholds()
    doc = baseline_document
    baseline = thresholds.module_size_baseline(doc)
    if "module_size_baseline" not in doc:
        assert baseline == {}
        assert size_baseline.violations(
            {"tests/unrecorded.py": 701}, baseline)["over"] == {
                "tests/unrecorded.py": (701, 700)}
    for rel, recorded in baseline.items():
        assert recorded > size_baseline.ceiling_for(rel)


def test_tracked_sizes_cover_sql_css_and_workflow_yaml(real_tracked_sizes):
    # The three families the audit found growing unmeasured (#393): the
    # shipped schema, the stylesheet and the workflow YAML join the
    # ratchet, priced against the production ceiling like any other
    # production source. The thresholds document itself is data, not
    # source, and stays outside.
    sizes = real_tracked_sizes
    for rel in ("backend/schema.sql", "public/app.css",
                ".github/workflows/tests.yml"):
        assert rel in sizes, f"{rel} is outside the size ratchet"
        actual = (REPO_ROOT / rel).read_text(
            encoding="utf-8").splitlines()
        assert sizes[rel] == len(actual)
    assert ".github/ci-thresholds.json" not in sizes


def test_committed_family_seeds_match_tree_and_only_over_ceiling(
        real_tracked_sizes, baseline_document):
    # Each seeded family entry is at least the file's real line count, and a
    # family seed exists only where the ceiling alone cannot gate (the
    # file is over ceiling); under-ceiling files in a new family need no
    # entry. Pairs with test_committed_baseline_seeds_only_over_ceiling.
    thresholds = _thresholds()
    size_baseline = _size_baseline()
    doc = baseline_document
    baseline = thresholds.module_size_baseline(doc)
    if "module_size_baseline" not in doc:
        assert baseline == {}
    sizes = real_tracked_sizes
    for rel in ("backend/schema.sql", "public/app.css"):
        if rel in baseline:
            assert sizes.get(rel, 0) <= baseline[rel]
            assert baseline[rel] > size_baseline.ceiling_for(rel)
        else:
            expected = {rel: (sizes[rel], 500)} if sizes[rel] > 500 else {}
            assert size_baseline.violations(
                {rel: sizes[rel]}, baseline)["over"] == expected
    # --tighten graduated tests.yml; under-ceiling files need no seed.
