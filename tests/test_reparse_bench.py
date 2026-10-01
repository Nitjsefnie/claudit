"""Tests for the deterministic reparse CPU bench (issue #436).

The bench is the CI-side instrument for the maintainer's reparse lever:
the CPU cost of one parse of one transcript, measured over the committed
mini fixture mirror through the REAL ingest hot path, and ratcheted down
through .github/ci-thresholds.json like every other floor. Nothing here
pins a measured number: the value a run produces is machine-dependent by
nature (SV-TEST-DATA), so what the cases below pin is the bench's SHAPE —
the corpus it walks, the code path it drives, the arithmetic that turns a
CPU reading into the recorded unit, and the properties that make the
number reproducible at all (no database, no network, no wall clock).
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MIRROR = REPO_ROOT / "fixtures" / "r2_mini"


def _load():
    """Import scripts/ci/reparse_bench.py by path.

    scripts/ci is not a package and deliberately has no __init__.py, so
    the loader is the same by-path dance the sibling CI-script tests use;
    the directory goes on sys.path first so the module's own importlib
    import of `thresholds` resolves.
    """
    path = REPO_ROOT / "scripts" / "ci" / "reparse_bench.py"
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "ci"))
    spec = importlib.util.spec_from_file_location("reparse_bench", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["reparse_bench"] = module
    spec.loader.exec_module(module)
    return module


bench = _load()
thresholds = sys.modules["thresholds"]


def _fixture_transcripts():
    """Every .jsonl under the committed mini mirror, at run time.

    Derived from the tree rather than written down, so a fixture added
    later (SV-FIXTURE-SIZE keeps the set small but not frozen) does not
    turn this into a stale pin. The mirror's first level is the bucket
    directory the file-mode client scans from, so the object key is the
    path below it.
    """
    bucket_root = MIRROR / bench.BUCKET
    return sorted(p.relative_to(bucket_root).as_posix()
                  for p in bucket_root.rglob("*.jsonl"))


def test_corpus_is_every_fixture_transcript():
    entries = bench.corpus()
    assert [entry.key for entry in entries] == [
        f"{bench.BUCKET}/{rel}" for rel in _fixture_transcripts()]


def test_corpus_is_sorted_so_parse_order_is_fixed():
    # The listing walk yields os.walk order, which is filesystem-dependent.
    # Sorting by key is what makes the measured pass walk the corpus in
    # the same order on every machine.
    keys = [entry.key for entry in bench.corpus()]
    assert keys == sorted(keys)


def test_corpus_ignores_ambient_bucket_configuration(monkeypatch):
    # A developer's real R2_ENDPOINT/R2_BUCKET must never change WHAT is
    # measured: the recorded floor is only comparable against the
    # committed fixture mirror, so the bench pins both itself.
    monkeypatch.setenv("R2_ENDPOINT", "https://example.invalid/bucket")
    monkeypatch.setenv("R2_BUCKET", "not-the-fixture-bucket")
    entries = bench.corpus()
    assert entries, "the fixture mirror listed nothing under a bogus endpoint"
    assert all(entry.key.startswith(f"{bench.BUCKET}/") for entry in entries)


def test_corpus_entries_carry_the_transcripts_bytes():
    entries = bench.corpus()
    for entry in entries:
        assert entry.blob, f"{entry.key} read no bytes"
        assert entry.blob.endswith(b"\n"), (
            f"{entry.key} is not a JSONL transcript")


def test_pass_drives_the_real_parser_for_every_file(monkeypatch):
    # The bench must exercise the hot path, not an approximation of it:
    # parse.parse_file is wrapped, and the wrapper has to see EVERY
    # corpus key. A hand-rolled parse loop would leave this counter at 0.
    from backend import parse  # pylint: disable=import-outside-toplevel

    seen = []
    real = parse.parse_file

    def spy(file_key, blob):
        seen.append(file_key)
        return real(file_key, blob)

    monkeypatch.setattr(parse, "parse_file", spy)
    entries = bench.corpus()
    records = bench.run_pass(entries)
    assert seen == [entry.key for entry in entries]
    assert records > 0


def test_pass_parses_through_the_ingest_fetch_and_parse_unit(monkeypatch):
    # fetch_and_parse is the per-file unit the ingest pool calls
    # (backend/ingest.py's _fetch_and_parse); the bench calls the same
    # function rather than re-implementing its sidecar handling.
    from backend import ingest_fetch  # pylint: disable=import-outside-toplevel

    calls = []
    real = ingest_fetch.fetch_and_parse

    def spy(file_key, sidecar_key, fetch, parse_file=None):
        calls.append((file_key, sidecar_key))
        return real(file_key, sidecar_key, fetch, parse_file)

    monkeypatch.setattr(ingest_fetch, "fetch_and_parse", spy)
    bench.run_pass(bench.corpus())
    assert len(calls) == len(bench.corpus())
    assert [key for key, _sidecar in calls] == [
        entry.key for entry in bench.corpus()]


def test_pass_reads_nothing_from_the_object_store(monkeypatch):
    # Bytes are read once, before the measured region: a fetch inside it
    # would measure syscalls and page-cache state, not parse work. The
    # reader the bench passes in therefore serves from memory only — so
    # with the object store refusing every read, a pass still completes.
    from backend import r2  # pylint: disable=import-outside-toplevel

    entries = bench.corpus()

    def refuse(_key):
        raise AssertionError("the bench read an object inside the pass")

    monkeypatch.setattr(r2, "get_object", refuse)
    assert bench.run_pass(entries) > 0


def test_repeated_passes_agree_on_the_parsed_record_count():
    # Determinism of the RESULT, which is what makes the CPU number
    # comparable across runs: no state may leak from one file's parse
    # into the next one's (a module-level cache or a set that keeps
    # growing would show here as a drifting count).
    entries = bench.corpus()
    counts = {bench.run_pass(entries) for _ in range(10)}
    assert len(counts) == 1


def test_bench_opens_no_database_connection(monkeypatch):
    # importing backend.db is fine (the parse path pulls it in
    # transitively); CONNECTING is not. The tripwire turns any pool or
    # connect attempt into a failure, so a bench run that completes has
    # proved the measurement is database-free.
    import psycopg  # pylint: disable=import-outside-toplevel

    from backend import db  # pylint: disable=import-outside-toplevel

    def refuse(*_args, **_kwargs):
        raise AssertionError("the bench opened a database connection")

    monkeypatch.setattr(psycopg, "connect", refuse)
    monkeypatch.setattr(db, "ConnectionPool", refuse)
    assert bench.run_pass(bench.corpus()) > 0


def test_measurement_budgets_cpu_time_and_stays_inside_it():
    # The repo's performance-test rule: a performance assertion budgets
    # CPU time (time.process_time), never wall time, so a loaded runner
    # cannot redden it. The budget is ~60x the steady-state cost of this
    # configuration on the seeding machine — wide enough for a slow CI
    # box, tight enough to catch a parse path that stopped being linear.
    budget_cpu_s = 8.0
    entries = bench.corpus()
    start = time.process_time()
    measurement = bench.measure(entries, passes=200, warmup=20)
    spent = time.process_time() - start
    assert spent < budget_cpu_s, (
        f"the bench spent {spent:.2f} CPU s where the budget is "
        f"{budget_cpu_s:.2f} CPU s")
    assert measurement.cpu_s > 0
    assert measurement.files == len(entries)


def test_measurement_reports_one_decimal_place_in_the_recorded_unit():
    # The recorded spelling carries exactly one decimal place, the same
    # contract the coverage family has, so the canonical bytes and the
    # ratchet's arithmetic both stay exact.
    measurement = bench.measure(bench.corpus(), passes=3, warmup=1)
    assert measurement.value.as_tuple().exponent == -1
    assert measurement.value > 0


def test_value_is_the_measurement_per_file_times_the_unit_scale():
    # Pure arithmetic, so it is pinned with synthetic numbers rather than
    # a machine-dependent reading: 2.0 CPU s over 1000 passes of 5 files
    # is 0.4 ms per file, which the unit records as 40.0.
    value = bench.value_for(cpu_s=2.0, files=5, passes=1000)
    assert value == Decimal("40.0")
    assert bench.value_for(cpu_s=2.0, files=5, passes=2000) == Decimal("20.0")


def test_value_matches_the_recorded_unit_constant():
    assert bench.UNIT == thresholds.REPARSE_UNIT
    assert bench.UNIT_FILES == thresholds.REPARSE_FILES_PER_UNIT
    measurement = bench.measure(bench.corpus(), passes=3, warmup=1)
    expected = (Decimal(repr(measurement.cpu_s))
                / Decimal(measurement.passes * measurement.files)
                * Decimal(1000) * bench.UNIT_FILES)
    assert measurement.value == expected.quantize(Decimal("0.1"))


def test_bench_measures_cpu_time_never_wall_clock():
    # Source-level on purpose: nothing here renders or executes the
    # measurement, so the pin that survives is the one on the code. A
    # wall-clock component would make the number depend on the runner's
    # load rather than on the parse work, which is the whole point of
    # the bench.
    source = (REPO_ROOT / "scripts" / "ci" / "reparse_bench.py").read_text(
        encoding="utf-8")
    assert "process_time" in source
    for wall_clock in ("perf_counter", "monotonic", "time.time("):
        assert wall_clock not in source, (
            f"the bench references {wall_clock}: a CPU budget cannot carry "
            "a wall-clock component")


def test_value_cli_prints_the_bare_number():
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "ci" / "reparse_bench.py"),
         "--value", "--passes", "3", "--warmup", "1"],
        capture_output=True, text=True, check=True)
    printed = result.stdout.strip()
    assert Decimal(printed) > 0
    assert printed == f"{Decimal(printed):.1f}"


def test_machine_line_carries_the_value_and_the_corpus_size():
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "ci" / "reparse_bench.py"),
         "--machine", "--passes", "3", "--warmup", "1"],
        capture_output=True, text=True, check=True)
    fields = dict(
        piece.split("=", 1)
        for piece in result.stdout.strip().split()
        if "=" in piece)
    assert Decimal(fields["measured"]) > 0
    assert int(fields["files"]) == len(_fixture_transcripts())
    assert int(fields["passes"]) == 3


def test_human_report_names_the_unit_and_the_corpus():
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "ci" / "reparse_bench.py"),
         "--passes", "3", "--warmup", "1"],
        capture_output=True, text=True, check=True)
    out = result.stdout
    assert bench.UNIT in out
    assert str(len(_fixture_transcripts())) in out


def test_bench_forces_the_fixture_mirror_over_the_environment():
    # The subprocess starts with a hostile R2_ENDPOINT; the corpus it
    # measures must still be the committed mirror.
    env = dict(os.environ)
    env["R2_ENDPOINT"] = "https://example.invalid/bucket"
    env["R2_BUCKET"] = "not-the-fixture-bucket"
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "ci" / "reparse_bench.py"),
         "--machine", "--passes", "3", "--warmup", "1"],
        capture_output=True, text=True, check=True, env=env, cwd=str(REPO_ROOT))
    assert f"files={len(_fixture_transcripts())}" in result.stdout


@pytest.mark.parametrize("passes", ["0", "-1"])
def test_non_positive_pass_count_refused(passes):
    with pytest.raises(ValueError, match="positive"):
        bench.measure(bench.corpus(), passes=int(passes), warmup=1)
