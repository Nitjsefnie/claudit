"""The coverage shard helper refuses partial or unreadable inputs."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from coverage import CoverageData
import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/ci/coverage_shards.py"


def _shards(root: Path, identical: bool = False) -> list[Path]:
    directories = []
    for shard in range(1, 5):
        directory = root / f"coverage-shard-{shard}"
        directory.mkdir()
        data = CoverageData(basename=str(directory / f".coverage.{shard}"))
        data.add_lines({"probe.py": {1 if identical else shard}})
        data.write()
        data.close()
        directories.append(directory)
    return directories


def _run(root: Path, directories: list[Path]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--validate-and-combine", *map(str, directories)],
        cwd=root, env={**os.environ, "COVERAGE_FILE": str(root / ".coverage")},
        capture_output=True, text=True, check=False, timeout=30)


@pytest.mark.parametrize("identical", [False, True])
def test_helper_consumes_all_four_readable_shards(tmp_path, identical):
    directories = _shards(tmp_path, identical)
    result = _run(tmp_path, directories)
    assert result.returncode == 0, result.stderr
    data = CoverageData(basename=str(tmp_path / ".coverage"))
    data.read()
    assert data.measured_files() == {"probe.py"}
    assert set(data.lines("probe.py") or []) == ({1} if identical else {1, 2, 3, 4})
    data.close()
    assert result.stderr.count("Combined 1 file") == 4
    assert "skipped" not in result.stderr and "errored" not in result.stderr
    assert all((directory / f".coverage.{shard}").is_file()
               for shard, directory in enumerate(directories, 1))


@pytest.mark.parametrize("identical", [False, True])
@pytest.mark.parametrize("corrupt_bytes", [b"corrupt coverage bytes", b""])
def test_helper_refuses_corrupt_shard_four(tmp_path, identical, corrupt_bytes):
    directories = _shards(tmp_path, identical)
    corrupt = directories[3] / ".coverage.4"
    corrupt.write_bytes(corrupt_bytes)
    result = _run(tmp_path, directories)
    assert result.returncode != 0
    assert "shard 4" in result.stderr
    assert not (tmp_path / ".coverage").exists()
    assert corrupt.read_bytes() == corrupt_bytes


@pytest.mark.parametrize("count", [3, 5])
def test_helper_requires_four_directories(tmp_path, count):
    directories = _shards(tmp_path)
    result = _run(tmp_path, directories[:count] if count == 3 else [*directories, tmp_path])
    assert result.returncode != 0
    assert "four" in result.stderr.lower()
    assert not (tmp_path / ".coverage").exists()


def test_helper_refuses_duplicate_directories(tmp_path):
    directories = _shards(tmp_path)
    result = _run(tmp_path, [*directories[:3], directories[2]])
    assert result.returncode != 0
    assert "distinct" in result.stderr.lower()
    assert not (tmp_path / ".coverage").exists()


def test_helper_refuses_multiple_files_in_one_shard(tmp_path):
    directories = _shards(tmp_path)
    (directories[3] / ".coverage.extra").write_bytes(b"extra data")
    result = _run(tmp_path, directories)
    assert result.returncode != 0
    assert "shard 4" in result.stderr
    assert not (tmp_path / ".coverage").exists()


@pytest.mark.parametrize("missing", [1, 2, 3, 4])
@pytest.mark.parametrize("kind", ["directory", "file", "nonfile"])
def test_helper_requires_every_shard_data_file(tmp_path, missing, kind):
    directories = _shards(tmp_path)
    directory = directories[missing - 1]
    if kind == "directory":
        directory.rename(tmp_path / f"withheld-shard-{missing}")
    else:
        path = directory / f".coverage.{missing}"
        path.rename(directory / "withheld-data")
        if kind == "nonfile":
            path.mkdir()
    result = _run(tmp_path, directories)
    assert result.returncode != 0
    assert f"shard {missing}" in result.stderr
    assert not (tmp_path / ".coverage").exists()


def test_helper_does_not_publish_a_partially_failed_combine(tmp_path):
    directories = []
    for shard in range(1, 5):
        directory = tmp_path / f"coverage-shard-{shard}"
        directory.mkdir()
        data = CoverageData(basename=str(directory / f".coverage.{shard}"))
        if shard == 4:
            data.add_arcs({"probe.py": {(1, 2)}})
        else:
            data.add_lines({"probe.py": {shard}})
        data.write()
        data.close()
        directories.append(directory)
    result = _run(tmp_path, directories)
    assert result.returncode != 0
    assert "shard 4" in result.stderr
    assert result.stderr.count("Combined 1 file") == 3
    assert not (tmp_path / ".coverage").exists()
