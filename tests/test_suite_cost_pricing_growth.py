"""The suite-cost measurement must not depend on the deployed rate document.
The bench sets the bounded scripts/ci/suite_pricing_doc.json on pricing before
any counted interval opens. Run each copy's own bench over a loadable deployed
document and over one no loader accepts: phase instruction counts must be
exactly equal, so no read of the deployed document, in or out of a counted
interval, can enter the measurement.
Copy the complete bench support closure and bounded document beside each
bench: the --repo-root copy must also be the bench root whose injection runs."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from backend.pricing_document import serialize_pricing_doc

ROOT = Path(__file__).resolve().parents[1]
# The bench's complete import closure, copied beside each copy's own
# bench so the executed bench's ROOT is the copy: the injection then
# loads the copy's bounded document and any deployed read reads the
# copy's own, which differs between the arms.
BENCH_MODULES = ("suite_bench.py", "suite_phases.py", "suite_report.py",
                 "tests_tree.py",
                 "thresholds.py", "thresholds_validate.py",
                 "suite_pricing_doc.json")
# The committed document — the only tree file the check mutates in its
# copies — and the test-tree modules the copy's conftest imports.
DOC_RELATIVE = Path("src") / "pricing.json"


def _build_copy(target: Path) -> Path:
    """A minimal tree the copy's own bench can measure: backend/, the
    deployed document, the test-tree modules the conftest imports, and
    the bench with its complete support closure and bounded document.
    Byte-identical between copies but for the document."""
    target.mkdir(parents=True)
    shutil.copytree(ROOT / "backend", target / "backend")
    shutil.copytree(ROOT / "tests", target / "tests",
                    ignore=shutil.ignore_patterns(
                        "__pycache__", "*.pyc", "test_*", ".pg"))
    (target / "scripts" / "ci").mkdir(parents=True)
    (target / "src").mkdir()
    bounded = json.loads((ROOT / "scripts" / "ci" / "suite_pricing_doc.json").read_text(encoding="utf-8"))
    (target / DOC_RELATIVE).write_text(serialize_pricing_doc(bounded), encoding="utf-8", newline="\n")
    for name in BENCH_MODULES:
        shutil.copy2(ROOT / "scripts" / "ci" / name,
                     target / "scripts" / "ci" / name)
    (target / "pytest.ini").write_text(
        "[pytest]\nasyncio_default_fixture_loop_scope = function\n",
        encoding="utf-8")
    (target / "tests" / "test_synth_probe.py").write_text(
        "from backend import pricing\n\n\ndef test_probe():\n    assert pricing.MODEL_RATES\n",
        encoding="utf-8", newline="\n")
    (target / "bench_files.txt").write_text("tests/test_synth_probe.py\n", encoding="utf-8", newline="\n")
    return target


def _measure(copy: Path) -> dict:
    """One bench pass over the copy's tree, executed by the COPY'S OWN
    bench: ``--repo-root``, the bench ROOT and the bounded document's
    owner are one tree, so an injection-timing regression cannot read a
    shared original document behind the comparison's back."""
    out = copy / "m.json"
    socket_dir = copy / "no-pg-socket"
    socket_dir.mkdir()
    result = subprocess.run(  # pylint: disable=subprocess-run-check
        [sys.executable, str(copy / "scripts" / "ci" / "suite_bench.py"),
         "--write", str(out),
         "--repo-root", str(copy),
         "--fixture", str(copy / "bench_files.txt")],
        cwd=str(copy), env=dict(os.environ, PGHOST=str(socket_dir)),
        capture_output=True, text=True, check=False, timeout=600)
    assert result.returncode == 0, result.stderr
    return json.loads(out.read_text(encoding="utf-8"))


def test_the_measurement_never_reads_the_deployed_document(tmp_path):
    plain, refused = (_build_copy(tmp_path / name) for name in ("plain", "refused"))
    (refused / DOC_RELATIVE).write_text("", encoding="utf-8")  # no loader accepts it
    baseline, unread = _measure(plain), _measure(refused)
    for phase in ("collection", "run", "residual"):
        assert unread["phases"][phase]["million_instructions"] == \
            baseline["phases"][phase]["million_instructions"], (
            f"{phase} moved with the deployed document: "
            f"{baseline['phases'][phase]['million_instructions']} -> "
            f"{unread['phases'][phase]['million_instructions']}")


# A stated ceiling, not a ratchet: the suite-cost bench loads a bounded
# document rather than the deployed one, so data growth is visible only
# here. ~60% headroom over the ~1.9 MB the vendor-row data take; the next
# real growth step past this needs a deliberate edit of this constant,
# which is the visibility the guard exists for.
PRICING_DOC_CEILING_BYTES = 3_000_000


@pytest.mark.skipif(
    os.environ.get("CLAUDIT_PERTURBED_DATA") == "1",
    reason="the perturbed tree inflates the document by design; this "
           "ceiling pins the DEPLOYED document, which the perturbed-data "
           "leg is not")
def test_the_deployed_document_stays_under_its_size_ceiling():
    doc = ROOT / DOC_RELATIVE
    assert doc.stat().st_size <= PRICING_DOC_CEILING_BYTES, (
        f"src/pricing.json grew past {PRICING_DOC_CEILING_BYTES} bytes; "
        "every byte is fetched synchronously by every browser page load "
        "and shipped to every meter deploy — state why it must grow, then "
        "raise this ceiling by editing it")
