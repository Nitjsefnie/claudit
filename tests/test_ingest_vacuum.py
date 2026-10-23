"""Post-ingest VACUUM wiring.

Which tables a run vacuums (each family it rewrote), when it skips (the
threshold's two sides, the batch cadence's two sides), the guarantees
around the statement itself — plain VACUUM only, outside any
transaction, abort- and failure-isolated — and the between-batch
interleave a full reparse runs.
"""
from __future__ import annotations

import inspect
import logging
import os
import re
import threading
import time
import tokenize
from pathlib import Path

import psycopg
import pytest

from test_ingest import (  # pylint: disable=unused-import
    _fresh_db_fixture as ingest_fresh_db_fixture,
    _mini_r2_env_fixture as ingest_mini_r2_env,
)
from backend import db, ingest, ingest_fetch, ingest_reprice, ingest_vacuum
from tests import mini_mirror

_REPO = Path(__file__).resolve().parents[1]

# One case per write family: the tables a run touched through that
# family alone must be exactly the tables vacuumed for it.
_FAMILY_CASES = [
    ("reparse", 10, {},
     {"records", "tool_uses", "records_price", "files", "projects"}),
    ("reprice", 0, {"reprice": 5}, {"records_price"}),
    ("canonical", 0, {"canonical": 3},
     {"records", "tool_uses", "files"}),
    ("suppressed", 0, {"suppressed": 7},
     {"records", "tool_uses", "records_price"}),
    ("aliases", 0, {"aliases": 2}, {"files", "projects"}),
    ("teammates", 0, {"teammates": 1}, {"files"}),
    ("usage_rollup", 0, {"usage_rollup": 4}, {"usage_rollup"}),
    ("tool_rollup", 0, {"tool_rollup": 4}, {"tool_rollup"}),
    ("tool_error_rollup", 0, {"tool_error_rollup": 4},
     {"tool_error_rollup"}),
    ("dispatch_rollup", 0, {"dispatch_rollup": 4}, {"dispatch_rollup"}),
    ("dispatch_brief_rollup", 0, {"dispatch_brief_rollup": 4},
     {"dispatch_brief_rollup"}),
    ("latency_rollup", 0, {"latency_rollup": 4}, {"latency_rollup"}),
    ("ctx_cost_rollup", 0, {"ctx_cost_rollup": 4}, {"ctx_cost_rollup"}),
    ("agent_rollup", 0, {"agent_rollup": 4},
     {"agent_rollup", "dim_agent_type"}),
    ("web_metrics_rollup", 0, {"web_metrics_rollup": 4},
     {"web_metrics_rollup"}),
    ("nothing_written", 0, {}, set()),
]


def _spy_executes(monkeypatch):
    executed: list = []
    monkeypatch.setattr(
        ingest_vacuum, "_execute",
        lambda tables, analyze: executed.append((tuple(tables), analyze)))
    return executed


def _force_full_reparse():
    """Make every stored file todo and the run's scope full — the state a
    PARSER_VERSION bump produces (the fingerprint carries the version, so
    clearing the derived-state marker is what the bump does to the scope,
    and the version mismatch is what it does to the todo list)."""
    with db.viz_conn() as conn:
        conn.execute("UPDATE files SET parser_version = 'forced-reparse'")
        conn.execute("DELETE FROM ingest_derived_state")
        conn.commit()


# ---------------------------------------------------------------------------
# Touched → vacuumed, per write family
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case_id,files_churned,phase_rows,expected",
    _FAMILY_CASES)
def test_each_family_vacuums_exactly_the_tables_it_rewrote(
        monkeypatch, case_id, files_churned, phase_rows, expected):
    """Every family the run rewrote rows through names its own tables,
    and the run vacuums (ANALYZE) that set once — nothing more."""
    executed = _spy_executes(monkeypatch)
    monkeypatch.setattr(ingest_vacuum, "VACUUM_MIN_WRITTEN", 0)
    ingest_vacuum.begin_run()
    ingest_vacuum.note_files(files_churned)
    for phase, rows in phase_rows.items():
        ingest_vacuum.note_phase(phase, rows)
    ingest_vacuum.vacuum_after_run()
    if expected:
        assert len(executed) == 1, (
            f"family case vacuumed {executed}, expected {expected}")
        tables, analyze = executed[0]
        assert set(tables) == expected, (
            f"family case vacuumed {tables}, expected {expected}")
        assert analyze is True, "the tail statement must carry ANALYZE"
    else:
        assert not executed, "a run that touched nothing must vacuum nothing"


def test_the_rebuild_records_every_phase_row_count(
        fresh_db, mini_r2_env, monkeypatch):
    """The tail decision derives from what each rebuild phase returned —
    recorded as the phases run, never re-scanned off the tables."""
    assert ingest.run_ingest(trigger="manual")["error"] is None
    # Stand in for the tail so its own drain does not hide what the
    # phases recorded; the drain itself is pinned by the threshold test.
    monkeypatch.setattr(ingest_vacuum, "vacuum_after_run",
                        lambda **kwargs: None)
    ingest_vacuum.begin_run()
    ingest._rebuild_derived_state()  # pylint: disable=protected-access
    rows = ingest_vacuum.take_phase_rows()
    assert set(rows) == {
        "suppressed", "reprice", "aliases", "canonical", "teammates",
        "usage_rollup", "tool_rollup", "tool_error_rollup",
        "dispatch_rollup", "dispatch_brief_rollup", "latency_rollup",
        "ctx_cost_rollup", "agent_rollup", "web_metrics_rollup",
    }
    assert rows["usage_rollup"] > 0, (
        "a populated corpus must produce usage_rollup rows")


def test_the_reprice_pass_notes_its_restatement_writes(fresh_db):
    """reprice_stale() returns only the CHANGED rows; the restamp sets
    (Phase A's clean restamp and the keyset's unchanged rows) write the
    sidecar too and must reach the vacuum's count through their own note."""
    ingest_vacuum.begin_run()
    ingest_reprice.reprice_stale()
    assert "reprice" in ingest_vacuum.take_phase_rows()


# ---------------------------------------------------------------------------
# The threshold: both sides of the module constant
# ---------------------------------------------------------------------------


def test_the_threshold_skips_just_below_and_vacuums_at_it(monkeypatch):
    """The real constant, both sides: one row below skips, at the value
    runs; a single reparsed file stays below it, enough files cross it."""
    executed = _spy_executes(monkeypatch)
    minimum = ingest_vacuum.VACUUM_MIN_WRITTEN
    per_file = ingest_vacuum.REPARSE_ROWS_PER_FILE
    assert minimum > per_file > 0, (
        "the constant's rationale assumes one reparsed file alone is "
        "below the threshold")

    ingest_vacuum.begin_run()
    ingest_vacuum.note_phase("reprice", minimum - 1)
    ingest_vacuum.vacuum_after_run()
    assert not executed, "one row below the threshold must skip"

    ingest_vacuum.begin_run()
    ingest_vacuum.note_phase("reprice", minimum)
    ingest_vacuum.vacuum_after_run()
    assert executed == [(("records_price",), True)], (
        "at the threshold the run must vacuum")

    # The spy's length is CUMULATIVE across these cases — each case
    # asserts its own delta, never an absolute count.
    before = len(executed)
    ingest_vacuum.begin_run()
    ingest_vacuum.note_files(1)
    ingest_vacuum.vacuum_after_run()
    assert len(executed) == before, (
        "one reparsed file alone must still skip")

    before = len(executed)
    ingest_vacuum.begin_run()
    ingest_vacuum.note_files(-(-minimum // per_file))
    ingest_vacuum.vacuum_after_run()
    assert len(executed) == before + 1, (
        "enough reparsed files must cross it")


@pytest.mark.parametrize(
    "constant",
    ["VACUUM_MIN_WRITTEN", "REPARSE_ROWS_PER_FILE", "VACUUM_BATCH_FILES"])
def test_every_vacuum_constant_carries_a_written_rationale(constant):
    """Threshold and cadence are module constants and each carries a
    written rationale directly above it — the comment block that says
    WHY the value is what it is, not just what it is."""
    lines = inspect.getsource(ingest_vacuum).splitlines()
    index = next(i for i, line in enumerate(lines)
                 if line.startswith(f"{constant} ="))
    comment: list[str] = []
    cursor = index - 1
    while cursor >= 0 and lines[cursor].lstrip().startswith("#"):
        comment.insert(0, lines[cursor])
        cursor -= 1
    prose = " ".join(part.lstrip("#").strip() for part in comment)
    assert "rationale" in prose.lower(), (
        f"{constant} has no rationale comment above it")
    assert len(prose) >= 60, (
        f"{constant}'s rationale is too thin to be one: {prose!r}")


# ---------------------------------------------------------------------------
# Plain VACUUM only: the relocating mode is never issued from ingest
# ---------------------------------------------------------------------------


def test_the_no_full_mode_detector_actually_detects():
    """Self-check for the source scan below: the pattern matches the
    forbidden statement — bare and parenthesised — and does not match
    a plain VACUUM."""
    pattern = re.compile(r"\bvacuum\s*\(?\s*full\b", re.IGNORECASE)
    assert pattern.search("VACUUM FULL records")
    assert pattern.search("vacuum\n   full")
    assert pattern.search("VACUUM (FULL) records")
    assert pattern.search("VACUUM(FULL) records")
    assert not pattern.search("VACUUM (ANALYZE) records")
    assert not pattern.search("VACUUM_BATCH_FILES")


def test_no_ingest_source_issues_the_relocating_vacuum():
    """backend/ingest*.py may run plain VACUUM only. The scan reads the
    executable token stream — comments stripped, so the rationale that
    names the forbidden mode is not itself the failure — and no string
    may carry the forbidden statement, bare or parenthesised."""
    pattern = re.compile(r"\bvacuum\s*\(?\s*full\b", re.IGNORECASE)
    sources = sorted((_REPO / "backend").glob("ingest*.py"))
    assert "ingest_vacuum.py" in [path.name for path in sources]
    for path in sources:
        kept: list[str] = []
        with path.open(encoding="utf-8") as handle:
            for token in tokenize.generate_tokens(handle.readline):
                if token.type != tokenize.COMMENT:
                    kept.append(token.string)
        assert not pattern.search(" ".join(kept)), (
            f"{path.name} carries a relocating VACUUM statement")


# ---------------------------------------------------------------------------
# Outside any transaction, statement shape, identifier allow-list
# ---------------------------------------------------------------------------


def test_the_statement_is_refused_inside_a_transaction(fresh_db):
    """The same plain VACUUM inside an explicit transaction is a server
    error — the property the ingest path must not have."""
    with db.viz_conn() as conn:
        with pytest.raises(
                psycopg.errors.DatabaseError,
                match="cannot run inside a transaction block"):
            conn.execute("VACUUM records")


def test_the_vacuum_connection_holds_no_transaction(fresh_db):
    """The module's own connection is autocommit — outside every
    transaction — and a real statement succeeds through it."""
    with ingest_vacuum._vacuum_connection() as conn:  # pylint: disable=protected-access
        assert conn.autocommit is True
    assert ingest_vacuum.run_vacuum(["records"]) is True


def test_the_statement_shape_and_identifier_allow_list():
    """VACUUM cannot parameterise identifiers, so the statement is built
    only from the module's own allow-listed table names."""
    assert (ingest_vacuum._statement(  # pylint: disable=protected-access
        ["records", "files"], analyze=True)
        == "VACUUM (ANALYZE) records, files")
    assert (ingest_vacuum._statement(  # pylint: disable=protected-access
        ["records"], analyze=False)
        == "VACUUM records")
    with pytest.raises(ValueError):
        ingest_vacuum._statement(  # pylint: disable=protected-access
            ["records; DROP TABLE files"], analyze=True)
    with pytest.raises(ValueError):
        ingest_vacuum._statement(  # pylint: disable=protected-access
            ["no_such_table"], analyze=True)


# ---------------------------------------------------------------------------
# Failure isolation and the abort guard
# ---------------------------------------------------------------------------


def test_a_failing_vacuum_keeps_the_run_green(
        fresh_db, mini_r2_env, monkeypatch, caplog):
    """A vacuum failure is booked as a run-summary warning and a log
    line; it never becomes the run's error — and it never enters the
    guest-visible ingest_done SSE payload, whose byte-identity for
    /api/events (constraint 1) wins: the key rides only the admin run
    summary the broadcast's caller receives."""
    monkeypatch.setattr(ingest_vacuum, "VACUUM_MIN_WRITTEN", 0)

    def boom(tables, analyze):
        raise RuntimeError("vacuum exploded")

    monkeypatch.setattr(ingest_vacuum, "_execute", boom)
    broadcasts: list = []
    monkeypatch.setattr(
        ingest.events, "broadcast_threadsafe",
        # Snapshot the payload the way the real broadcast serializes it
        # (json.dumps runs eagerly inside), so later mutations of the
        # run summary cannot enter the captured SSE bytes.
        lambda *args, **kwargs: broadcasts.append((args[0], dict(args[1]))))
    with caplog.at_level(logging.WARNING, logger="claudit.ingest"):
        summary = ingest.run_ingest(trigger="manual")
    assert summary["error"] is None, summary
    assert summary.get("vacuum_warning"), summary
    assert "vacuum failed" in summary["vacuum_warning"]
    assert broadcasts, "a rewriting run must broadcast ingest_done"
    event_name, payload = broadcasts[0]
    assert event_name == "ingest_done"
    assert "vacuum_warning" not in payload, (
        f"the SSE payload must stay byte-identical, got {payload}")
    assert any("vacuum exploded" in record.getMessage()
               for record in caplog.records
               if record.name == "claudit.ingest"), (
        "the failure must reach the server log")


def test_a_cancelled_mid_statement_vacuum_ends_promptly_and_stays_green(
        fresh_db, mini_r2_env, monkeypatch):
    """The vacuum's dedicated connection is registered with db's
    cancellation registry, so teardown's db.cancel_viz_queries()
    interrupts an in-flight statement: the
    run's wait ends promptly, the driver's cancellation is swallowed as
    an ordinary vacuum failure, and the run still closes green with the
    warning."""
    monkeypatch.setattr(ingest_vacuum, "VACUUM_MIN_WRITTEN", 0)
    monkeypatch.setattr(
        ingest_vacuum, "_statement",
        lambda tables, analyze: "SELECT pg_sleep(120)")
    outcome: dict = {}
    done = threading.Event()

    def runner():
        try:
            outcome["summary"] = ingest.run_ingest(trigger="manual")
        except Exception as exc:  # noqa: BLE001
            outcome["error"] = repr(exc)
        finally:
            done.set()

    threading.Thread(target=runner).start()
    # The synchronisation point is the server, not a sleep: wait until
    # pg_stat_activity shows the vacuum's sleep executing, then cancel.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with db.viz_conn() as conn:
            # pid guard: this poll's own text contains 'pg_sleep' and
            # would otherwise match itself on the first pass, cancelling
            # before the vacuum statement ever starts.
            row = conn.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE query LIKE '%pg_sleep%%' AND state = 'active' "
                "AND pid <> pg_backend_pid()"
            ).fetchone()
        if row and row[0]:
            break
        time.sleep(0.05)
    else:
        pytest.fail("the vacuum statement never reached the server")
    db.cancel_viz_queries()
    assert done.wait(timeout=30), (
        "a cancelled vacuum must end the run's wait promptly")
    assert "error" not in outcome, outcome
    summary = outcome["summary"]
    assert summary["error"] is None, summary
    assert summary.get("vacuum_warning"), summary
    assert "vacuum failed" in summary["vacuum_warning"]


def test_a_stop_signal_skips_the_tail_vacuum(monkeypatch):
    """An aborting run never starts the tail vacuum: the guard is
    consulted first, the skip is silent success, nothing propagates."""
    executed = _spy_executes(monkeypatch)
    monkeypatch.setattr(ingest_vacuum, "VACUUM_MIN_WRITTEN", 0)

    def raise_abort():
        raise ingest_reprice.IngestAborted("shutdown requested")

    ingest_vacuum.begin_run()
    ingest_vacuum.note_files(1_000_000)
    ingest_vacuum.vacuum_after_run(check_shutdown=raise_abort)
    assert not executed, "a stop signal must skip pending vacuums"


def test_an_aborting_rebuild_skips_the_vacuum(
        fresh_db, mini_r2_env, monkeypatch):
    """Shutdown requested mid-rebuild unwinds the run as aborted before
    the tail vacuum's line is reached — no vacuum, run closes aborted."""
    monkeypatch.setattr(ingest_vacuum, "VACUUM_MIN_WRITTEN", 0)
    executed = _spy_executes(monkeypatch)
    real_purge = ingest.purge_suppressed

    def aborting(*args, **kwargs):
        ingest._SHUTDOWN.set()  # pylint: disable=protected-access
        return real_purge(*args, **kwargs)

    monkeypatch.setattr(ingest, "purge_suppressed", aborting)
    try:
        summary = ingest.run_ingest(trigger="manual")
    finally:
        ingest._SHUTDOWN.clear()  # pylint: disable=protected-access
    assert summary["aborted"] is True, summary
    assert "aborted" in summary["error"], summary
    assert not executed, "an aborted run must skip every vacuum"


# ---------------------------------------------------------------------------
# The run's tracked counts drive the decision
# ---------------------------------------------------------------------------


def test_a_run_that_wrote_little_skips_the_vacuum(
        fresh_db, mini_r2_env, monkeypatch):
    """Both real runs over the mini mirror sit under the default
    threshold: no statement is issued and no table gains a manual
    vacuum (last_vacuum is set by manual VACUUM only — autovacuum writes
    last_autovacuum)."""
    executed = _spy_executes(monkeypatch)
    first = ingest.run_ingest(trigger="manual")
    second = ingest.run_ingest(trigger="manual")
    assert first["error"] is None and second["error"] is None
    assert not executed, (
        "runs this small must skip under the default threshold")
    with db.viz_conn() as conn:
        vacuumed = conn.execute(
            "SELECT relname FROM pg_stat_user_tables "
            "WHERE relname = ANY(%s) AND last_vacuum IS NOT NULL",
            (["records", "tool_uses", "records_price", "files",
              "usage_rollup"],)).fetchall()
    assert vacuumed == [], (
        f"manual vacuum ran on {[row[0] for row in vacuumed]}")


def test_the_run_vacuums_and_analyzes_what_it_rewrote(
        fresh_db, mini_r2_env, monkeypatch, caplog):
    """Above the threshold the run vacuums (ANALYZE) every table its own
    counts say were rewritten — persist's five plus the rollup it wrote
    — and the logged count comes from the run's tracked file counts, not
    a table rescan."""
    monkeypatch.setattr(ingest_vacuum, "VACUUM_MIN_WRITTEN", 0)
    with caplog.at_level(logging.INFO, logger="claudit.ingest"):
        summary = ingest.run_ingest(trigger="manual")
    assert summary["error"] is None, summary
    assert f"{summary['inserted']} file(s) rewritten" in caplog.text, (
        "the vacuum's count must equal the run's tracked inserts")
    with db.viz_conn() as conn:
        checked = conn.execute(
            "SELECT relname, last_vacuum IS NOT NULL, "
            "last_analyze IS NOT NULL "
            "FROM pg_stat_user_tables WHERE relname = ANY(%s) "
            "ORDER BY relname",
            (["records", "tool_uses", "records_price", "files", "projects",
              "usage_rollup"],)).fetchall()
    assert len(checked) == 6, checked
    for relname, manual_vacuum, analyzed in checked:
        assert manual_vacuum, f"{relname} was not vacuumed by the run"
        assert analyzed, f"{relname} was not analyzed by the run"


# ---------------------------------------------------------------------------
# The between-batch interleave during a full reparse
# ---------------------------------------------------------------------------


def test_the_batch_vacuum_waits_for_its_cadence(monkeypatch):
    """The cadence gates the between-batch vacuum: nothing before it, a
    plain VACUUM (never ANALYZE there) at it, nothing until the cadence
    has accrued again from the last firing — and never after a single
    file."""
    assert ingest_vacuum.VACUUM_BATCH_FILES >= 2, (
        "the cadence must not be one file")
    executed = _spy_executes(monkeypatch)
    cadence = ingest_vacuum.VACUUM_BATCH_FILES
    batch = ingest_vacuum.BatchVacuum(enabled=True)
    batch.maybe(cadence - 1)
    assert not executed, "below the cadence nothing runs"
    batch.maybe(cadence)
    assert executed == [(ingest_vacuum.REPARSE_TABLES, False)], (
        "at the cadence the batch vacuum runs, plain VACUUM")
    batch.maybe(cadence + 1)
    assert len(executed) == 1, "one more file must not re-fire"
    batch.maybe(2 * cadence)
    assert len(executed) == 2, "the cadence accrues from the last firing"


def test_the_batch_vacuum_is_inert_when_the_run_is_not_full(monkeypatch):
    """Only a full-scope run (what a PARSER_VERSION bump produces) runs
    the interleave; an incremental run never does, however many files it
    reparses."""
    executed = _spy_executes(monkeypatch)
    batch = ingest_vacuum.BatchVacuum(enabled=False)
    batch.maybe(10 ** 9)
    assert not executed


def test_the_batch_vacuum_guard_skips_on_a_stop_signal(monkeypatch):
    """The between-batch vacuum consults the stop signal first and
    skips — never delays — on an abort."""
    executed = _spy_executes(monkeypatch)

    def raise_abort():
        raise RuntimeError("stop")

    batch = ingest_vacuum.BatchVacuum(enabled=True, check_shutdown=raise_abort)
    batch.maybe(10 ** 9)  # must not raise
    assert not executed


def test_a_failing_batch_vacuum_disables_itself_and_warns(monkeypatch):
    """One failure disables the interleave for the rest of the run (no
    retry storm) and books the warning the run summary carries."""
    attempts: list = []

    def boom(tables, analyze):
        attempts.append(1)
        raise RuntimeError("batch vacuum exploded")

    monkeypatch.setattr(ingest_vacuum, "_execute", boom)
    batch = ingest_vacuum.BatchVacuum(enabled=True)
    batch.maybe(10 ** 9)
    batch.maybe(2 * 10 ** 9)
    assert len(attempts) == 1, "a failed batch vacuum must not retry"
    warning = ingest_vacuum.take_warning()
    assert "vacuum failed" in (warning or ""), warning


def _assert_interleave_fires(monkeypatch):
    """Shared setup: seed a corpus, force a full reparse, spy on the
    executor with the cadence forced to one file. Returns the forced-
    reparse run's summary and the recorded (tables, analyze) calls."""
    assert ingest.run_ingest(trigger="manual")["error"] is None
    _force_full_reparse()
    executed = _spy_executes(monkeypatch)
    monkeypatch.setattr(ingest_vacuum, "VACUUM_BATCH_FILES", 1)
    # Shrink thread chunks so the corpus spans multiple batches.
    monkeypatch.setattr(ingest_fetch, "worker_count", lambda: 1)
    summary = ingest.run_ingest(trigger="manual")
    assert summary["error"] is None, summary
    return summary, executed


def test_the_between_batch_vacuum_respects_the_cadence(
        fresh_db, mini_r2_env, monkeypatch):
    """The default-side pair: at a cadence above the run's reparsed
    count, no interleave fires — only the tail's ANALYZE statement
    (forced to run here) appears."""
    assert mini_mirror.counts()["transcripts"] < ingest_vacuum.VACUUM_BATCH_FILES
    assert ingest.run_ingest(trigger="manual")["error"] is None
    _force_full_reparse()
    executed = _spy_executes(monkeypatch)
    monkeypatch.setattr(ingest_vacuum, "VACUUM_MIN_WRITTEN", 0)
    summary = ingest.run_ingest(trigger="manual")
    assert summary["error"] is None, summary
    assert executed, "the tail vacuum must still run"
    assert all(analyze for _tables, analyze in executed), (
        "with the cadence unmet no plain between-batch call may fire")


def _parse_with_pid(key, sidecar_key, etag=None, size=None, *, collection=None):
    parsed = ingest_fetch.fetch_and_parse(
        key, sidecar_key, ingest_fetch.fetch_with_retry,
        etag=etag, size=size, collection=collection)
    parsed["parse_pid"] = os.getpid()
    return parsed


@pytest.mark.parametrize("processes", [1, 2], ids=["threads", "process-pool"])
def test_both_pipelines_vacuum_between_batches(
        fresh_db, mini_r2_env, monkeypatch, processes):
    """Both pipelines interleave plain VACUUM over the reparse tables."""
    transcripts = mini_mirror.counts()["transcripts"]
    monkeypatch.setenv("CLAUDIT_INGEST_PARSE_PROCESSES", str(processes))
    assert ingest_fetch.parse_process_count() == processes
    parse_pids = []
    real_persist = ingest._persist  # pylint: disable=protected-access

    def persist(obj, proj, parsed, parser_version):
        parse_pids.append(parsed.pop("parse_pid"))
        return real_persist(obj, proj, parsed, parser_version)

    monkeypatch.setattr(ingest, "_fetch_and_parse", _parse_with_pid)
    monkeypatch.setattr(ingest, "_persist", persist)
    monkeypatch.setattr(ingest_vacuum, "VACUUM_BATCH_FILES", 1)
    if processes == 1:
        monkeypatch.setattr(ingest_fetch, "worker_count", lambda: 1)
        assert transcripts > 4, "thread pipeline needs >1 chunk"
    else:
        assert transcripts > 8, "pool pipeline needs >1 chunk"
    _summary, executed = _assert_interleave_fires(monkeypatch)
    assert len(parse_pids) == 2 * transcripts
    assert all((pid != os.getpid()) == (processes > 1) for pid in parse_pids)
    batch_calls = [tables for tables, analyze in executed if not analyze]
    assert batch_calls, f"processes={processes}: no between-batch vacuum"
    assert batch_calls[0] == ingest_vacuum.REPARSE_TABLES
