"""Behavioral tests for the pytest file partition and its CLI."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ci" / "pytest_shards.py"
FILES = [
    ("tests/test_z.py", 8),
    ("tests/test_b.py", 8),
    ("tests/nested/test_c.py", 4),
    ("tests/nested/test_a.py", 4),
    ("tests/test_y.py", 2),
    ("tests/test_x.py", 2),
]


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("pytest_shards", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["pytest_shards"] = module
    spec.loader.exec_module(module)
    return module


def _run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], cwd=root,
        capture_output=True, text=True, check=False, timeout=30)


@pytest.fixture(name="mini_tree")
def mini_tree_fixture(tmp_path: Path) -> Path:
    for relative, lines in FILES:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("pass\n" * lines, encoding="utf-8")
    for relative in ("tests/conftest.py", "tests/helper.py", "tests/other_test.py",
                     "tests/nested/test_data.txt", "outside/test_ignored.py"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("pass\n", encoding="utf-8")
    (tmp_path / "tests" / "test_directory.py").mkdir()
    return tmp_path


@pytest.mark.parametrize("shards", [1, 2, 4, 8])
def test_partition_covers_each_file_once(shards: int):
    groups = _load().partition(FILES, shards)
    assert len(groups) == shards
    assert {item for group in groups for item in group} == set(FILES)
    assert sum(len(group) for group in groups) == len(FILES)
    for index, group in enumerate(groups):
        for other in groups[index + 1:]:
            assert set(group).isdisjoint(other)


def test_partition_is_deterministic_and_preserves_input():
    module = _load()
    files = FILES.copy()
    first = module.partition(files, 4)
    assert module.partition(files, 4) == first
    assert module.partition(list(reversed(files)), 4) == first
    assert files == FILES


@pytest.mark.parametrize("shards", [1, 2, 4, 8])
def test_partition_balance_is_bounded_by_largest_file(shards: int):
    groups = _load().partition(FILES, shards)
    loads = [sum(lines for _, lines in group) for group in groups]
    assert max(loads) <= min(loads) + max(lines for _, lines in FILES)


def test_partition_greedy_ties_and_path_sorted_results():
    assert _load().partition(FILES, 2) == [
        [("tests/nested/test_a.py", 4), ("tests/test_b.py", 8),
         ("tests/test_x.py", 2)],
        [("tests/nested/test_c.py", 4), ("tests/test_y.py", 2),
         ("tests/test_z.py", 8)],
    ]


@pytest.mark.parametrize("shard, expected", [
    (1, "tests/nested/test_a.py tests/test_b.py tests/test_x.py\n"),
    (2, "tests/nested/test_c.py tests/test_y.py tests/test_z.py\n"),
])
def test_cli_prints_only_recursive_test_files(mini_tree: Path, shard: int,
                                              expected: str):
    result = _run(mini_tree, "--shard", str(shard), "--of", "2")
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected
    assert result.stderr == ""


def test_cli_table_counts_lines_and_defaults_to_four_shards(mini_tree: Path):
    result = _run(mini_tree, "--table")
    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        "shard 1: 1 files, 8 lines\n"
        "shard 2: 1 files, 8 lines\n"
        "shard 3: 2 files, 6 lines\n"
        "shard 4: 2 files, 6 lines\n")


def test_cli_table_respects_shard_count(mini_tree: Path):
    result = _run(mini_tree, "--table", "--of", "2")
    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        "shard 1: 3 files, 14 lines\n"
        "shard 2: 3 files, 14 lines\n")


@pytest.mark.parametrize("args", [
    ("--shard", "0", "--of", "4"),
    ("--shard", "5", "--of", "4"),
    ("--shard", "1", "--of", "0"),
    ("--table", "--of", "-1"),
])
def test_cli_refuses_invalid_shard_numbers(mini_tree: Path, args: tuple[str, ...]):
    result = _run(mini_tree, *args)
    assert result.returncode == 2
    assert result.stdout == ""
    assert "error:" in result.stderr


@pytest.mark.parametrize("args", [("--shard", "1", "--of", "4"), ("--table",)])
@pytest.mark.parametrize("tests_directory", [False, True])
def test_cli_refuses_empty_tree(tmp_path: Path, args: tuple[str, ...],
                                tests_directory: bool):
    if tests_directory:
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "helper.py").write_text("pass\n", encoding="utf-8")
    result = _run(tmp_path, *args)
    assert result.returncode == 2
    assert result.stdout == ""
    assert "error:" in result.stderr
