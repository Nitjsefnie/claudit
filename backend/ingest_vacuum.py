"""Post-ingest VACUUM of the tables a run actually rewrote (issue #959).

After the rollup rebuild a run issues ONE plain ``VACUUM (ANALYZE)``
statement over the tables its own tracked counts say it rewrote — and
skips the whole step when the run wrote little. During a full reparse
the run additionally vacuums BETWEEN persist batches, so the space its
per-file DELETEs leave is returned to the free space map before the
rest of the run inserts into it: without that interleave a full
corpus reparse's heap grows toward twice its live size.

Design constraints, all binding:

- **Outside every transaction.** VACUUM cannot run inside one, so the
  statement runs on a dedicated autocommit connection
  (``_vacuum_connection``), never through the ingest transaction's
  pooled connection.
- **No exclusive lock.** Plain VACUUM only: it takes SHARE UPDATE
  EXCLUSIVE, so readers and writers keep running. The relocating mode,
  which would take ACCESS EXCLUSIVE and rewrite the heap, is never
  issued from ingest — asserted over this module family's token stream
  by tests/test_ingest_vacuum.py.
- **Never fails the run.** A statement failure is logged and kept as
  the run summary's ``vacuum_warning``; the run stays green.
- **Never delays an abort.** Every entry point consults the caller's
  stop guard first and skips silently when it reports a stop.
- **Counts come from the run, never a rescan.** ``note_files`` carries
  the walk's own file counts (the ingest_runs vocabulary), note_phase
  carries what each derived-state phase returned; the decision reads
  only those.

Tables with no dead-tuple backlog are deliberately absent: sessions
gains only live inserts (its upsert is DO NOTHING), and the dim_* lookup
tables hold hundreds of rows — under autovacuum's absolute floor, so
they never queue a backlog like the issue's 256k dead rows did.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from contextlib import contextmanager

import psycopg

from backend import db
from backend.env_config import require_env

log = logging.getLogger("claudit.ingest")

# Rationale: the threshold below which a run's dead space is left to
# autovacuum — "the run wrote little". VACUUM's cost scales with the
# TABLE's heap, not with the rows just written, so a run that touched
# almost nothing would pay a full heap scan for a handful of dead
# tuples. 10,000 rows is ~0.6% of the 1.79M-row records table — far
# below autovacuum's 20% trigger, which is exactly where the issue's
# backlog sat (tool_uses waited at 256k dead rows) — so runs AT or
# above it leave dead space that would otherwise linger for hours,
# while the hourly trickle of a few changed files stays under it and
# costs nothing.
VACUUM_MIN_WRITTEN = 10_000

# Rationale: rows one reparsed file leaves dead. Its persist rewrites
# records, tool_uses AND records_price (per-file DELETE + INSERT, plus
# the files/projects upserts). Full-corpus ground truth on the
# issue's mirror: (1,789,661 records + 1,817,453 tool_uses +
# 1,789,661 sidecar rows) / 36,037 transcripts = 149.7 rows/file —
# 150 is that quotient rounded up, so the threshold sees a reparsed
# file's real footprint instead of a bare file count (a file count
# would understate a full reparse by ~50x).
REPARSE_ROWS_PER_FILE = 150

# Rationale: the between-batch VACUUM fires once this many files have
# been REPARSED since the last one — never after a single file. A
# reparse's per-file DELETE leaves its old rows dead and their pages
# unreusable until a VACUUM returns them to the free space map; over a
# full corpus reparse (36,037 files, ~5.4M rewritten rows) the heap
# would otherwise double. Vacuuming after every file would pay ~36k
# heap scans of a multi-GB heap; 4096 bounds that to ~9 scans per full
# reparse while keeping the dead-row backlog between two of them
# (~614k rows) far below the live table's size.
VACUUM_BATCH_FILES = 4096

# What one reparsed file rewrites: its records, tool_uses and
# records_price rows are deleted and re-inserted, and files/projects
# are upserted (a new row version either way).
REPARSE_TABLES = ("records", "tool_uses", "records_price", "files",
                  "projects")

# Which tables each derived-state phase rewrites, keyed by the phase
# name the rebuild loop records (its rows are what the phase returned).
_PHASE_TABLES: dict[str, tuple[str, ...]] = {
    "suppressed": ("records", "tool_uses", "records_price"),
    "reprice": ("records_price",),
    "aliases": ("files", "projects"),
    # adopt_original_models UPDATEs files.models beside the records'
    # model_id, so the canonical family covers the small files table too.
    "canonical": ("records", "tool_uses", "files"),
    "teammates": ("files",),
    # One entry per rollup: each rebuild DELETEs + INSERTs its own table.
    "usage_rollup": ("usage_rollup",),
    "tool_rollup": ("tool_rollup",),
    "tool_error_rollup": ("tool_error_rollup",),
    "dispatch_rollup": ("dispatch_rollup",),
    "dispatch_brief_rollup": ("dispatch_brief_rollup",),
    "latency_rollup": ("latency_rollup",),
    "ctx_cost_rollup": ("ctx_cost_rollup",),
    # agent_rollup also writes its dim (NOT EXISTS + DO NOTHING: only a
    # value the build introduces becomes a row).
    "agent_rollup": ("agent_rollup", "dim_agent_type"),
    "web_metrics_rollup": ("web_metrics_rollup",),
}

# The only identifiers a statement may ever name. VACUUM cannot
# parameterise, so the statement is built exclusively from this set.
_ALLOWED_TABLES = frozenset(REPARSE_TABLES) | {
    table for tables in _PHASE_TABLES.values() for table in tables}

# The running run's bookkeeping, reset at run open (begin_run) and
# drained by the tail vacuum: file counts from the walk, row counts
# from the rebuild phases, and the first vacuum failure's warning.
_FILES_CHURNED = 0
_PHASE_ROWS: dict[str, int] = {}
_WARNING: str | None = None


def begin_run() -> None:
    """Reset the vacuum bookkeeping when a run books its row."""
    global _FILES_CHURNED, _WARNING
    _FILES_CHURNED = 0
    _PHASE_ROWS.clear()
    _WARNING = None


def note_files(files_churned: int) -> None:
    """Carry the walk's own file counts into the vacuum's decision."""
    global _FILES_CHURNED
    _FILES_CHURNED += files_churned


def note_phase(phase: str, rows: int) -> int:
    """Add a phase's written-row count and return `rows` unchanged, so
    the rebuild loop can keep using its own value (reprice's promote_full
    gate keys on the CHANGED count, not the accumulated total)."""
    _PHASE_ROWS[phase] = _PHASE_ROWS.get(phase, 0) + rows
    return rows


def take_phase_rows() -> dict[str, int]:
    """Drain the recorded per-phase row counts (tests and the tail)."""
    rows = dict(_PHASE_ROWS)
    _PHASE_ROWS.clear()
    return rows


def take_warning() -> str | None:
    """Drain the first vacuum failure's run-summary warning."""
    global _WARNING
    warning, _WARNING = _WARNING, None
    return warning


def _note_warning(message: str) -> None:
    """Keep the FIRST failure's message; later ones still reach the log."""
    global _WARNING
    if _WARNING is None:
        _WARNING = message


def touched_tables(files_churned: int,
                   phase_rows: dict[str, int]) -> list[str]:
    """The tables this run rewrote rows in, derived only from its own
    counts — sorted so the statement and the logs are deterministic."""
    tables: set[str] = set()
    if files_churned:
        tables.update(REPARSE_TABLES)
    for phase, rows in phase_rows.items():
        if rows:
            tables.update(_PHASE_TABLES.get(phase, ()))
    return sorted(tables)


def _statement(tables: Iterable[str], analyze: bool) -> str:
    """One VACUUM statement over allow-listed identifiers only."""
    names = list(tables)
    if not names:
        raise ValueError("no tables to vacuum")
    unknown = [name for name in names if name not in _ALLOWED_TABLES]
    if unknown:
        raise ValueError(f"not an allow-listed ingest table: {unknown}")
    option = " (ANALYZE)" if analyze else ""
    return f"VACUUM{option} {', '.join(names)}"


@contextmanager
def _vacuum_connection():
    """A dedicated autocommit connection — outside every transaction.

    VACUUM refuses to run inside a transaction block, and the viz pool's
    connections are transactional, so the statement gets its own
    connection the way the ingest advisory lock does — opened, used and
    closed here, never pooled mid-transaction.
    """
    conn = psycopg.Connection.connect(
        require_env("CLAUDIT_DATABASE_URL_VIZ"), autocommit=True)
    try:
        yield conn
    finally:
        conn.close()


def _execute(tables: Iterable[str], analyze: bool) -> None:
    """Run the statement on the dedicated connection (the spy seam).

    The connection is registered with db's cancellation registry for the
    duration, so lifespan teardown's cancel_viz_queries() interrupts an
    in-flight statement instead of the stop window waiting it out — the
    same issue-#372 mechanism every pooled connection already has.
    """
    statement = _statement(tables, analyze)
    with _vacuum_connection() as conn:
        db.track_viz_conn(conn)
        try:
            conn.execute(db.sql_text(statement))
        finally:
            db.untrack_viz_conn(conn)
    log.info("ingest vacuum: %s", statement)


def run_vacuum(tables: Iterable[str], *, analyze: bool = True,
               check_shutdown: Callable[[], None] | None = None) -> bool:
    """Vacuum `tables` outside any transaction; never raise.

    The stop guard is consulted BEFORE the statement, so an aborting
    run skips its pending vacuums instead of delaying shutdown; any
    failure afterwards is logged and booked as the run's warning —
    a vacuum failure must not fail a run whose work is already done.
    """
    names = list(tables)
    if not names:
        return False
    if check_shutdown is not None:
        try:
            check_shutdown()
        except Exception:  # noqa: BLE001 - any stop signal skips the vacuum
            log.info("ingest vacuum skipped: a stop signal arrived "
                     "before it started")
            return False
    try:
        _execute(names, analyze)
    except Exception as exc:  # noqa: BLE001 - vacuum never fails the run
        _note_warning(f"vacuum failed: {type(exc).__name__}")
        log.warning("post-ingest vacuum failed; the run continues: %s",
                    exc, exc_info=True)
        return False
    return True


def _take_files() -> int:
    """Drain the recorded file-churn count."""
    global _FILES_CHURNED
    churned, _FILES_CHURNED = _FILES_CHURNED, 0
    return churned


def vacuum_after_run(*, check_shutdown: Callable[[], None] | None = None
                     ) -> None:
    """The ingest tail: VACUUM (ANALYZE) each table this run rewrote.

    Called once the rollup rebuild finishes, still inside the run's
    lifecycle — so finished_at stays the signal that the run's work,
    vacuums included, is complete. Skips (quietly, at info level) when
    the run's total written-row estimate sits under the threshold.
    """
    files_churned = _take_files()
    phase_rows = take_phase_rows()
    written = files_churned * REPARSE_ROWS_PER_FILE + sum(phase_rows.values())
    if written < VACUUM_MIN_WRITTEN:
        log.info("post-ingest vacuum skipped: the run wrote little "
                 "(about %d rows < %d)", written, VACUUM_MIN_WRITTEN)
        return
    tables = touched_tables(files_churned, phase_rows)
    if not tables:
        return
    log.info("post-ingest vacuum: %d file(s) rewritten, about %d rows "
             "in total; tables: %s", files_churned, written,
             ", ".join(tables))
    run_vacuum(tables, analyze=True, check_shutdown=check_shutdown)


class BatchVacuum:
    """The between-batch plain VACUUM of a full reparse (issue #959).

    `enabled` is the run's full scope — a PARSER_VERSION bump changes
    the derived-state fingerprint, which is what makes the run full —
    and the driver reports the CUMULATIVE reparsed-file count after
    each persist batch: the vacuum fires when that count has advanced
    by VACUUM_BATCH_FILES since the last firing, so an incremental
    run's small todo never reaches it. Plain VACUUM, no ANALYZE: the
    tail's ANALYZE follows the run anyway, and mid-run statistics would
    be stale the moment the next batch lands. One failure (or one stop
    signal) disables the interleave for the rest of the run — no retry
    storm, and the run's own tail statement still gets its chance.
    """

    def __init__(self, enabled: bool,
                 check_shutdown: Callable[[], None] | None = None) -> None:
        self._enabled = enabled
        self._check_shutdown = check_shutdown
        self._fired_at = 0

    def maybe(self, reparsed: int) -> None:
        """Fire if the cadence has accrued since the last firing."""
        if not self._enabled:
            return
        if reparsed - self._fired_at < VACUUM_BATCH_FILES:
            return
        self._fired_at = reparsed
        if not run_vacuum(REPARSE_TABLES, analyze=False,
                          check_shutdown=self._check_shutdown):
            self._enabled = False
