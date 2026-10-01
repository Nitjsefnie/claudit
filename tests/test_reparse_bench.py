"""Tests for the deterministic reparse CPU bench (issue #436).

The bench is the CI-side instrument for the maintainer's reparse lever:
the CPU work of one parse of one transcript, measured over the committed
mini fixture mirror through the REAL ingest hot path, split into the
phases it is made of, and ratcheted down through
.github/ci-thresholds.json like every other floor. Nothing here pins a
measured number: what a run produces is machine-dependent by nature
(SV-TEST-DATA), so the cases below pin the bench's SHAPE — the corpus it
walks, the code path it drives, the partition it reports, the arithmetic
behind the recorded shares, and the properties that make the number
reproducible at all (no database, no network, no wall clock).
"""
from __future__ import annotations

import importlib.util
import sys
import time
from decimal import Decimal
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MIRROR = REPO_ROOT / "fixtures" / "r2_mini"
BENCH = REPO_ROOT / "scripts" / "ci" / "reparse_bench.py"


def _load(name="reparse_bench"):
    """Import a scripts/ci module by path.

    scripts/ci is not a package and deliberately has no __init__.py, so
    the loader is the same by-path dance the sibling CI-script tests use;
    the directory goes on sys.path first so the module's own importlib
    imports of its siblings resolve.
    """
    if str(REPO_ROOT / "scripts" / "ci") not in sys.path:
        sys.path.insert(0, str(REPO_ROOT / "scripts" / "ci"))
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "scripts" / "ci" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bench = _load()
report_module = _load("reparse_report")
phases = _load("reparse_phases")
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


def _short(**kwargs):
    """A measurement too short to be a recorded number, fast enough for
    a test: the shape is what these assert, never the value."""
    kwargs.setdefault("passes", 5)
    kwargs.setdefault("warmup", 1)
    return bench.measure(bench.corpus(), **kwargs)


# --- the corpus and the path -------------------------------------------------

def test_corpus_is_every_fixture_transcript():
    entries = bench.corpus()
    assert [entry.key for entry in entries] == [
        f"{bench.BUCKET}/{rel}" for rel in _fixture_transcripts()]


def test_corpus_is_sorted_so_parse_order_is_fixed():
    # The listing walk yields os.walk order, which is filesystem-
    # dependent. Sorting by key is what makes the measured pass walk the
    # corpus in the same order on every machine.
    keys = [entry.key for entry in bench.corpus()]
    assert keys == sorted(keys)


def test_corpus_ignores_ambient_bucket_configuration(monkeypatch):
    # A developer's real R2_ENDPOINT/R2_BUCKET must never change WHAT is
    # measured: the recorded shares are only comparable against the
    # committed fixture mirror, so the bench pins both settings itself.
    monkeypatch.setenv("R2_ENDPOINT", "https://example.invalid/bucket")
    monkeypatch.setenv("R2_BUCKET", "not-the-fixture-bucket")
    entries = bench.corpus()
    assert entries, "the fixture mirror listed nothing under a bogus endpoint"
    assert all(entry.key.startswith(f"{bench.BUCKET}/") for entry in entries)


def test_corpus_entries_carry_the_transcripts_bytes():
    for entry in bench.corpus():
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
    entries = bench.corpus()
    bench.run_pass(entries)
    assert len(calls) == len(entries)
    assert [key for key, _sidecar in calls] == [e.key for e in entries]


def test_pass_reads_nothing_from_the_object_store(monkeypatch):
    # Bytes are read once, before the measured region: a fetch inside it
    # would measure syscalls and page-cache state, not parse work. The
    # reader the pass calls serves from memory only — so with the object
    # store refusing every read, a pass still completes.
    from backend import r2  # pylint: disable=import-outside-toplevel

    entries = bench.corpus()

    def refuse(_key):
        raise AssertionError("the bench read an object inside the pass")

    monkeypatch.setattr(r2, "get_object", refuse)
    assert bench.run_pass(entries) > 0


def test_repeated_passes_agree_on_the_parsed_record_count():
    # Determinism of the RESULT, which is what makes the CPU number
    # comparable across runs: no state may leak from one file's parse
    # into the next one's.
    entries = bench.corpus()
    counts = {bench.run_pass(entries) for _ in range(10)}
    assert len(counts) == 1


def test_bench_opens_no_database_connection(monkeypatch):
    # importing backend.db is fine (the parse path pulls it in
    # transitively); CONNECTING is not. The tripwire turns any pool or
    # connect attempt into a failure, so a measurement that completes
    # has proved the run is database-free.
    import psycopg  # pylint: disable=import-outside-toplevel

    from backend import db  # pylint: disable=import-outside-toplevel

    def refuse(*_args, **_kwargs):
        raise AssertionError("the bench opened a database connection")

    monkeypatch.setattr(psycopg, "connect", refuse)
    monkeypatch.setattr(db, "ConnectionPool", refuse)
    assert bench.run_pass(bench.corpus()) > 0


# --- the decomposition -------------------------------------------------------

def test_phases_come_from_the_loader():
    # One source of truth: a phase cannot be measured under one name and
    # recorded in the document under another.
    assert bench.PHASES == thresholds.REPARSE_PHASES
    assert bench.UNIT == thresholds.REPARSE_UNIT


def test_every_phase_is_reported_and_they_account_for_the_pass():
    measurement = _short()
    assert set(measurement.shares) == set(bench.PHASES)
    # The phases are a partition, so their CPU sums to the run's own CPU
    # and their shares to 100% (each rounded to one decimal).
    assert sum(measurement.phase_cpu_s.values()) == pytest.approx(
        measurement.cpu_s, rel=1e-9)
    assert float(sum(measurement.shares.values())) == pytest.approx(
        100.0, abs=0.2)
    for name in bench.PHASES:
        assert measurement.shares[name] >= 0
        assert measurement.phase_cpu_s[name] >= 0


def test_sniff_and_parse_body_are_phases_of_the_parse_not_of_the_pass():
    # sniff is nested inside parse_file and parse_body is what is left of
    # parse_file once the sniff is taken out, so the two cannot double
    # count — that is what makes the shares a partition rather than a
    # list of overlapping timers. Together they still sit inside the
    # pass, so the residual has room to be a phase of its own.
    measurement = _short(passes=20)
    assert measurement.phase_cpu_s["sniff"] > 0
    assert measurement.phase_cpu_s["parse_body"] > measurement.phase_cpu_s["sniff"]
    assert measurement.shares["parse_body"] > measurement.shares["sniff"]
    assert (measurement.phase_cpu_s["sniff"]
            + measurement.phase_cpu_s["parse_body"]) < measurement.cpu_s


def test_residual_is_the_divergence_not_a_rounding():
    # The residual is everything the wrapped callables do not account
    # for. It is reported as its own phase rather than spread over the
    # others, so an unmeasured phase cannot hide inside a measured one.
    measurement = _short()
    residual = measurement.phase_cpu_s["residual"]
    assert residual > 0
    assert measurement.phase_cpu_s["residual"] < measurement.cpu_s


def test_sidecar_phase_is_measured_when_a_sidecar_exists():
    # The committed mirror carries no meta.json sidecar, so this phase
    # reads 0.0 there. The instrumentation still has to see one when the
    # corpus has one: a phase that is wired up but never reached would
    # be indistinguishable from a phase that is not wired up.
    from backend import parse  # pylint: disable=import-outside-toplevel

    entry = bench.corpus()[0]
    assert entry.sidecar_key is None, (
        "this case needs a transcript without a sidecar to pair one onto")
    sided = entry._replace(
        sidecar_key=f"{entry.key}.meta.json",
        sidecar_blob=b'{"agentType": "bench-sidecar-role"}')
    measurement = bench.measure([sided], passes=5, warmup=1)
    assert measurement.phase_cpu_s["sidecar"] > 0
    assert measurement.shares["sidecar"] > 0
    assert parse.parse_file is not None  # the module is back in place


def test_instrumentation_is_removed_after_a_measurement():
    from backend import agent_sidecar, parse  # pylint: disable=import-outside-toplevel

    before = (parse.sniff_format, parse.parse_file,
              agent_sidecar.apply_agent_sidecar)
    _short()
    after = (parse.sniff_format, parse.parse_file,
             agent_sidecar.apply_agent_sidecar)
    assert after == before


def test_instrumentation_is_removed_even_when_the_parse_raises(monkeypatch):
    # A wrapper left installed after a failed parse would make every
    # LATER measurement in the process time a function nobody calls.
    from backend import parse  # pylint: disable=import-outside-toplevel

    def boom(*_args, **_kwargs):
        raise RuntimeError("the parse failed")

    monkeypatch.setattr(parse, "parse_file", boom)
    totals = {"sniff": 0.0, "parse_file": 0.0, "sidecar": 0.0}
    with pytest.raises(RuntimeError):
        with phases.instrumented(totals):
            parse.parse_file("k", b"")
    assert parse.parse_file is boom


def test_share_of_is_pure_arithmetic():
    # Pinned on synthetic numbers rather than a machine-dependent reading.
    assert bench.share_of(1.0, 4.0) == Decimal("25.0")
    assert bench.share_of(0.0, 4.0) == Decimal("0.0")
    assert bench.share_of(3.0, 4.0) == Decimal("75.0")


def test_share_of_refuses_a_run_with_no_cpu():
    with pytest.raises(ValueError, match="nothing to share"):
        bench.share_of(0.0, 0.0)


# --- the measurement, the file and the gate ----------------------------------

def test_measurement_budgets_cpu_time_and_stays_inside_it():
    # The repo's performance-test rule: a performance assertion budgets
    # CPU time (time.process_time), never wall time, so a loaded runner
    # cannot redden it. The budget is orders of magnitude above this
    # configuration's cost on the seeding machine — wide enough for a
    # slow CI box, tight enough to catch a parse path that stopped being
    # linear in the corpus.
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


def test_shares_carry_exactly_one_decimal_place():
    # The recorded spelling carries one decimal place, the same contract
    # the coverage family has, so the canonical bytes and the ratchet's
    # arithmetic both stay exact.
    for share in _short().shares.values():
        assert share.as_tuple().exponent == -1


@pytest.mark.parametrize("passes", ["0", "-1"])
def test_non_positive_pass_count_refused(passes):
    with pytest.raises(ValueError, match="positive"):
        bench.measure(bench.corpus(), passes=int(passes), warmup=1)


def test_bench_measures_cpu_time_never_wall_clock():
    # Source-level on purpose: nothing here renders or executes the
    # measurement, so the pin that survives is the one on the code. A
    # wall-clock component would make the number depend on the runner's
    # load rather than on the parse work, which is the whole point.
    source = BENCH.read_text(encoding="utf-8")
    assert "process_time" in source
    for wall_clock in ("perf_counter", "monotonic", "time.time("):
        assert wall_clock not in source, (
            f"the bench references {wall_clock}: a CPU budget cannot carry "
            "a wall-clock component")


def test_written_measurement_round_trips_exactly(tmp_path):
    # Shares go through the file as STRINGS on purpose: a float would
    # round-trip a one-decimal share to whatever the nearest double
    # spells, and the recorded value has to be the value measured.
    measurement = _short()
    path = tmp_path / "m.json"
    report_module.write_measurement(path, measurement)
    assert '"share": "' in path.read_text(encoding="utf-8")
    restored = report_module.measurement_from_file(path)
    assert restored.shares == measurement.shares
    assert restored.phase_cpu_s == measurement.phase_cpu_s
    assert restored.cpu_s == measurement.cpu_s
    assert restored.files == measurement.files


def test_measurement_file_without_phases_refused(tmp_path):
    path = tmp_path / "m.json"
    path.write_text('{"unit": "percent_of_pass_cpu"}', encoding="utf-8")
    with pytest.raises(ValueError, match="no phases"):
        report_module.measurement_from_file(path)


def test_missing_measurement_file_refused(tmp_path):
    with pytest.raises(ValueError, match="cannot read"):
        report_module.measurement_from_file(tmp_path / "absent.json")


def test_check_passes_while_every_phase_is_within_its_floor(tmp_path):
    thresholds_path = tmp_path / "ci-thresholds.json"
    thresholds.write(thresholds_path, _document(_budgets()))
    path = tmp_path / "m.json"
    report_module.write_measurement(path, _measurement_with(
        {name: Decimal("9.0") for name in bench.PHASES}))
    assert bench.main(["--check", str(path), "--thresholds",
                       str(thresholds_path)]) == 0


def test_check_fails_on_a_phase_over_its_share_floor(tmp_path):
    thresholds_path = tmp_path / "ci-thresholds.json"
    thresholds.write(thresholds_path, _document(_budgets()))
    shares = {name: Decimal("9.0") for name in bench.PHASES}
    shares["parse_body"] = Decimal("40.0")
    path = tmp_path / "m.json"
    report_module.write_measurement(path, _measurement_with(shares))
    assert bench.main(["--check", str(path), "--thresholds",
                       str(thresholds_path)]) == 1


def test_check_fails_on_a_phase_over_its_bytecode_floor(tmp_path, capsys):
    # The share and the count are gated separately, and a phase over
    # either one fails: the count is what catches a uniform slowdown,
    # where every share stays put.
    thresholds_path = tmp_path / "ci-thresholds.json"
    thresholds.write(thresholds_path, _document(_budgets()))
    counted = {name: Decimal("1.0") for name in bench.PHASES}
    counted["parse_body"] = Decimal("40.0")
    path = tmp_path / "m.json"
    report_module.write_measurement(path, _measurement_with(
        {name: Decimal("9.0") for name in bench.PHASES}, counted))
    assert bench.main(["--check", str(path), "--thresholds",
                       str(thresholds_path)]) == 1
    err = capsys.readouterr().err
    assert "parse_body: 40.0" in err
    assert "sniff" not in err, "only the offending phase is named"


def test_check_names_the_offending_phase(tmp_path, capsys):
    thresholds_path = tmp_path / "ci-thresholds.json"
    thresholds.write(thresholds_path, _document(_budgets()))
    shares = {name: Decimal("9.0") for name in bench.PHASES}
    shares["sniff"] = Decimal("40.0")
    path = tmp_path / "m.json"
    report_module.write_measurement(path, _measurement_with(shares))
    assert bench.main(["--check", str(path), "--thresholds",
                       str(thresholds_path)]) == 1
    err = capsys.readouterr().err
    assert "sniff: 40.0% of the pass, floor 11.5%" in err
    assert "never raised by hand" in err


def _document(reparse):
    return {
        "schema_version": 1,
        "coverage": {
            "python": {"measured": Decimal("92.6"), "floor": Decimal("91.1")},
            "javascript": {"measured": Decimal("50.0"),
                           "floor": Decimal("48.5")},
        },
        "reparse": reparse,
        "module_size_baseline": {},
        "pylint_suppression_baseline": {},
    }


def _budgets(measured="10.0", **overrides):
    """A synthetic family: both metrics for every phase, one override
    set per call."""
    budgets = {
        phase: {metric: {"measured": Decimal(measured),
                         "floor": Decimal(measured) + Decimal("1.5")}
                for metric in thresholds.REPARSE_METRICS}
        for phase in thresholds.REPARSE_PHASES
    }
    for label, value in overrides.items():
        phase, metric = label.split(".")
        budgets[phase][metric] = {"measured": Decimal(value),
                                  "floor": Decimal(value) + Decimal("1.5")}
    return budgets


def _measurement_with(shares, counted=None):
    """A measurement whose phases read the given values, under both
    instruments."""
    measurement = _short()
    return measurement._replace(
        shares=shares,
        phase_cpu_s={name: 0.0 for name in bench.PHASES},
        instruction_per_file=(dict.fromkeys(bench.PHASES, None) if counted
                              is None else counted))


def test_the_corpus_endpoint_is_built_with_as_uri_not_an_f_string():
    """The spelling half, because the behavioural half cannot fire on Linux.

    `as_uri()` and f'file://{path}' produce the IDENTICAL string on POSIX, so
    the assertion above — which checks what r2 makes of the URL — passes with
    either construction here and only bites on the Windows legs. Since the
    defect was invisible everywhere except the platform that failed, the
    construction itself has to be pinned, and that is a source-text
    assertion: it is the only form that can distinguish two expressions that
    agree on the platform running the test.
    """
    source = BENCH.read_text(encoding='utf-8')
    assert 'MIRROR.as_uri()' in source, (
        "the corpus endpoint is not built with as_uri(); on Windows the "
        "f-string form parses to an empty path and every Windows leg fails")
    assert "f'file://{MIRROR}'" not in source, (
        "the f-string form is back: it is correct on POSIX and produces an "
        "empty URL path on Windows")
