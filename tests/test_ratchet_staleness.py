"""Classify intervening commits using real git history and blob reads."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts.ci.classify_bot import BOT_EMAIL

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/ci/ratchet_staleness.py"


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def _commit(repo, changes, email="human@example.com"):
    for name, content in changes.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(repo, "add", *changes)
    _git(repo, "-c", f"user.email={email}", "commit", "-qm", "Update fixture")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture(name="history")
def _history(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "Fixture author")
    _git(tmp_path, "config", "user.email", "human@example.com")
    base = _commit(tmp_path, {
        ".github/ci-thresholds.json": "{}\n",
        "backend/constants.py": "PRICING_VERSION = '41'\n",
        "src/pricing.json": "{}\n",
        "src/pricing_notices.json": "[]\n",
    })
    return tmp_path, base


def _decision(repo, base, tip):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--base", base, "--tip", tip],
        cwd=repo, capture_output=True, text=True, check=False)


def _pricing_changes(version="42"):
    return {"backend/constants.py": f"PRICING_VERSION = '{version}'\n",
            "src/pricing.json": '{"synthetic": []}\n'}


def test_thresholds_only_is_data(history):
    repo, base = history
    tip = _commit(repo, {".github/ci-thresholds.json": '{"raised": true}\n'})
    result = _decision(repo, base, tip)
    assert result.returncode == 0, result.stderr


def test_comment_share_only_is_data(history):
    repo, base = history
    tip = _commit(repo, {".github/comment-share.json": '{}\n'})
    result = _decision(repo, base, tip)
    assert result.returncode == 0, result.stderr


def test_both_ratchet_data_files_are_data(history):
    repo, base = history
    tip = _commit(repo, {
        ".github/ci-thresholds.json": '{"raised": true}\n',
        ".github/comment-share.json": '{}\n',
    })
    result = _decision(repo, base, tip)
    assert result.returncode == 0, result.stderr


def test_comment_share_with_code_is_not_data(history):
    repo, base = history
    tip = _commit(repo, {
        ".github/comment-share.json": '{}\n',
        "backend/code.py": "answer = 1\n",
    })
    assert _decision(repo, base, tip).returncode == 1


def test_thresholds_deletion_is_data_and_requires_workflow_validation(history):
    repo, base = history
    _git(repo, "rm", ".github/ci-thresholds.json")
    _git(repo, "commit", "-qm", "Delete fixture thresholds")
    tip = _git(repo, "rev-parse", "HEAD")
    assert _decision(repo, base, tip).returncode == 0


@pytest.mark.parametrize("notices", [False, True])
def test_bot_numeric_constants_is_data(history, notices):
    repo, base = history
    changes = _pricing_changes()
    if notices:
        changes["src/pricing_notices.json"] = '["synthetic"]\n'
    tip = _commit(repo, changes, BOT_EMAIL)
    result = _decision(repo, base, tip)
    assert result.returncode == 0, result.stderr


def test_code_is_not_data(history):
    repo, base = history
    tip = _commit(repo, {"backend/code.py": "answer = 1\n"})
    assert _decision(repo, base, tip).returncode == 1


def test_non_bot_pricing_shape_is_not_data(history):
    repo, base = history
    tip = _commit(repo, _pricing_changes())
    assert _decision(repo, base, tip).returncode == 1


def test_digit_width_change_is_not_data(history):
    repo, base = history
    tip = _commit(repo, _pricing_changes("100"), BOT_EMAIL)
    assert _decision(repo, base, tip).returncode == 1


def test_non_ancestor_is_not_data(history):
    repo, base = history
    descendant = _commit(repo, {".github/ci-thresholds.json": "[]\n"})
    assert _decision(repo, descendant, base).returncode == 1


def test_thresholds_stacked_onto_bot_is_data(history):
    repo, base = history
    _commit(repo, _pricing_changes(), BOT_EMAIL)
    tip = _commit(repo, {".github/ci-thresholds.json": '{"raised": true}\n'})
    assert _decision(repo, base, tip).returncode == 0


def test_extra_path_on_bot_commit_is_not_data(history):
    repo, base = history
    changes = {**_pricing_changes(), "backend/code.py": "answer = 1\n"}
    tip = _commit(repo, changes, BOT_EMAIL)
    assert _decision(repo, base, tip).returncode == 1


def test_reverted_code_between_data_commits_is_not_data(history):
    repo, base = history
    _commit(repo, {"backend/code.py": "answer = 1\n"})
    _git(repo, "revert", "--no-edit", "HEAD")
    tip = _commit(repo, {".github/ci-thresholds.json": "[]\n"})
    assert _decision(repo, base, tip).returncode == 1


def test_missing_constants_parent_is_not_data(history):
    repo, _base = history
    _git(repo, "rm", "backend/constants.py")
    _git(repo, "commit", "-qm", "Remove fixture constants")
    base = _git(repo, "rev-parse", "HEAD")
    tip = _commit(repo, _pricing_changes(), BOT_EMAIL)
    assert _decision(repo, base, tip).returncode == 1


def test_unreadable_ref_is_not_data(history):
    repo, base = history
    assert _decision(repo, base, "missing-ref").returncode == 1


def test_rename_into_thresholds_is_not_data(history):
    repo, _base = history
    base = _commit(repo, {"source.json": "[]\n"})
    _git(repo, "mv", "-f", "source.json", ".github/ci-thresholds.json")
    _git(repo, "commit", "-qm", "Rename fixture")
    assert _decision(repo, base, _git(repo, "rev-parse", "HEAD")).returncode == 1
