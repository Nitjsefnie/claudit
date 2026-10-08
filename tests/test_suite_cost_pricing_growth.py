"""Issue #840's check: the suite-cost bench's measurement does not move
when only rate data grows.

The incident (#872): a data-only pricing refresh grew ``src/pricing.json``
3.4x and the gate's residual phase grew with it, breaching its ceiling —
the counted workload's conftest-time ``backend.pricing`` import parses the
whole document. The fix routes the bench's counted workload through a
bounded document — the bench loads ``scripts/ci/suite_pricing_doc.json``
and rebinds the pricing tables before any counted window opens —
so this check runs the bench over a copy of the real suite machinery twice
— the deployed document, and the same document with synthetic rows
appended — and requires the phase counts to be EXACTLY equal.

Red on the scaling code: before the seam, both copies' benches parsed the
deployed document, and the appended rows moved the residual by their parse
cost. Nothing here reads rate values: the appended rows are loader-valid
synthetic hosts, and the comparison is between instruction counts.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "scripts" / "ci" / "suite_bench.py"
# The bench modules the counted child imports, beside the bench itself.
BENCH_MODULES = ("suite_bench.py", "suite_phases.py", "suite_report.py",
                 "thresholds.py")
# The committed document — the only tree file the check mutates in its
# copies — and the test-tree modules the copy's conftest imports.
DOC_RELATIVE = Path("src") / "pricing.json"  # sv-test-data: allow (the #840 check copies the deployed document and appends rows to the copy; the counts, never the rates, are asserted on)
CONFTEST_SIBLINGS = ("__init__.py", "conftest.py", "scratch_db.py")


def _build_copy(target: Path) -> Path:
    """A minimal tree the real bench can measure: backend/, the deployed
    document, the conftest trio, the bench modules and their thresholds
    document. Byte-identical between copies but for the document."""
    target.mkdir(parents=True)
    shutil.copytree(ROOT / "backend", target / "backend")
    shutil.copytree(ROOT / "tests", target / "tests",
                    ignore=shutil.ignore_patterns(
                        "__pycache__", "*.pyc", "test_*", ".pg"))
    (target / "scripts" / "ci").mkdir(parents=True)
    (target / "src").mkdir()
    shutil.copy2(ROOT / DOC_RELATIVE, target / DOC_RELATIVE)
    for name in BENCH_MODULES:
        shutil.copy2(ROOT / "scripts" / "ci" / name,
                     target / "scripts" / "ci" / name)
    (target / "pytest.ini").write_text(
        "[pytest]\nasyncio_default_fixture_loop_scope = function\n",
        encoding="utf-8")
    (target / "tests" / "test_synth_probe.py").write_text(
        "def test_probe():\n    assert True\n", encoding="utf-8")
    return target


def _measure(copy: Path) -> dict:
    """One bench pass over the copy's probe, as the gate runs it."""
    out = copy / "m.json"
    result = subprocess.run(  # pylint: disable=subprocess-run-check
        [sys.executable, str(BENCH),
         "--write", str(out),
         "--repo-root", str(copy),
         "--fixture", str(copy / "bench_files.txt")],
        cwd=str(copy), env=dict(os.environ), capture_output=True,
        text=True, check=False, timeout=600)
    assert result.returncode == 0, result.stderr
    return json.loads(out.read_text(encoding="utf-8"))


def _appended_document(doc: dict) -> dict:
    """The deployed document with a data-only refresh's worth of synthetic
    rows appended: twenty hosts on the tracked model, forty entries each,
    every entry loader-valid and dated after the committed history."""
    rates = {"fresh": 0.5, "create_5m": 0.625, "create_1h": 1.0,
             "read": 0.05, "output": 2.0}
    model = next(iter(doc["providers"]))
    for host_index in range(20):
        entries = [{"from": f"2027-{1 + stamp // 12:02d}-{1 + stamp % 12:02d}"
                    "T00:00:00Z", **rates}
                   for stamp in range(40)]
        doc["providers"][model][f"Synthetic{host_index}"] = entries
    newcomer = {key: [{"from": None, **rates}]
                for key in ("suite-synth-appended-model",)}
    doc["models"].update(newcomer)
    return doc


@pytest.fixture(name="bench_copies")
def bench_copies_fixture(tmp_path):
    """Two copies of the real suite machinery: the deployed document, and
    that document with synthetic rows appended."""
    plain = _build_copy(tmp_path / "plain")
    grown = _build_copy(tmp_path / "grown")
    doc_path = grown / DOC_RELATIVE
    doc = json.loads(doc_path.read_text(encoding="utf-8"))
    doc_path.write_text(
        json.dumps(_appended_document(doc), indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    (plain / "bench_files.txt").write_text("tests/test_synth_probe.py\n",
                                           encoding="utf-8")
    shutil.copy2(plain / "bench_files.txt", grown / "bench_files.txt")
    return plain, grown


def test_appended_rows_do_not_move_the_measurement(bench_copies):
    plain, grown = bench_copies
    baseline = _measure(plain)
    grown_measure = _measure(grown)
    for phase in ("collection", "run", "residual"):
        assert grown_measure["phases"][phase]["million_instructions"] == \
            baseline["phases"][phase]["million_instructions"], (
            f"{phase} moved with appended rate rows: "
            f"{baseline['phases'][phase]['million_instructions']} -> "
            f"{grown_measure['phases'][phase]['million_instructions']}")
