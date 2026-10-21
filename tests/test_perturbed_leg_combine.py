"""Combine synthetic shard telemetry without running the real suite."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/ci/perturbed_leg_combine.py"


def _measurement(seed: int, shard: int, *, code: int = 0, cpu: str = "2.4") -> dict:
    return {
        "instrument": "wall_and_cpu", "tests_tree_lines": 123,
        "shard": shard, "of": 2,
        "seeds": [{"seed": seed, "phases": {
            "perturb": {"wall_s": "0.2", "cpu_s": "0.1", "exit_code": 0},
            "suite": {"wall_s": "3.1", "cpu_s": cpu, "exit_code": code}}}],
    }


def _run(tmp_path: Path, measurements: list[dict]) -> tuple[subprocess.CompletedProcess, str]:
    directory = tmp_path / "measurements"
    directory.mkdir()
    for index, doc in enumerate(measurements):
        artifact = directory / f"artifact-{index}"
        artifact.mkdir()
        (artifact / "measurement.json").write_text(json.dumps(doc), encoding="utf-8")
    summary = tmp_path / "step-summary.md"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--measurements", str(directory),
         "--seed", "42", "--seed", "43", "--of", "2", "--summary", str(summary)],
        capture_output=True, text=True, check=False, timeout=30)
    return proc, summary.read_text(encoding="utf-8") if summary.exists() else ""


@pytest.mark.parametrize("code", [0, 1, 5])
def test_per_seed_totals_and_verdict_include_every_shard(tmp_path, code):
    proc, summary = _run(tmp_path, [
        _measurement(43, 2), _measurement(42, 1),
        _measurement(43, 1), _measurement(42, 2, code=code)])
    assert proc.returncode == (1 if code else 0), proc.stderr
    assert "| 42 | perturb | 0.4 | 0.2 | 0 |" in summary
    assert f"| 42 | suite | 6.2 | 4.8 | {code} |" in summary
    assert "| 43 | suite | 6.2 | 4.8 | 0 |" in summary
    assert f"| 42 | {'FAIL' if code else 'PASS'} |" in summary
    assert "| 43 | PASS |" in summary
    assert f"| 42 | 2 of 2 | suite | 3.1 | 2.4 | {code} |" in summary
    assert "tests_tree_lines: 123" in summary
    assert "sum of shard wall times" in summary
    assert summary in proc.stdout


def test_unmeasured_cpu_stays_unmeasured(tmp_path):
    proc, summary = _run(tmp_path, [
        _measurement(seed, shard, cpu="-") for seed in (42, 43) for shard in (1, 2)])
    assert proc.returncode == 0, proc.stderr
    assert "| 42 | suite | 6.2 | - | 0 |" in summary


@pytest.mark.parametrize("missing", [False, True])
def test_missing_measurements_cannot_report_a_passing_seed(tmp_path, missing):
    measurements = [] if missing else [_measurement(42, 1)]
    proc, summary = _run(tmp_path, measurements)
    assert proc.returncode == 1, proc.stderr
    assert "| 42 | FAIL |" in summary
    assert "| 43 | FAIL |" in summary
    assert "missing shards" in summary


@pytest.mark.parametrize("problem", ["duplicate", "workload", "shard_count", "seed"])
def test_inconsistent_measurements_are_refused(tmp_path, problem):
    docs = [_measurement(seed, shard) for seed in (42, 43) for shard in (1, 2)]
    if problem == "duplicate":
        docs.append(docs[0])
    elif problem == "workload":
        docs[0]["tests_tree_lines"] += 1
    elif problem == "shard_count":
        docs[0]["of"] = 3
    else:
        docs[0]["seeds"][0]["seed"] = 44
    proc, _ = _run(tmp_path, docs)
    assert proc.returncode == 1, proc.stderr
    assert "::error::" in proc.stderr
