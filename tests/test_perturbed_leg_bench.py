"""The perturbed-data leg's attribution bench (issue #456) under test.

These tests drive ``scripts/ci/perturbed_leg_bench.py`` over a MINIMAL
synthetic pricing.json and constants module in tmp_path, over a
one-test pytest subprocess, never the real tree: the tree's own
pricing.json is never mutated. The end-to-end runs are POSIX-only --
the bench reads child CPU from ``getrusage`` -- and the report-shape
and wiring tests run everywhere.
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


def _seed_tree(tmp_path: Path) -> tuple[Path, Path]:
    pricing_path = tmp_path / "pricing.json"
    constants_path = tmp_path / "constants.py"
    doc = {
        "models": {"acme/acme-9": [{"from": None, **RATES}]},
        "providers": {},
        "provider_rates_fetched": "2026-06-01T00:00:00Z",
        "long_context_models": [],
        "openrouter": {"data_region": "global", "models": {}},
    }
    pricing_path.write_text(
        json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    constants_path.write_text(
        '"""Synthetic constants."""\n'
        'PARSER_VERSION = "81"\n'
        'PRICING_VERSION = "5"\n'
        'MARKER_READER_VERSION = "1"\n',
        encoding="utf-8")
    return pricing_path, constants_path


def _mini_suite(tmp_path: Path, *, passing: bool = True) -> list[str]:
    body = ("def test_one():\n"
            "    assert True\n" if passing else
            "def test_one():\n"
            "    assert False\n")
    mini = tmp_path / "test_mini.py"
    mini.write_text(body, encoding="utf-8")
    return [str(mini), "-q", "--tb=no", "-p", "no:cacheprovider"]


# --- the measurement's shape --------------------------------------------------


@POSIX
def test_a_passing_run_records_both_phases(tmp_path):
    pricing_path, constants_path = _seed_tree(tmp_path)
    args = _mini_suite(tmp_path)
    measurement = bench.measure([42], pricing_path, constants_path, args)
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
    pricing_path, constants_path = _seed_tree(tmp_path)
    args = _mini_suite(tmp_path)
    measurement = bench.measure([42, 43], pricing_path, constants_path, args)
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
    (entries,) = doc["models"].values()
    assert len(entries) == 6  # 1 real + 5 appended
    constants_text = constants_path.read_text(encoding="utf-8")
    assert 'PRICING_VERSION = "6"' in constants_text  # 5 + one bump


@POSIX
def test_the_exit_is_zero_when_every_seed_passes(tmp_path):
    pricing_path, constants_path = _seed_tree(tmp_path)
    args = _mini_suite(tmp_path)
    code = bench.main([
        "--seed", "42",
        "--pricing", str(pricing_path), "--constants", str(constants_path),
        "--pytest-args", " ".join(args),
    ])
    assert code == 0


@POSIX
def test_a_failing_suite_stops_the_run_and_is_the_exit(tmp_path):
    pricing_path, constants_path = _seed_tree(tmp_path)
    args = _mini_suite(tmp_path, passing=False)
    code = bench.main([
        "--seed", "42", "--seed", "43",
        "--pricing", str(pricing_path), "--constants", str(constants_path),
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
    pricing_path, constants_path = _seed_tree(tmp_path)
    pricing_path.write_text(
        json.dumps({"models": {}, "providers": {}}), encoding="utf-8")
    code = bench.main([
        "--seed", "42",
        "--pricing", str(pricing_path), "--constants", str(constants_path),
        "--pytest-args", " ".join(_mini_suite(tmp_path)),
    ])
    assert code == 1
    # The "names it" half: the handler prints the perturbation's own
    # error, naming the document, before returning nonzero.
    captured = capsys.readouterr()
    assert "no rate rows" in captured.err
    assert str(pricing_path) in captured.err


# --- the reports -----------------------------------------------------------------


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


# --- the CI wiring ---------------------------------------------------------------


def _workflow_doc():
    return yaml.load(
        (ROOT / ".github" / "workflows" / "test-data.yml").read_text(
            encoding="utf-8"), Loader=yaml.BaseLoader) or {}


def test_the_leg_step_calls_the_bench_with_the_fixed_seeds():
    (job,) = _workflow_doc()["jobs"].values()
    steps = [step for step in job["steps"]
             if "perturbed_leg_bench" in (step.get("run") or "")]
    assert len(steps) == 1
    # The shell folds backslash continuations into one command: join
    # them before reading commands, so the pin sees what the runner
    # sees.
    joined = steps[0]["run"].replace("\\\n", " ")
    lines = [line.strip() for line in joined.splitlines() if line.strip()]
    assert len(lines) == 1
    # Exact command equality: a dropped seed, a dropped --summary, or a
    # trailing `|| true` (which would stop the bench's exit being the
    # step's) all fail this pin. The attribution lands in the step
    # summary, and the bench's exit code is the step's own.
    assert " ".join(lines[0].split()) == (
        "python scripts/ci/perturbed_leg_bench.py "
        "--seed 42 --seed 1234567890 --seed 1720000000 "
        '--summary "$GITHUB_STEP_SUMMARY"')
    # The default is the production invocation: no override here.
    assert "--pytest-args" not in lines[0]


def test_the_leg_step_no_longer_runs_pytest_directly():
    (job,) = _workflow_doc()["jobs"].values()
    for step in job["steps"]:
        assert "python -m pytest" not in (step.get("run") or "")
