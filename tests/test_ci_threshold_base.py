from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

CI_DIR = Path(__file__).resolve().parents[1] / "scripts" / "ci"
sys.path.insert(0, str(CI_DIR))
TRACKED_PATH = "thresholds.json"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, CI_DIR / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


SCRIPT = Path(_load("threshold_base").__file__ or "")


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=check, timeout=10)


def _init(repo: Path) -> Path:
    repo.mkdir()
    _git(repo, "init")
    return repo


def _commit(repo: Path, content: str, path: str = TRACKED_PATH) -> str:
    (repo / path).write_text(content, encoding="utf-8")
    _git(repo, "add", path)
    _git(repo, "-c", "user.email=ci@example.invalid", "-c", "user.name=CI",
         "-c", "commit.gpgsign=false", "commit", "-m", content)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _run(repo: Path, before: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), before, TRACKED_PATH],
        cwd=repo, capture_output=True, text=True, check=False, timeout=10)


def _assert_base(result: subprocess.CompletedProcess[str], expected: str) -> None:
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{expected}\n"
    assert result.stderr == ""
    assert len(expected) == 40
    assert all(character in "0123456789abcdef" for character in expected)


@pytest.fixture(name="history")
def _history(tmp_path: Path) -> tuple[Path, str]:
    repo = _init(tmp_path / "repo-a")
    parent = _commit(repo, "first")
    _commit(repo, "second")
    return repo, parent


def test_absent_before_falls_back_to_parent(tmp_path: Path, history: tuple[Path, str]) -> None:
    repo, parent = history
    other = _init(tmp_path / "repo-b")
    absent = _commit(other, "unrelated")
    assert _git(repo, "cat-file", "-e", f"{absent}^{{commit}}", check=False).returncode != 0

    _assert_base(_run(repo, absent), parent)


@pytest.mark.parametrize("advance_head", [False, True], ids=["parent", "older-ancestor"])
def test_fast_forward_uses_before(history: tuple[Path, str], advance_head: bool) -> None:
    repo, before = history
    if advance_head:
        _commit(repo, "third")

    _assert_base(_run(repo, before), before)


@pytest.mark.parametrize("before", ["0" * 40, ""], ids=["all-zeros", "empty"])
def test_zero_or_empty_before_uses_parent(history: tuple[Path, str], before: str) -> None:
    repo, parent = history

    _assert_base(_run(repo, before), parent)


def test_before_without_path_uses_parent(tmp_path: Path) -> None:
    repo = _init(tmp_path / "repo")
    before = _commit(repo, "no thresholds", "other.txt")
    parent = _commit(repo, "added thresholds")
    _commit(repo, "updated thresholds")
    assert _git(repo, "cat-file", "-e", f"{before}:{TRACKED_PATH}", check=False).returncode != 0

    _assert_base(_run(repo, before), parent)


def test_no_base_reports_both_candidates(tmp_path: Path) -> None:
    repo = _init(tmp_path / "repo-a")
    _commit(repo, "only commit")
    other = _init(tmp_path / "repo-b")
    absent = _commit(other, "unrelated")
    assert _git(repo, "cat-file", "-e", f"{absent}^{{commit}}", check=False).returncode != 0

    result = _run(repo, absent)

    assert result.returncode == 3
    assert result.stdout == ""
    assert absent in result.stderr
    assert "HEAD^" in result.stderr
    assert result.stderr.count("does not resolve to a commit") == 2
    assert "Traceback" not in result.stderr


def test_parent_without_path_reports_missing_path(tmp_path: Path) -> None:
    repo = _init(tmp_path / "repo")
    _commit(repo, "no thresholds", "other.txt")
    _commit(repo, "added thresholds")

    result = _run(repo, "0" * 40)

    assert result.returncode == 3
    assert result.stdout == ""
    assert "0" * 40 in result.stderr
    assert "all zeros" in result.stderr
    assert "HEAD^" in result.stderr
    assert "resolves to a commit" in result.stderr
    assert TRACKED_PATH in result.stderr
