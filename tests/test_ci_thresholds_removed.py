"""Declared-removal policy for the CI thresholds direction guard."""
from __future__ import annotations

import copy
import subprocess

from scripts.ci import thresholds, thresholds_removed

from tests.test_ci_thresholds_guard import (
    _document, _git, _guard_result, _judged_guard, _raised_document,
)


BASELINE_VALUES = {
    "pylint_suppression_baseline": 2,
}
REMOVED_ENTRIES = ["coverage.python", "coverage.javascript", "suite_cost",
                   "reparse", *BASELINE_VALUES]


def _removal(entry):
    base = _document()
    if entry in BASELINE_VALUES:
        base[entry] = dict.fromkeys(
            ("backend/kept.py", "backend/second.py"), BASELINE_VALUES[entry])
    head = copy.deepcopy(base)
    if entry.startswith("coverage."):
        del head["coverage"][entry.split(".")[1]]
    else:
        del head[entry]
    return base, head


def test_removal_requires_reason(tmp_path, monkeypatch, capsys):
    for entry in REMOVED_ENTRIES:
        for kind in [
            None,
            "bug",
            "definition",
        ]:
            base, head = _removal(entry)
            message = "fix: remove threshold"
            if kind:
                message += f"\n\nThresholds-Removed: {entry}={kind}"
            result = _judged_guard(tmp_path, monkeypatch, base, head, [message])
            entries = ([f"{entry}.{path}" for path in base[entry]]
                       if entry in BASELINE_VALUES else [entry])
            errors = [line for line in capsys.readouterr().err.splitlines()
                      if line.startswith("forbidden move:")]
            expected = ([] if kind else [
                f"forbidden move: removed without a reason: {name}" for name in entries])
            assert (result, errors) == (0 if kind else 1, expected)


def test_tracked_path_removal_trailer(tmp_path, monkeypatch, capsys):
    for family in BASELINE_VALUES:
        for value, allowed in [
            ("", False),
            ("{entry}=bug", True),
            ("{entry}=definition", True),
            ("{entry}.other=bug", False),
            ("{family}=bug", True),
            ("{entry}=bug, other=typo", False),
            ("other, {entry}=bug", False),
            ("{entry}=bug, =definition", False),
            (" {entry} = bug , coverage.python = definition ", True),
            ("{entry}=bug\nThresholds-Removed: invalid", True),
        ]:
            base, head = _removal(family)
            head[family] = {"backend/second.py": base[family]["backend/second.py"]}
            entry = f"{family}.backend/kept.py"
            trailer = value.format(entry=entry, family=family)
            result = _judged_guard(tmp_path, monkeypatch, base, head, [
                f"fix: remove entry\n\nThresholds-Removed:\t{trailer}"])
            err = capsys.readouterr().err
            assert (result, f"removed without a reason: {entry}" in err) == (
                0 if allowed else 1, not allowed)
            if not allowed:
                assert all(part in err for part in (
                    "Thresholds-Removed:", "<entry>=<kind>", "bug", "definition",
                    "tracked", "path"))


def test_gone_path_removal_needs_no_trailer(tmp_path, monkeypatch):
    for family, value in BASELINE_VALUES.items():
        base = _document()
        base[family] = {"backend/gone.py": value}
        head = _document()
        assert _judged_guard(tmp_path, monkeypatch, base, head, ["fix: delete file"]) == 0


def test_unproved_removal_range_fails(tmp_path, monkeypatch, capsys):
    for revs in [
        [],
        ["--base-rev", ""],
        ["--base-rev", " "],
        ["--base-rev", "unknown-revision"],
        ["--base-rev", "HEAD", "--head-rev", "HEAD"],
    ]:
        base, head = _removal("suite_cost")
        assert _judged_guard(tmp_path, monkeypatch, base, head, [
            "fix: remove\n\nThresholds-Removed: suite_cost=bug"], revs) == 1
        assert "removed without a reason: suite_cost" in capsys.readouterr().err


def test_removal_trailer_does_not_admit_readded_looser_value(tmp_path, monkeypatch, capsys):
    for family in [
        "suite_cost",
        "reparse",
        *BASELINE_VALUES,
    ]:
        base, _ = _removal(family)
        head = copy.deepcopy(base)
        if family in BASELINE_VALUES:
            head[family]["backend/kept.py"] = 1000
        else:
            head = _raised_document(family)
        assert _judged_guard(tmp_path, monkeypatch, base, head, [
            f"fix: remove\n\nThresholds-Removed: {family}=definition",
            "fix: re-add with looser value"]) == 1
        assert "removed without a reason" not in capsys.readouterr().err


def test_absent_family_seed_has_no_removal(tmp_path):
    for entry in REMOVED_ENTRIES:
        head, base = _removal(entry)
        assert _guard_result(tmp_path, base, head) == 0


def test_exemptions_intersect_committed_removals_declarations_and_candidates(tmp_path):
    root = tmp_path / 'repo'
    (root / '.github').mkdir(parents=True)
    _git(root, 'init', '-q')
    _git(root, 'config', 'user.name', 'Removal Test')
    _git(root, 'config', 'user.email', 'removal@example.invalid')
    path = root / '.github/ci-thresholds.json'
    base = {'schema_version': 1, **{
        family: dict.fromkeys(('backend/kept.py', 'backend/second.py'), value)
        for family, value in BASELINE_VALUES.items()}}
    thresholds.write(path, base)
    _git(root, 'add', '--', '.github/ci-thresholds.json')
    _git(root, 'commit', '-qm', 'record base')
    base_rev = _git(root, 'rev-parse', 'HEAD')
    head = {'schema_version': 1, **{
        family: {'backend/second.py': value} for family, value in BASELINE_VALUES.items()}}
    thresholds.write(path, head)
    _git(root, 'add', '--', '.github/ci-thresholds.json')
    tokens = ', '.join(f'{family}=definition, {family}.backend/new.py=bug'
                       for family in BASELINE_VALUES)
    _git(root, 'commit', '-qm', f'remove entries\n\nThresholds-Removed: {tokens}')
    for family in BASELINE_VALUES:
        assert thresholds_removed.exempt_paths(
            base_rev, 'HEAD', root, family, head[family],
            ['backend/kept.py', 'backend/second.py', 'backend/new.py']) == {'backend/kept.py'}
        assert thresholds_removed.exempt_paths(
            base_rev, 'HEAD', root, family, head[family], ['backend/new.py']) == set()
        assert thresholds_removed.exempt_paths(
            base_rev, 'HEAD', root, family, {}, ['backend/kept.py']) == {'backend/kept.py'}
    for base_ref, head_ref in [(None, 'HEAD'), (' ', 'HEAD'), ('missing', 'HEAD'),
                               (base_rev, 'missing'), ('HEAD', 'HEAD')]:
        assert thresholds_removed.exempt_paths(
            base_ref, head_ref, root, 'pylint_suppression_baseline', {}, ['backend/kept.py']) == set()


def test_unreadable_base_or_range_grants_no_exemptions(tmp_path, monkeypatch):
    for failed_command, answer in [
        ('log', OSError('unreadable history')),
        ('log', subprocess.TimeoutExpired('git', 10)),
        ('show', OSError('unreadable document')),
        ('show', subprocess.TimeoutExpired('git', 10)),
        ('show', None), ('show', b'{bad json'),
        ('show', b'[]'), ('show', b'{"schema_version": 2}'),
    ]:
        def read_git(argv, failed_command=failed_command, answer=answer, **kwargs):
            assert kwargs == {'capture_output': True, 'check': False, 'timeout': 10}
            if argv[3] == failed_command:
                if isinstance(answer, Exception):
                    raise answer
                return subprocess.CompletedProcess(argv, int(answer is None), answer)
            assert argv[3:] == ['log', '--format=%B', 'base..HEAD']
            return subprocess.CompletedProcess(
                argv, 0, b'Thresholds-Removed: pylint_suppression_baseline=definition')
        monkeypatch.setattr(thresholds_removed.subprocess, 'run', read_git)
        assert thresholds_removed.exempt_paths(
            'base', 'HEAD', tmp_path, 'pylint_suppression_baseline', {}, ['backend/kept.py']) == set()
