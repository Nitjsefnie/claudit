"""Declared-removal policy for the CI thresholds direction guard."""
from __future__ import annotations

import copy

from tests.test_ci_thresholds_guard import (
    _document, _guard_result, _judged_guard, _raised_document,
)


BASELINE_VALUES = {
    "module_size_baseline": 900, "pylint_suppression_baseline": 2,
    "comment_share_baseline": [20, 100],
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
    for family in BASELINE_VALUES:
        base = _document()
        base[family] = {"backend/gone.py": BASELINE_VALUES[family]}
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
            head[family]["backend/kept.py"] = (
                [30, 100] if family == "comment_share_baseline" else 1000)
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
