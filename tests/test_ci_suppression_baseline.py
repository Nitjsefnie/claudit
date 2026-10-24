"""Tests for the inline pylint complexity-suppression baseline ratchet.

Production Python gains a second only-shrinks baseline: the per-file
count of inline ``pylint: disable``/``disable-next`` comments whose check
list names a complexity check (any ``too-many-…``), under ``backend/``
and ``scripts/`` — ``tests/`` stays outside. The cases pin
the counted shape, the four reported statuses, the tighten direction
(only ever down or gone) and the committed document's shrink-tolerant
agreement with the actual tree.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests import git_meta
from tests.thresholds_history import stub_history

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
    schema = _load("thresholds")
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
        "module_size_baseline": {},
        "pylint_suppression_baseline": baseline or {},
        "suite_cost": {
            "collection": {"measured": 25.8, "floor": 27.3},
            "run": {"measured": 309.8, "floor": 311.3},
            "residual": {"measured": 6.5, "floor": 8.0},
        },
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


@pytest.mark.parametrize('entry,recorded,sibling,status', [
    ('pylint_suppression_baseline.backend/api.py', True, False, 0),
    ('pylint_suppression_baseline', True, False, 0),
    ('', True, False, 1),
    ('pylint_suppression_baseline.backend/other.py', True, False, 1),
    ('pylint_suppression_baseline', True, True, 1),
    ('pylint_suppression_baseline.backend/api.py', False, False, 1),
])
def test_declared_removal_check(tmp_path, monkeypatch, capsys, entry, recorded, sibling, status):
    module = _suppression_baseline()
    target = _written(tmp_path)
    measured = {'backend/api.py': 2}
    if sibling:
        measured['backend/new.py'] = 2
    monkeypatch.setattr(module, 'tracked_suppression_counts', lambda: measured)
    base = {'schema_version': 1, 'pylint_suppression_baseline':
            {'backend/api.py': 3} if recorded else {}}
    stub_history(monkeypatch, module, REPO_ROOT, f'Thresholds-Removed: {entry}=bug', base)
    assert module.main(['--thresholds', str(target),
                        '--base-rev', 'base', '--head-rev', 'HEAD']) == status
    captured = capsys.readouterr()
    if status == 0:
        assert 'declared-removed: backend/api.py' in captured.out
        assert '2' in captured.out
    else:
        assert 'over:' in captured.err
        if sibling:
            assert 'backend/new.py' in captured.err
            assert 'declared-removed: backend/api.py' in captured.out


def test_declaration_does_not_exempt_recorded_statuses():
    module = _suppression_baseline()
    found = module.violations(
        {'backend/grown.py': 3, 'backend/graduated.py': 0},
        {'backend/grown.py': 2, 'backend/graduated.py': 1, 'backend/gone.py': 1},
        declared_removed={'backend/grown.py', 'backend/graduated.py', 'backend/gone.py'})
    assert found == {'grown': {'backend/grown.py': (3, 2)}, 'over': {},
                     'missing': ['backend/gone.py'], 'graduated': {'backend/graduated.py': 0}}


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


def test_main_check_reports_missing_and_graduated_without_failing(
        tmp_path, capsys, monkeypatch):
    suppression = _suppression_baseline()
    target = _written(tmp_path, baseline={
        "backend/missing.py": 1,
        "backend/graduated.py": 3,
    })
    monkeypatch.setattr(suppression, "tracked_suppression_counts",
                        lambda: {"backend/graduated.py": 0})
    assert suppression.main(["--thresholds", str(target)]) == 0
    captured = capsys.readouterr()
    assert "missing" in captured.err
    assert "graduated" in captured.err
    assert "--tighten" in captured.err


def test_script_help_invokes_cli():
    script = REPO_ROOT / "scripts" / "ci" / "suppression_baseline.py"
    result = subprocess.run(
        [sys.executable, str(script), "--help"], cwd=REPO_ROOT,
        capture_output=True, check=False, text=True)
    assert result.returncode == 0
    assert "usage:" in result.stdout


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


@pytest.mark.parametrize("absent_family", [False, True])
def test_committed_document_matches_tree(tmp_path, absent_family):
    # Entries bound production files from above. Master's --tighten
    # follows reduced counts down and drops missing or clean files, so a
    # PR need not update thresholds for those reductions.
    git_meta.require_own_git_metadata(REPO_ROOT)
    thresholds = _thresholds()
    suppression = _suppression_baseline()
    path = THRESHOLDS_PATH
    if absent_family:
        data = _document()
        del data["pylint_suppression_baseline"]
        path = tmp_path / "absent.json"
        thresholds.write(path, data)
    doc = thresholds.load(path)
    baseline = doc.get("pylint_suppression_baseline", {})
    if absent_family:
        assert baseline == {}
        assert suppression.violations(
            {"backend/unrecorded.py": 1}, baseline)["over"] == {
                "backend/unrecorded.py": (1, 1)}
        return
    counts = suppression.tracked_suppression_counts()
    for rel, recorded in baseline.items():
        assert counts.get(rel, 0) <= recorded, f"grown entry for {rel}"
    exempt = suppression.thresholds_removed.exempt_paths(
        os.environ.get('CLAUDIT_THRESHOLDS_BASE_REV'), 'HEAD', REPO_ROOT,
        'pylint_suppression_baseline', baseline, counts)
    found = suppression.violations(
        counts, baseline,
        declared_removed=exempt)
    assert not found["grown"], found["grown"]
    assert not found["over"], found["over"]


@pytest.mark.parametrize("state", ["absent", "empty", "recorded", "declared-removal"])
def test_tighten_seeds_unrecorded_suppressions(tmp_path, monkeypatch, capsys, state):
    thresholds = _thresholds()
    module = _suppression_baseline()
    data = _document()
    measured = {"backend/new.py": 1, "backend/multiple.py": 3,
                "backend/clean.py": 0}
    expected = {"backend/new.py": 1, "backend/multiple.py": 3}
    if state == "absent":
        del data["pylint_suppression_baseline"]
    elif state == "recorded":
        data["pylint_suppression_baseline"] = {
            "backend/shrunk.py": 4, "backend/grown.py": 1,
            "backend/gone.py": 2, "backend/graduated.py": 2}
        measured.update({"backend/shrunk.py": 2, "backend/grown.py": 3,
                         "backend/graduated.py": 0})
        expected.update({"backend/shrunk.py": 2, "backend/grown.py": 1})
    target = tmp_path / "thresholds.json"
    thresholds.write(target, data)
    monkeypatch.setattr(module, "tracked_suppression_counts", lambda: measured)
    if state == 'declared-removal':
        base = dict(data, pylint_suppression_baseline={'backend/new.py': 4, 'backend/multiple.py': 4})
        stub_history(monkeypatch, module, REPO_ROOT, 'Thresholds-Removed: pylint_suppression_baseline=definition', base)
        removed = module.thresholds_removed.declared('base', 'HEAD', REPO_ROOT)
        assert not importlib.import_module('thresholds_guard').forbidden_moves(
            base, data, set(thresholds.BASELINE_MEMBERS), removal_declarations=removed)
        assert module.main(['--thresholds', str(target), '--base-rev', 'base']) == 0
    assert module.main(["--tighten", "--thresholds", str(target)]) == 0
    assert thresholds.load(target).get("pylint_suppression_baseline", {}) == expected
    assert "seeded" in capsys.readouterr().out


@pytest.mark.parametrize('entry,recorded', [
    ('pylint_suppression_baseline', True),
    ('pylint_suppression_baseline.backend/api.py', True),
    ('pylint_suppression_baseline.backend/api.py', False),
])
def test_committed_removal_is_limited_to_base_entries(tmp_path, monkeypatch, entry, recorded):
    module = _suppression_baseline()
    target = _written(tmp_path)
    monkeypatch.setattr(sys.modules[__name__], 'THRESHOLDS_PATH', target)
    monkeypatch.setattr(sys.modules[__name__], '_suppression_baseline', lambda: module)
    monkeypatch.setattr(git_meta, 'require_own_git_metadata', lambda _root: None)
    removed_only = {'backend/api.py': 1}

    def consume(measured):
        monkeypatch.setattr(module, 'tracked_suppression_counts', lambda: measured)
        test_committed_document_matches_tree(tmp_path, False)

    base = {'schema_version': 1, 'pylint_suppression_baseline':
            {'backend/api.py': 4} if recorded else {}}
    stub_history(monkeypatch, module, REPO_ROOT, f'Thresholds-Removed: {entry}=definition', base)
    for head_absent in (False, True):
        if head_absent:
            data = _document()
            del data['pylint_suppression_baseline']
            _thresholds().write(target, data)
        monkeypatch.setenv('CLAUDIT_PERTURBED_DATA', '1')
        monkeypatch.delenv('CLAUDIT_THRESHOLDS_BASE_REV', raising=False)
        with pytest.raises(AssertionError, match='backend/api.py'):
            consume(removed_only)
        monkeypatch.setenv('CLAUDIT_THRESHOLDS_BASE_REV', 'base')
        if recorded:
            consume(removed_only)
            with pytest.raises(AssertionError, match='backend/new.py'):
                consume(dict(removed_only, **{'backend/new.py': 1}))
        else:
            with pytest.raises(AssertionError, match='backend/api.py'):
                consume(removed_only)
