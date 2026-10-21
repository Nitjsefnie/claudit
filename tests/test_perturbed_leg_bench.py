"""The perturbed-data leg's attribution bench under test.

These tests drive ``scripts/ci/perturbed_leg_bench.py`` over a MINIMAL
synthetic pricing.json, constants module and parser-version module in
tmp_path, over a one-test pytest subprocess, never the real tree: the
tree's own pricing.json is never mutated. The end-to-end runs are
POSIX-only -- the bench reads child CPU from ``getrusage`` -- and the
report-shape and wiring tests run everywhere.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from tests.refresh_fixture_builders import seed_doc

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ci" / "perturbed_leg_bench.py"

RATES = {"fresh": 1.0, "create_5m": 1.25, "create_1h": 2.0,
         "read": 0.1, "output": 5.0}

POSIX = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the bench's child-CPU instrument is getrusage(2), POSIX-only")


def _load():
    spec = importlib.util.spec_from_file_location(
        "perturbed_leg_bench", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["perturbed_leg_bench"] = module
    spec.loader.exec_module(module)
    return module


bench = _load()


def _seed_tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    pricing_path = tmp_path / "pricing.json"
    constants_path = tmp_path / "constants.py"
    parser_path = tmp_path / "parser_version.py"
    doc = seed_doc(models={"acme/acme-9": [{"from": None, **RATES}]},
                   fetched="2026-06-01T00:00:00Z")
    pricing_path.write_text(
        json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    constants_path.write_text(
        '"""Synthetic constants."""\n'
        'PRICING_VERSION = "5"\n'
        'MARKER_READER_VERSION = "1"\n',
        encoding="utf-8")
    parser_path.write_text(
        '"""Synthetic parser version."""\n'
        'PARSER_VERSION = "81"\n',
        encoding="utf-8")
    return pricing_path, constants_path, parser_path


def _mini_suite(tmp_path: Path, *, passing: bool = True) -> list[str]:
    body = ("def test_one():\n"
            "    assert True\n" if passing else
            "def test_one():\n"
            "    assert False\n")
    mini = tmp_path / "test_mini.py"
    mini.write_text(body, encoding="utf-8")
    return [str(mini), "-q", "--tb=no", "-p", "no:cacheprovider"]


@POSIX
def test_a_passing_run_records_both_phases(tmp_path):
    pricing_path, constants_path, parser_path = _seed_tree(tmp_path)
    args = _mini_suite(tmp_path)
    measurement = bench.measure([42], pricing_path, constants_path,
                                parser_path, args)
    assert measurement["instrument"] == bench.INSTRUMENT
    (seed_run,) = measurement["seeds"]
    assert seed_run["seed"] == 42
    assert set(seed_run["phases"]) == {"perturb", "suite"}
    for phase in seed_run["phases"].values():
        assert Decimal(phase["wall_s"]) >= Decimal("0.0")
        assert Decimal(phase["cpu_s"]) >= Decimal("0.0")
        # The string contract: one decimal place, never a float or a
        # wider spelling.
        assert isinstance(phase["wall_s"], str)
        assert isinstance(phase["cpu_s"], str)
        assert re.fullmatch(r"\d+\.\d", phase["wall_s"])
        assert re.fullmatch(r"\d+\.\d", phase["cpu_s"])
        assert isinstance(phase["exit_code"], int)
    assert seed_run["phases"]["suite"]["exit_code"] == 0
    assert seed_run["phases"]["perturb"]["exit_code"] == 0
    assert measurement["pytest_args"] == args
    assert measurement["tests_tree_lines"] > 0


@POSIX
def test_two_seeds_sum_and_start_pristine(tmp_path):
    pricing_path, constants_path, parser_path = _seed_tree(tmp_path)
    args = _mini_suite(tmp_path)
    measurement = bench.measure([42, 43], pricing_path, constants_path,
                                parser_path, args)
    assert [run["seed"] for run in measurement["seeds"]] == [42, 43]
    for phase in bench.PHASES:
        for column in ("wall_s", "cpu_s"):
            total = Decimal(measurement["totals"][phase][column])
            per_seed = sum(
                (Decimal(run["phases"][phase][column])
                 for run in measurement["seeds"]), Decimal("0"))
            assert total == per_seed
    # The last seed's perturbation is the only one on disk: exactly
    # five appended entries per row, not ten -- seed 43 did not
    # perturb seed 42's tree.
    doc = json.loads(pricing_path.read_text(encoding="utf-8"))
    entries = doc["models"]["acme/acme-9"]
    assert len(entries) == 6  # 1 real + 5 appended
    assert len(doc["models"]["claude-opus-4-7"]) == 6, "every row perturbs"
    constants_text = constants_path.read_text(encoding="utf-8")
    assert 'PRICING_VERSION = "6"' in constants_text  # 5 + one bump
    parser_text = parser_path.read_text(encoding="utf-8")
    assert 'PARSER_VERSION = "82"' in parser_text  # 81 + one bump


@POSIX
def test_the_exit_is_zero_when_every_seed_passes(tmp_path):
    pricing_path, constants_path, parser_path = _seed_tree(tmp_path)
    args = _mini_suite(tmp_path)
    code = bench.main([
        "--seed", "42",
        "--pricing", str(pricing_path), "--constants", str(constants_path),
        "--parser-version", str(parser_path),
        "--pytest-args", " ".join(args),
    ])
    assert code == 0


@POSIX
def test_a_failing_suite_stops_the_run_and_is_the_exit(tmp_path):
    pricing_path, constants_path, parser_path = _seed_tree(tmp_path)
    args = _mini_suite(tmp_path, passing=False)
    code = bench.main([
        "--seed", "42", "--seed", "43",
        "--pricing", str(pricing_path), "--constants", str(constants_path),
        "--parser-version", str(parser_path),
        "--pytest-args", " ".join(args),
        "--measurement", str(tmp_path / "leg-measurement.json"),
    ])
    assert code == 1
    measurement = json.loads(
        (tmp_path / "leg-measurement.json").read_text(encoding="utf-8"))
    assert [run["seed"] for run in measurement["seeds"]] == [42]


def test_the_default_pytest_args_are_the_legs_historical_invocation():
    # Literal pin: the workflow passes no --pytest-args (the wiring pin
    # below asserts that), so this constant IS what the leg runs — the
    # removed loop's exact invocation.
    assert bench.DEFAULT_PYTEST_ARGS == ["tests/", "-q", "--tb=short", "-ra"]


def test_a_perturb_failure_names_it_and_is_nonzero(tmp_path, capsys):
    pricing_path, constants_path, parser_path = _seed_tree(tmp_path)
    pricing_path.write_text(
        json.dumps({"models": {}, "providers": {}}), encoding="utf-8")
    code = bench.main([
        "--seed", "42",
        "--pricing", str(pricing_path), "--constants", str(constants_path),
        "--parser-version", str(parser_path),
        "--pytest-args", " ".join(_mini_suite(tmp_path)),
    ])
    assert code == 1
    # The "names it" half: the handler prints the perturbation's own
    # error, naming the document, before returning nonzero.
    captured = capsys.readouterr()
    assert "no rate rows" in captured.err
    assert str(pricing_path) in captured.err


def _synthetic_measurement() -> dict:
    return {
        "instrument": bench.INSTRUMENT,
        "note": bench.TELEMETRY_NOTE,
        "pytest_args": ["tests/", "-q"],
        "tests_tree_lines": 12345,
        "seeds": [{"seed": 42, "phases": {
            "perturb": {"wall_s": "0.2", "cpu_s": "0.2", "exit_code": 0},
            "suite": {"wall_s": "421.0", "cpu_s": "376.4",
                      "exit_code": 0}}}],
        "totals": {"perturb": {"wall_s": "0.2", "cpu_s": "0.2"},
                   "suite": {"wall_s": "421.0", "cpu_s": "376.4"}},
    }


def test_the_report_names_every_seed_phase_and_column():
    text = bench.report(_synthetic_measurement())
    for needle in ("42", "perturb", "suite", "wall_s", "cpu_s",
                   "exit_code", "421.0", "12345"):
        assert needle in text
    assert "telemetry" in text


def test_the_summary_markdown_carries_the_table():
    text = bench.summary_markdown(_synthetic_measurement())
    assert text.startswith("### Perturbed leg attribution")
    for needle in ("| seed | phase |", "| 42 | perturb |", "| 42 | suite |",
                   "| total | perturb |", "| total | suite |",
                   "exit_code", "12345"):
        assert needle in text


def _workflow_doc():
    return yaml.load(
        (ROOT / ".github" / "workflows" / "test-data.yml").read_text(
            encoding="utf-8"), Loader=yaml.BaseLoader) or {}


def test_the_leg_runs_one_seed_and_shard_per_matrix_job():
    job = _workflow_doc()["jobs"]["perturbed-suite"]
    strategy = job.get("strategy") or {}
    # Every seed reports its own verdict (option A): the
    # seeds are independent trees, so fail-fast's cancellation would
    # discard a seed still to run.
    assert strategy.get("fail-fast") == "false", strategy
    # The fixed seeds live in the matrix now, so this is
    # where the leg's seed policy is pinned: a dropped or added seed
    # fails here.
    assert (strategy.get("matrix") or {}).get("seed") == [
        "42", "1234567890", "1720000000"], strategy
    assert strategy["matrix"].get("shard") == ["1", "2", "3", "4"]
    assert job["timeout-minutes"] == "25"
    assert job["name"] == (
        "perturbed suite (seed ${{ matrix.seed }}, shard ${{ matrix.shard }} of 4)")
    save, = [step for step in job["steps"] if step.get("name") == "Save the pip cache"]
    assert save["if"] == (
        "github.event_name == 'push' && github.ref == 'refs/heads/master' "
        "&& matrix.seed == '42' && matrix.shard == 1")
    steps = [step for step in job["steps"]
             if "perturbed_leg_bench" in (step.get("run") or "")]
    assert len(steps) == 1
    # The shell folds backslash continuations into one command: join
    # them before reading commands, so the pin sees what the runner
    # sees.
    joined = steps[0]["run"].replace("\\\n", " ")
    lines = [line.strip() for line in joined.splitlines() if line.strip()]
    assert len(lines) == 1
    # Exact command equality: a job still looping the seeds itself, a
    # dropped --summary, or a trailing `|| true` (which would stop the
    # bench's exit being the step's) all fail this pin. The
    # attribution lands in the step summary, and the bench's exit code
    # is the step's own.
    assert " ".join(lines[0].split()) == (
        "python scripts/ci/perturbed_leg_bench.py "
        '--seed "${{ matrix.seed }}" '
        '--shard "${{ matrix.shard }}" --of 4 '
        '--measurement "perturbed-${{ matrix.seed }}-${{ matrix.shard }}.json" '
        '--summary "$GITHUB_STEP_SUMMARY"')
    assert "--pytest-args" not in lines[0]


def test_the_leg_step_no_longer_runs_pytest_directly():
    job = _workflow_doc()["jobs"]["perturbed-suite"]
    for step in job["steps"]:
        assert "python -m pytest" not in (step.get("run") or "")


@POSIX
@pytest.mark.parametrize("shard,expected", [
    (None, ["a", "b", "c", "d"]), (1, ["a", "d"]), (2, ["b", "c"]),
])
def test_cli_selects_exact_shard_files(tmp_path, monkeypatch, capsys, shard, expected):
    pricing_path, constants_path, parser_path = _seed_tree(tmp_path)
    (tmp_path / "tests").mkdir()
    for index, name in enumerate("abcd"):
        (tmp_path / "tests" / f"test_{name}.py").write_text(
            f"def test_{name}():\n"
            f"    with open('executed', 'a', encoding='utf-8') as stream:\n"
            f"        stream.write('{name}\\n')\n"
            "    import os\n"
            "    assert os.environ['CLAUDIT_PERTURBED_DATA'] == '1'\n"
            + "\n" * (3 - index), encoding="utf-8")
    monkeypatch.setattr(bench, "REPO_ROOT", tmp_path)
    measurement_path = tmp_path / "measurement.json"
    summary_path = tmp_path / "summary.md"
    args = ["--seed", "42", "--pricing", str(pricing_path),
            "--constants", str(constants_path), "--parser-version", str(parser_path),
            "--measurement", str(measurement_path), "--summary", str(summary_path)]
    if shard is not None:
        args += ["--shard", str(shard), "--of", "2"]
    assert bench.main(args) == 0
    assert sorted((tmp_path / "executed").read_text(encoding="utf-8").splitlines()) == expected
    measurement = json.loads(measurement_path.read_text(encoding="utf-8"))
    flags = ["-q", "--tb=short", "-ra"]
    assert measurement["pytest_args"] == (
        ["tests/", *flags] if shard is None else
        [*(f"tests/test_{name}.py" for name in expected), *flags])
    assert measurement["tests_tree_lines"] == 26
    if shard is not None:
        assert (measurement["shard"], measurement["of"]) == (shard, 2)
        assert f"shard {shard} of 2" in capsys.readouterr().out
        assert f"shard {shard} of 2" in summary_path.read_text(encoding="utf-8")


def test_combine_runs_after_failed_shards_and_publishes_measurements():
    jobs = _workflow_doc()["jobs"]
    suite = jobs["perturbed-suite"]
    upload, = [step for step in suite["steps"] if step.get("name") == "Upload the shard measurement"]
    assert upload["if"] == "${{ !cancelled() }}"
    assert "continue-on-error" not in suite
    assert all("continue-on-error" not in step for step in suite["steps"])
    combine = jobs["perturbed-combine"]
    assert combine["needs"] == ["perturbed-suite"]
    assert combine["if"] == "${{ !cancelled() }}"
    step, = [step for step in combine["steps"] if "run" in step]
    assert " ".join(step["run"].replace("\\\n", " ").split()) == (
        "python scripts/ci/perturbed_leg_combine.py --measurements shard-data "
        "--seed 42 --seed 1234567890 --seed 1720000000 --of 4 "
        '--summary "$GITHUB_STEP_SUMMARY"')
