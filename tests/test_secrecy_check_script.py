from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "secrecy-check.sh"
POSIX_ONLY = pytest.mark.skipif(
    os.name == "nt", reason="the script runs under POSIX Bash")
LIT_A = "zq" + "literal" + "alpha"
LIT_B = "zq" + "literal" + "beta"
AUTH_DB = "zq" + "authdb" + "name"
# No ambient identity or config: the fixture repositories must behave the same on any host.
ENV = {**{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
       "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
       "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, env=ENV, check=True,
                   capture_output=True, text=True)


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


def _commit(repo: Path, name: str, text: str) -> None:
    _write(repo / name, text)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", f"add {name}")


def _worktrees(tmp_path: Path, bare: bool = False) -> tuple[Path, Path]:
    """The main location (a checkout, or the bare repository) and a linked worktree."""
    main, linked = tmp_path / "main", tmp_path / "linked"
    main.mkdir()
    _git(main, "init", "-q", "-b", "main")
    _commit(main, "README", "clean\n")
    if not bare:
        _git(main, "worktree", "add", "-q", "-b", "feature", str(linked))
        return main, linked
    base = tmp_path / "base.git"
    _git(tmp_path, "clone", "-q", "--bare", str(main), str(base))
    _git(base, "worktree", "add", "-q", "-b", "feature", str(linked))
    return base, linked


def _check(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(SCRIPT), *args], cwd=cwd, env=ENV,
                          capture_output=True, text=True, check=False, timeout=60)


def _env_file(directory: Path) -> None:
    _write(directory / ".env",
           f"CLAUDIT_DATABASE_URL_AUTH=postgresql://u:p@h/{AUTH_DB}?sslmode=x\n")


@POSIX_ONLY
def test_linked_worktree_reads_literals_file_from_the_main_worktree(tmp_path: Path) -> None:
    main, linked = _worktrees(tmp_path)
    _write(main / ".secrecy-literals", f"{LIT_A}  # one per line\n")
    clean = _check(linked, "--tree")
    assert (clean.returncode, clean.stdout) == (0, "secrecy-check: tree clean (1 literal(s))\n")
    _commit(linked, "planted.txt", f"x {LIT_A} y\n")
    planted = _check(linked, "--tree")
    assert planted.returncode == 1
    assert "a listed literal" in planted.stderr and "planted.txt" in planted.stderr


@POSIX_ONLY
def test_linked_worktree_reads_the_auth_database_name_from_the_main_worktree(
        tmp_path: Path) -> None:
    main, linked = _worktrees(tmp_path)
    _env_file(main)
    assert _check(linked, "--tree").stdout == "secrecy-check: tree clean (1 literal(s))\n"
    _commit(linked, "planted.txt", f"{AUTH_DB}\n")
    planted = _check(linked, "--tree")
    assert planted.returncode == 1
    assert "the auth-DB name" in planted.stderr and "planted.txt" in planted.stderr


@POSIX_ONLY
def test_the_worktrees_own_copy_adds_to_the_main_worktrees_literals(tmp_path: Path) -> None:
    main, linked = _worktrees(tmp_path)
    _write(main / ".secrecy-literals", f"{LIT_A}\n")
    _write(linked / ".secrecy-literals", f"{LIT_B}\n")
    _commit(linked, "only_a.txt", f"{LIT_A}\n")
    _commit(linked, "only_b.txt", f"{LIT_B}\n")
    result = _check(linked, "--tree")
    assert result.returncode == 1
    assert result.stderr.count("is in the working tree") == 2
    found = {("only_a.txt" in block, "only_b.txt" in block)
             for block in result.stderr.split("FAIL")[1:]}
    assert found == {(True, False), (False, True)}


@POSIX_ONLY
def test_a_main_worktree_literal_in_history_fails_the_full_run(tmp_path: Path) -> None:
    main, linked = _worktrees(tmp_path)
    _write(main / ".secrecy-literals", f"{LIT_A}\n")
    _commit(main, "leak.txt", f"{LIT_A}\n")
    _git(main, "rm", "-q", "leak.txt")
    _git(main, "commit", "-q", "-m", "remove the leak")
    result = _check(linked)
    assert result.returncode == 1
    assert "is in committed history" in result.stderr
    assert "working tree" not in result.stderr


@POSIX_ONLY
def test_the_main_checkout_reads_its_files_once(tmp_path: Path) -> None:
    main, _ = _worktrees(tmp_path)
    _write(main / ".secrecy-literals", f"{LIT_A}\n")
    result = _check(main)
    assert (result.returncode, result.stdout) == (
        0, "secrecy-check: clean (tree + history, 1 literal(s))\n")


@POSIX_ONLY
def test_no_literal_in_either_location_is_an_error(tmp_path: Path) -> None:
    _, linked = _worktrees(tmp_path)
    result = _check(linked, "--tree")
    assert result.returncode == 1
    assert "ERROR" in result.stderr and "no literals available" in result.stderr


@POSIX_ONLY
def test_a_bare_repositorys_worktree_needs_a_copy_of_its_own(tmp_path: Path) -> None:
    base, linked = _worktrees(tmp_path, bare=True)
    _write(base / ".secrecy-literals", f"{LIT_A}\n")  # a bare repository holds no worktree files
    result = _check(linked, "--tree")
    assert result.returncode == 1
    assert "no literals available" in result.stderr
    _write(linked / ".secrecy-literals", f"{LIT_B}\n")
    own = _check(linked, "--tree")
    assert (own.returncode, own.stdout) == (0, "secrecy-check: tree clean (1 literal(s))\n")
