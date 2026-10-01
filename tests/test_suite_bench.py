"""Tests for the suite instruction-count bench.

The bench is driven as a SUBPROCESS over a synthetic mini pytest suite in
tmp_path (its own conftest.py, not the repo's), because the bench
re-execs itself for PYTHONHASHSEED and measures in-process pytest runs:
exactly the path CI takes. The cases pin the partition (every phase
counted, residual never negative), determinism across hash seeds, the
gate's fail-closed behaviour on a counts-less measurement, and that no
wall-clock value can reach a gating decision.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from tests import git_meta

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_CI = REPO_ROOT / "scripts" / "ci"
BENCH = SCRIPTS_CI / "suite_bench.py"

MINI_CONFTEST = '''\
"""The mini suite's own shared fixture."""
import pytest


@pytest.fixture
def doubled():
    return 2
'''

MINI_TEST_A = '''\
def test_double(doubled):
    assert doubled * 1 == doubled


def test_sum():
    assert sum(range(100)) == 4950


def test_heavy():
    # Enough real bytecode work that the run phase's count clears the
    # 1.5-unit gap and a recorded value one step cheaper can fail the
    # gate: the gate-failure case needs a budget that can lose.
    total = 0
    for _ in range(20000):
        total += sum(range(100))
    assert total == 4950 * 20000
'''

MINI_TEST_B = '''\
def test_reverse():
    assert list(reversed("abc")) == ["c", "b", "a"]
'''


@pytest.fixture(name="mini_suite")
def mini_suite_fixture(tmp_path):
    """A synthetic mini pytest suite with its own conftest and bench
    fixture list, shaped like the real one."""
    (tmp_path / "conftest.py").write_text(MINI_CONFTEST, encoding="utf-8")
    (tmp_path / "mini_test_a.py").write_text(MINI_TEST_A, encoding="utf-8")
    (tmp_path / "mini_test_b.py").write_text(MINI_TEST_B, encoding="utf-8")
    fixture = tmp_path / "bench_files.txt"
    fixture.write_text(
        "# the mini suite's pinned bench files\n"
        "mini_test_a.py\n"
        "mini_test_b.py\n",
        encoding="utf-8")
    return tmp_path


def _run_bench(args, cwd, env=None):
    return subprocess.run(  # pylint: disable=subprocess-run-check
        [sys.executable, str(BENCH), *args],
        cwd=str(cwd), env=env, capture_output=True, text=True, check=False,
        timeout=600)


def _measure(mini_suite, name="m.json", seed=None):
    env = dict(os.environ)
    if seed is not None:
        env["PYTHONHASHSEED"] = seed
    result = _run_bench(
        ["--write", name, "--repo-root", str(mini_suite),
         "--fixture", str(mini_suite / "bench_files.txt")],
        cwd=mini_suite, env=env)
    assert result.returncode == 0, result.stderr
    return json.loads((mini_suite / name).read_text(encoding="utf-8"))


def _thresholds_for(tmp_path, measurement, lift=Decimal("0.0")):
    """A thresholds document whose suite budgets sit `lift` against the
    measurement: lift 0.0 puts every ceiling exactly the gap above the
    measured value (the seed shape, the gate must accept); lift -1.6
    puts each ceiling one increment BELOW a phase (the gate must
    refuse). A recorded value is clamped at 0.0 -- the loader's
    non-negative bound -- so a phase whose measurement cannot cover the
    gap passes honestly rather than being forced under."""
    path = SCRIPTS_CI / "thresholds.py"
    spec = importlib.util.spec_from_file_location("thresholds", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["thresholds"] = module
    spec.loader.exec_module(module)
    doc = {
        "schema_version": 1,
        "coverage": {
            "python": {"measured": Decimal("92.6"),
                       "floor": Decimal("91.1")},
            "javascript": {"measured": 50.0, "floor": 48.5},
        },
        "module_size_baseline": {},
        "pylint_suppression_baseline": {},
        "suite_cost": {
            phase: {
                "measured": max(
                    Decimal(measurement["phases"][phase]
                            ["million_instructions"]) + lift,
                    Decimal("0.0")).quantize(Decimal("0.1")),
                "floor": (max(
                    Decimal(measurement["phases"][phase]
                            ["million_instructions"]) + lift,
                    Decimal("0.0")).quantize(Decimal("0.1"))
                    + module.CALIBRATION_GAP),
            }
            for phase in ("collection", "run", "residual")
        },
        # The loader requires EVERY top-level family, so a synthetic
        # suite_cost document must also carry a valid reparse family.
        # It is inert here — nothing in this module reads it.
        "reparse": {
            phase: {
                metric: {
                    "measured": Decimal("10.0"),
                    "floor": Decimal("10.0") + module.CALIBRATION_GAP,
                }
                for metric in module.REPARSE_METRICS
            }
            for phase in module.REPARSE_PHASES
        },
    }
    target = tmp_path / "thresholds.json"
    module.write(target, doc)
    return target


def test_every_phase_is_counted_above_zero(mini_suite):
    measurement = _measure(mini_suite)
    assert measurement["instrument"] == "instruction_count"
    assert measurement["unit"] == "million_instructions"
    counts = {phase: Decimal(
        measurement["phases"][phase]["million_instructions"])
        for phase in ("collection", "run", "residual")}
    assert all(count > 0 for count in counts.values()), counts
    # The file's numbers are STRINGS carrying exactly one decimal place:
    # a float would round-trip them to whatever the nearest double
    # spells, and the recorded value has to be the value measured.
    for phase in ("collection", "run", "residual"):
        spelling = measurement["phases"][phase]["million_instructions"]
        assert isinstance(spelling, str)
        assert spelling == f"{Decimal(spelling):.1f}"


def test_total_equals_the_phase_sum(mini_suite):
    measurement = _measure(mini_suite)
    total = Decimal(measurement["total_million_instructions"])
    phases = sum(Decimal(measurement["phases"][phase]
                         ["million_instructions"])
                 for phase in ("collection", "run", "residual"))
    assert total == phases


def test_two_runs_identical_counts_across_hash_seeds(mini_suite):
    # One subprocess per run, with DIFFERENT outer hash seeds: the
    # bench's PYTHONHASHSEED=0 re-exec makes both runs' counts equal,
    # which is what lets a seeded budget survive across CI runs.
    first = _measure(mini_suite, name="a.json", seed="1")
    second = _measure(mini_suite, name="b.json", seed="2")
    assert {phase: first["phases"][phase]["million_instructions"]
            for phase in ("collection", "run", "residual")} == {
        phase: second["phases"][phase]["million_instructions"]
        for phase in ("collection", "run", "residual")}
    assert (first["total_million_instructions"]
            == second["total_million_instructions"])


def test_failing_test_still_measures(mini_suite):
    # A failing test does not fail the bench: the tests gate owns that
    # verdict, and a non-passing test still retires its instructions.
    (mini_suite / "mini_test_bad.py").write_text(
        "def test_broken():\n    assert 1 == 2\n", encoding="utf-8")
    measurement = _measure(mini_suite, name="m.json")
    assert Decimal(
        measurement["phases"]["run"]["million_instructions"]) > 0


def test_collection_error_fails_the_run(mini_suite):
    # A file that cannot even be collected is a setup failure, not a
    # speed result: the bench refuses to write a measurement of a run
    # that never happened.
    (mini_suite / "mini_test_worse.py").write_text(
        "import no_such_module anywhere\n", encoding="utf-8")
    fixture = mini_suite / "broken_files.txt"
    fixture.write_text(
        "mini_test_a.py\nmini_test_worse.py\n", encoding="utf-8")
    result = _run_bench(
        ["--write", "m.json", "--repo-root", str(mini_suite),
         "--fixture", str(fixture)],
        cwd=mini_suite)
    assert result.returncode != 0
    assert "did not produce a usable run" in (result.stderr + result.stdout)


def test_check_passes_at_the_recorded_ceiling(mini_suite, tmp_path):
    measurement = _measure(mini_suite)
    target = _thresholds_for(tmp_path, measurement)
    result = _run_bench(
        ["--check", str(mini_suite / "m.json"), "--thresholds",
         str(target)], cwd=mini_suite)
    assert result.returncode == 0, result.stderr
    assert "within budget" in result.stdout


def test_check_fails_one_increment_over_a_phase(mini_suite, tmp_path):
    measurement = _measure(mini_suite)
    target = _thresholds_for(tmp_path, measurement, lift=Decimal("-1.6"))
    result = _run_bench(
        ["--check", str(mini_suite / "m.json"), "--thresholds",
         str(target)], cwd=mini_suite)
    assert result.returncode != 0
    expected_over = {
        phase for phase in ("collection", "run", "residual")
        if (Decimal(measurement["phases"][phase]["million_instructions"])
            > max(
                Decimal(measurement["phases"][phase]
                        ["million_instructions"]) - Decimal("1.6"),
                Decimal("0.0")) + Decimal("1.5"))}
    assert expected_over, "no phase went over: the case measures nothing"
    for phase in expected_over:
        value = measurement["phases"][phase]["million_instructions"]
        recorded = max(Decimal(value) - Decimal("1.6"),
                       Decimal("0.0")).quantize(Decimal("0.1"))
        ceiling = recorded + Decimal("1.5")
        over_line = (f"{phase}: {value} million_instructions, "
                     f"ceiling {ceiling}")
        assert over_line in result.stderr
    assert "never raised by hand" in result.stderr


def test_check_on_a_counts_less_measurement_fails_closed(mini_suite,
                                                         tmp_path):
    # The process_time fallback is telemetry: a measurement that is
    # ABSENT must never read as a measurement that passed.
    measurement = _measure(mini_suite)
    measurement["instrument"] = "process_time"
    for record in measurement["phases"].values():
        record["million_instructions"] = None
    (mini_suite / "m.json").write_text(
        json.dumps(measurement), encoding="utf-8")
    target = _thresholds_for(tmp_path, _measure(mini_suite, name="x.json"))
    result = _run_bench(
        ["--check", str(mini_suite / "m.json"), "--thresholds",
         str(target)], cwd=mini_suite)
    assert result.returncode != 0
    assert "no instruction count" in result.stderr


def test_partition_rejects_a_negative_residual():
    # sys.path first: the module's sibling import of thresholds must
    # resolve however this test is invoked (solo, file-wide, or from a
    # mutant runner), never through a sibling test's leftover seeding.
    sys.path.insert(0, str(SCRIPTS_CI))
    phases = SCRIPTS_CI / "suite_phases.py"
    spec = importlib.util.spec_from_file_location("suite_phases", phases)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["suite_phases"] = module
    spec.loader.exec_module(module)
    with pytest.raises(ValueError, match="instrument error"):
        module.partition(10, 6, 5)


def test_the_gate_modules_touch_no_wall_clock():
    # The only time-module member any bench module may name is
    # process_time — the portable fallback, telemetry only. A wall read
    # in the gate's path would make the verdict machine-speed-dependent
    # again, which is the regression this family exists to close. The
    # net is wider than the imports: ANY `<name>.<wall-member>` shape
    # and ANY bare wall-member call is flagged, imported or not — a
    # planted `time.perf_counter()` that forgot its own import must
    # still be caught (mutation M9).
    wall = {"perf_counter", "perf_counter_ns", "monotonic",
            "monotonic_ns", "time", "gmtime", "strftime"}
    seen = {}
    for name in ("suite_bench.py", "suite_phases.py", "suite_report.py",
                 "suite_ratchet.py"):
        tree = ast.parse(
            (SCRIPTS_CI / name).read_text(encoding="utf-8"))
        offenders = set()
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.attr in wall):
                offenders.add(node.attr)
            elif (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in wall):
                offenders.add(node.func.id)
        seen[name] = offenders
        assert not offenders, (name, offenders)
    # The instrument is really in use somewhere: the pin would also pass
    # a tree where process_time was quietly dropped from the fallback.
    source = " ".join(
        (SCRIPTS_CI / member).read_text(encoding="utf-8")
        for member in ("suite_bench.py", "suite_phases.py"))
    assert "time.process_time()" in source


def test_committed_fixture_files_exist_and_pass_marker():
    # Every path the committed fixture lists is a real test file of this
    # tree; the pin reads the list, not a copy.
    git_meta.require_own_git_metadata(REPO_ROOT)
    listed = [
        line.strip() for line in
        (SCRIPTS_CI / "suite_bench_files.txt").read_text(encoding="utf-8")
        .splitlines()
        if line.strip() and not line.strip().startswith("#")]
    assert listed, "the fixture list seeded empty"
    for rel in listed:
        assert rel.startswith("tests/"), rel
        assert (REPO_ROOT / rel).is_file(), rel


def test_summary_markdown_names_the_phases(mini_suite, tmp_path):
    summary = mini_suite / "summary.md"
    _run_bench(
        ["--write", "m.json", "--repo-root", str(mini_suite),
         "--fixture", str(mini_suite / "bench_files.txt"),
         "--summary", str(summary)], cwd=mini_suite)
    text = summary.read_text(encoding="utf-8")
    for phase in ("collection", "run", "residual"):
        assert phase in text
    assert "million_instructions" in text


def test_check_refuses_a_non_deterministic_measurement(mini_suite, tmp_path):
    # The committed budgets are exact counts of a PYTHONHASHSEED=0 run;
    # a measurement recorded under a randomized seed is not comparable
    # with them, and the gate refuses it rather than reading the dead
    # telemetry field as irrelevant.
    measurement = _measure(mini_suite)
    measurement["hash_seed"] = "randomized"
    (mini_suite / "m.json").write_text(
        json.dumps(measurement), encoding="utf-8")
    target = _thresholds_for(tmp_path, _measure(mini_suite, name="x.json"))
    result = _run_bench(
        ["--check", str(mini_suite / "m.json"), "--thresholds",
         str(target)], cwd=mini_suite)
    assert result.returncode != 0
    assert "hash seed" in result.stderr
    assert "PYTHONHASHSEED=0" in result.stderr


def test_fixture_paths_outside_the_repo_root_are_refused(tmp_path):
    # read_fixture's docstring claims an off-root path is a setup error;
    # the control makes the claim true: both a ../escape and an absolute
    # path (which the / operator adopts whole) must be refused.
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "escape.py"
    outside.write_text("def test_ok():\n    assert True\n",
                       encoding="utf-8")
    for needle in ("../escape.py", str(outside)):
        fixture = tmp_path / "bench_files.txt"
        fixture.write_text(needle + "\n", encoding="utf-8")
        result = _run_bench(
            ["--write", "m.json", "--repo-root", str(root),
             "--fixture", str(fixture)],
            cwd=tmp_path)
        assert result.returncode != 0, needle
        assert "outside" in (result.stderr + result.stdout), needle


def _purge_bytecode_caches(root):
    for cache in root.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def test_the_bench_compiles_every_pinned_file_before_it_counts(mini_suite):
    # The count must not depend on the caches the checkout arrived with:
    # compiling a module costs about as much again as loading it, and
    # the two callers did not agree on whether the compile had already
    # happened -- speed.yml measures a fresh checkout, tests.yml
    # measures after the suite has run. warm_bytecode_caches draws that
    # split outside the counted window, and this is the control: from a
    # tree carrying no bytecode caches at all, one measurement must
    # leave EVERY pinned file compiled. Without the warm-up the tree
    # would still be bare, and the gate would read one number while
    # the tighten bot wrote the other into the budgets (issue #496).
    #
    # The per-file assertion is what makes it bite: a warm-up that
    # collected only part of the fixture would compile a __pycache__ and
    # still pass a looser "some cache exists" check.
    _purge_bytecode_caches(mini_suite)
    assert not list(mini_suite.rglob("__pycache__"))
    _measure(mini_suite, name="warmed.json")
    compiled = mini_suite / "__pycache__"
    for pinned in ("mini_test_a", "mini_test_b", "conftest"):
        assert list(compiled.glob(f"{pinned}.cpython-*.pyc")), pinned


def test_repeated_measurements_from_a_purged_tree_agree(mini_suite):
    # Repeatability, and only that: two measurements each taken from a
    # PURGED tree must read alike in every phase. This is NOT the
    # bare-versus-inherited claim -- the bench draws its own compile/load
    # split before counting either run, so both start warm and that
    # comparison is structural, not something a unit test at this tree's
    # size can observe. What this does pin is that the warm-up leaves the
    # counted state stable run to run, which a warm whose effect varied
    # between invocations would break. process_time is telemetry and is
    # deliberately not compared.
    counts = []
    for name in ("bare.json", "inherited.json"):
        _purge_bytecode_caches(mini_suite)
        measurement = _measure(mini_suite, name=name)
        counts.append({phase: value['million_instructions']
                       for phase, value in measurement['phases'].items()})
    assert counts[1] == counts[0], f'bare {counts[0]} vs {counts[1]}'


def test_measure_releases_the_monitoring_tool_id(mini_suite):
    # close() is not optional: its docstring promises the tool id goes
    # back, and an in-process caller that measured once must not leave
    # the INSTRUCTION tax armed for the rest of its life. The control:
    # after measure() returns, a fresh counter can claim the tool id.
    sys.path.insert(0, str(SCRIPTS_CI))

    def _load(name, filename):
        spec = importlib.util.spec_from_file_location(
            name, SCRIPTS_CI / filename)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    bench = _load("suite_bench", "suite_bench.py")
    fixture = bench.read_fixture(
        mini_suite / "bench_files.txt", mini_suite)
    bench.measure(fixture, mini_suite)
    # A fresh CLASS under a fresh module name, so the check reads the
    # process-global tool id, not a cached module's attribute.
    fresh = _load("suite_phases_fresh", "suite_phases.py").InstructionCounter()
    try:
        assert fresh.available, fresh.reason
    finally:
        fresh.close()
