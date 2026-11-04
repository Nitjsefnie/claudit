"""Behavioral tests for the pytest file partition and its CLI."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml

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


def _tree(root: Path, files: dict[str, int]) -> None:
    for relative, lines in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("pass\n" * lines, encoding="utf-8", newline="\n")


def _weights(root: Path, maps: dict[str, dict[str, int]]) -> Path:
    path = root / "weights.json"
    path.write_text(json.dumps({"run_id": "123", "weights": maps}),
                    encoding="utf-8", newline="\n")
    return path


def _weighted_shards(root: Path, table: Path, os_name: str) -> list[list[str]]:
    groups = []
    for shard in ("1", "2"):
        result = _run(root, "--shard", shard, "--of", "2", "--weights", str(table),
                      "--os", os_name)
        assert result.returncode == 0, result.stderr
        groups.append(result.stdout.split())
    return groups


def test_weighted_cli_uses_recorded_positive_weights(tmp_path: Path):
    _tree(tmp_path, {"tests/test_a.py": 1, "tests/test_b.py": 2, "tests/test_c.py": 3})
    table = _weights(tmp_path, {"os": {"tests/test_a.py": 10, "tests/test_b.py": 9,
                                       "tests/test_c.py": 8}})
    result = _run(tmp_path, "--shard", "1", "--of", "2")
    assert result.returncode == 0 and result.stdout.split() == ["tests/test_c.py"]
    assert _weighted_shards(tmp_path, table, "os") == [
        ["tests/test_a.py"], ["tests/test_b.py", "tests/test_c.py"]]


def test_weighted_cli_spreads_recorded_zeroes(tmp_path: Path):
    files = {f"tests/test_{name}.py": 1 for name in "abcd"}
    _tree(tmp_path, files)
    table = _weights(tmp_path, {"os": dict.fromkeys(files, 0)})
    assert _weighted_shards(tmp_path, table, "os") == [
        ["tests/test_a.py", "tests/test_c.py"],
        ["tests/test_b.py", "tests/test_d.py"]]


def test_weighted_cli_scales_missing_files_from_positive_current_entries(tmp_path: Path):
    _tree(tmp_path, {"tests/test_a.py": 10, "tests/test_b.py": 10,
                     "tests/test_c.py": 100, "tests/test_d.py": 9})
    table = _weights(tmp_path, {"os": {"tests/test_a.py": 1000, "tests/test_b.py": 400,
                                       "tests/test_c.py": 0, "tests/test_gone.py": 999999}})
    assert _weighted_shards(tmp_path, table, "os") == [
        ["tests/test_a.py", "tests/test_c.py"],
        ["tests/test_b.py", "tests/test_d.py"]]


def test_weighted_table_reports_floor_and_rounded_fallback(tmp_path: Path):
    _tree(tmp_path, {"tests/test_a.py": 2, "tests/test_b.py": 1, "tests/test_c.py": 1})
    table = _weights(tmp_path, {"os": {"tests/test_a.py": 5, "tests/test_c.py": 0}})
    result = _run(tmp_path, "--table", "--of", "2", "--weights", str(table), "--os", "os")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "shard 1: 1 files, 200 weight\nshard 2: 2 files, 7 weight\n"


def test_weighted_cli_refuses_unscalable_missing_files(tmp_path: Path):
    _tree(tmp_path, {"tests/test_a.py": 1, "tests/test_new.py": 1})
    table = _weights(tmp_path, {"os": {"tests/test_a.py": 0}})
    result = _run(tmp_path, "--shard", "1", "--weights", str(table), "--os", "os")
    assert result.returncode != 0
    assert "positive" in result.stderr.lower()


def test_weighted_cli_selects_os_map(tmp_path: Path):
    files = {f"tests/test_{name}.py": 1 for name in "abc"}
    _tree(tmp_path, files)
    table = _weights(tmp_path, {
        "first": {"tests/test_a.py": 10, "tests/test_b.py": 9, "tests/test_c.py": 8},
        "second": {"tests/test_a.py": 8, "tests/test_b.py": 9, "tests/test_c.py": 10},
    })
    assert _weighted_shards(tmp_path, table, "first")[0] == ["tests/test_a.py"]
    assert _weighted_shards(tmp_path, table, "second")[0] == ["tests/test_c.py"]


@pytest.mark.parametrize("args, message", [
    (("--weights", "weights.json", "--os", "missing"), "regenerate"),
    (("--weights", "weights.json"), "together"),
    (("--os", "os"), "together"),
])
def test_weighted_cli_rejects_missing_or_unknown_os(tmp_path: Path, args, message):
    _tree(tmp_path, {"tests/test_a.py": 1})
    _weights(tmp_path, {"os": {"tests/test_a.py": 1}})
    result = _run(tmp_path, "--shard", "1", *args)
    assert result.returncode != 0
    assert message in result.stderr.lower()


def test_weighted_shards_cover_discovery_and_ignore_stale_entries(tmp_path: Path):
    _tree(tmp_path, {"tests/test_a.py": 1, "tests/nested/test_b.py": 2,
                     "tests/test_new.py": 4})
    table = _weights(tmp_path, {"os": {"tests/test_a.py": 20, "tests/nested/test_b.py": 0,
                                       "tests/test_gone.py": 500}})
    groups = _weighted_shards(tmp_path, table, "os")
    paths = [path for group in groups for path in group]
    assert sorted(paths) == [path for path, _ in _load().discover(tmp_path)]
    assert len(paths) == len(set(paths))


def test_weighted_shards_ignore_table_key_order_across_processes(tmp_path: Path):
    _tree(tmp_path, {f"tests/test_{name}.py": 1 for name in "abcd"})
    entries = [(f"tests/test_{name}.py", 200) for name in "abcd"]
    table = _weights(tmp_path, {"os": dict(entries)})
    first = _weighted_shards(tmp_path, table, "os")
    other = tmp_path / "other"
    other.mkdir()
    reverse_table = _weights(other, {"os": dict(reversed(entries))})
    assert _weighted_shards(tmp_path, reverse_table, "os") == first == [
        ["tests/test_a.py", "tests/test_c.py"], ["tests/test_b.py", "tests/test_d.py"]]


@pytest.mark.skipif(os.name == "nt", reason="the workflow step runs under POSIX Bash here")
def test_portable_step_uses_the_matrix_os_weights(tmp_path: Path):
    _tree(tmp_path, {"tests/test_a.py": 1, "tests/test_b.py": 3, "tests/test_c.py": 10})
    workflow = yaml.load((SCRIPT.parents[2] / ".github/workflows/tests.yml").read_text(
        encoding="utf-8"), Loader=yaml.BaseLoader)
    job = workflow["jobs"]["pytest-portable"]
    systems = job["strategy"]["matrix"]["os"]
    assert len(systems) >= 2
    weight_maps = {}
    for index, system in enumerate(systems):
        values = (100, 90, 80) if index % 2 == 0 else (80, 90, 100)
        weight_maps[system] = dict(zip((f"tests/test_{c}.py" for c in "abc"), values))
    table = tmp_path / "scripts/ci/pytest_shard_weights.json"
    table.parent.mkdir(parents=True)
    table.write_text(json.dumps({"run_id": "123", "weights": weight_maps}),
                     encoding="utf-8", newline="\n")
    fake = tmp_path / "python"
    fake.write_text('#!/bin/sh\nif [ "$1" = scripts/ci/pytest_shards.py ]; then\n'
                    '  shift; exec "$REAL" "$SHARD_SCRIPT" "$@"\nfi\n'
                    'printf "%s\\n" "$@" > "$ARGV"\n', encoding="utf-8", newline="\n")
    fake.chmod(0o755)
    step, = (item for item in job["steps"] if item.get("name") == "Run tests")
    env = dict(os.environ, PATH=f'{tmp_path}{os.pathsep}{os.environ["PATH"]}',
               REAL=sys.executable, SHARD_SCRIPT=str(SCRIPT), ARGV=str(tmp_path / "argv"))
    for index, system in enumerate(systems):
        command = step["run"].replace("${{ matrix.shard }}", "1").replace(
            "${{ matrix.os }}", system)
        result = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", command],
                                cwd=tmp_path, env=env, capture_output=True, text=True,
                                check=False, timeout=30)
        assert result.returncode == 0, result.stderr
        arguments = (tmp_path / "argv").read_text(encoding="utf-8").splitlines()
        expected = "a" if index % 2 == 0 else "c"
        assert arguments[2:-9] == [f"tests/test_{expected}.py"]


def _report(root: Path, name: str, cases: str) -> None:
    artifact = root / name
    artifact.mkdir(parents=True)
    (artifact / "pytest-junit.xml").write_text(f"<testsuite>{cases}</testsuite>",
                                               encoding="utf-8", newline="\n")


def _record(root: Path, reports: Path):
    return _run(root, "--record", str(reports), "--run-id", "123",
                "--output", "recorded.json")


@pytest.mark.parametrize("extra", [("--weights", "weights.json"), ("--os", "os")])
def test_record_also_rejects_unpaired_weight_options(tmp_path: Path, extra):
    _tree(tmp_path, {"tests/test_a.py": 1})
    reports = tmp_path / "reports"
    _report(reports, "pytest-junit-ubuntu-latest-3.14-shard-1", "")
    result = _run(tmp_path, "--record", str(reports), "--run-id", "123",
                  "--output", "recorded.json", *extra)
    assert result.returncode != 0
    assert "together" in result.stderr
    assert not (tmp_path / "recorded.json").exists()


def test_record_sums_os_cells_and_maps_longest_classname_prefix(tmp_path: Path):
    _tree(tmp_path, {"tests/test_a.py": 1, "tests/test_a/test_inner.py": 1,
                     "tests/test_unused.py": 1})
    reports = tmp_path / "reports"
    for os_name, seconds in (("ubuntu-latest", "0.125"), ("windows-latest", "0.225")):
        for version in ("3.14", "3.15"):
            _report(reports, f"pytest-junit-{os_name}-{version}-shard-1",
                    f'<testcase classname="tests.test_a.TestY" time="{seconds}"/>')
            _report(reports, f"pytest-junit-{os_name}-{version}-shard-2",
                    '<testcase classname="tests.test_a.test_inner.TestY" time="0.050">'
                    '<skipped/></testcase>')
    result = _record(tmp_path, reports)
    assert result.returncode == 0, result.stderr
    recorded = json.loads((tmp_path / "recorded.json").read_text(encoding="utf-8"))
    assert recorded == {"run_id": "123", "weights": {
        "ubuntu-latest": {"tests/test_a.py": 250, "tests/test_a/test_inner.py": 100,
                          "tests/test_unused.py": 0},
        "windows-latest": {"tests/test_a.py": 450, "tests/test_a/test_inner.py": 100,
                           "tests/test_unused.py": 0},
    }}


@pytest.mark.parametrize("directory, classname, problem", [
    ("pytest-junit-ubuntu-latest-3.14-shard-1", "tests.not_a_file.TestY", "tests.not_a_file.TestY"),
    ("bad-artifact", "tests.test_a", "bad-artifact"),
])
def test_record_refuses_unmappable_cases_and_unparseable_artifacts(
        tmp_path: Path, directory: str, classname: str, problem: str):
    _tree(tmp_path, {"tests/test_a.py": 1})
    reports = tmp_path / "reports"
    _report(reports, directory, f'<testcase classname="{classname}" time="0.010"/>')
    result = _record(tmp_path, reports)
    assert result.returncode != 0
    assert problem in result.stderr
    assert not (tmp_path / "recorded.json").exists()


def test_record_accepts_every_artifact_name_emitted_by_portable_matrix(tmp_path: Path):
    _tree(tmp_path, {"tests/test_a.py": 1})
    workflow = yaml.load((SCRIPT.parents[2] / ".github/workflows/tests.yml").read_text(
        encoding="utf-8"), Loader=yaml.BaseLoader)
    job = workflow["jobs"]["pytest-portable"]
    matrix = job["strategy"]["matrix"]
    upload, = (step for step in job["steps"]
               if step.get("uses", "").startswith("actions/upload-artifact@"))
    template = upload["with"]["name"]
    reports = tmp_path / "reports"
    for os_name in matrix["os"]:
        for version in matrix["python-version"]:
            for shard in matrix["shard"]:
                name = template.replace("${{ matrix.os }}", os_name).replace(
                    "${{ matrix.python-version }}", version).replace("${{ matrix.shard }}", shard)
                _report(reports, name, "")
    result = _record(tmp_path, reports)
    assert result.returncode == 0, result.stderr
    recorded = json.loads((tmp_path / "recorded.json").read_text(encoding="utf-8"))
    assert set(recorded["weights"]) == set(matrix["os"])
