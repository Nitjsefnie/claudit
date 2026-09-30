"""Tests for the git-metadata skip guard (issue #392).

Six tests of the documented portable subset shell out to git over the
tree under test and error with ``git exit 128`` from a plain source
archive (no ``.git``). The guard skips such a test only when the tree
under test has no git metadata OF ITS OWN — ``git rev-parse
--show-toplevel`` run inside the tree does not resolve to the tree —
so an archive unpacked inside another checkout skips too, instead of
letting git answer for the outer repository. In a real checkout every
guarded test still runs and still bites.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests import git_meta

REPO_ROOT = Path(__file__).resolve().parents[1]


def _fake_run(returncode, stdout, expected_cwd=None):
    """A git probe standing in for subprocess.run, returning `stdout`.

    Asserts the exact command and (when `expected_cwd` is given) the
    working directory the probe ran with, so a mutant that drops
    `cwd=root` fails these tests instead of passing invisibly.
    """
    def fake_run(cmd, **kwargs):
        assert cmd == ["git", "rev-parse", "--show-toplevel"], cmd
        if expected_cwd is not None:
            assert kwargs.get("cwd") == expected_cwd, kwargs.get("cwd")
        return subprocess.CompletedProcess(cmd, returncode, stdout)
    return fake_run


def test_skip_raises_with_reason_when_probe_false(monkeypatch):
    monkeypatch.setattr(git_meta, "has_own_git_metadata",
                        lambda root: False)
    with pytest.raises(pytest.skip.Exception) as raised:
        git_meta.require_own_git_metadata(REPO_ROOT)
    assert git_meta.SKIP_REASON in str(raised.value)


def test_require_returns_silently_when_probe_true(monkeypatch):
    monkeypatch.setattr(git_meta, "has_own_git_metadata",
                        lambda root: True)
    assert git_meta.require_own_git_metadata(REPO_ROOT) is None


def test_probe_accepts_matching_toplevel(monkeypatch, tmp_path):
    monkeypatch.setattr(
        git_meta.subprocess, "run",
        _fake_run(0, f"{tmp_path}\n".encode(), expected_cwd=tmp_path))
    assert git_meta.has_own_git_metadata(tmp_path)


def test_probe_rejects_foreign_toplevel(monkeypatch, tmp_path):
    # An enclosing repository's toplevel is not this tree's metadata.
    monkeypatch.setattr(
        git_meta.subprocess, "run",
        _fake_run(0, f"{tmp_path / 'outer'}\n".encode(),
                  expected_cwd=tmp_path))
    assert not git_meta.has_own_git_metadata(tmp_path)


def test_probe_rejects_git_failure(monkeypatch, tmp_path):
    # Outside any repository git exits 128 ("not a git repository").
    monkeypatch.setattr(
        git_meta.subprocess, "run",
        _fake_run(128, b"", expected_cwd=tmp_path))
    assert not git_meta.has_own_git_metadata(tmp_path)


def test_probe_tolerates_missing_git(monkeypatch, tmp_path):
    def missing(cmd, **kwargs):
        raise FileNotFoundError("git")
    monkeypatch.setattr(git_meta.subprocess, "run", missing)
    assert not git_meta.has_own_git_metadata(tmp_path)


def test_probe_reads_the_real_tree_in_a_checkout():
    # Archive-safe form: the probe's verdict on THIS tree must equal
    # what a direct git probe says from the suite's own directory. In a
    # checkout both say "own metadata"; from an archive both say no.
    direct = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=REPO_ROOT, capture_output=True, check=False)
    resolved = (Path(direct.stdout.decode().strip()).resolve()
                if direct.returncode == 0 else None)
    assert git_meta.has_own_git_metadata(REPO_ROOT) == (
        resolved is not None
        and resolved == REPO_ROOT.resolve())
