"""Plain VACUUM of tables the ingest run rewrote.

After rollups, run one VACUUM (ANALYZE) only when tracked write counts
justify its heap-scan cost. During full reparses, vacuum between
persist batches to reclaim dead per-file rows before later inserts;
otherwise the heap can grow toward twice its live size.

Use a dedicated autocommit connection: VACUUM cannot run in a
transaction. Plain VACUUM preserves reader/writer access; never use
the relocating mode that takes ACCESS EXCLUSIVE. Tests enforce that
constraint over this module family's token stream.

Consult the stop guard before every entry point; statement failures
become vacuum_warning, never the run error. Use the run's own counts,
not rescans. Sessions only gain live inserts, and small dim tables
have no comparable dead-tuple backlog, so neither is vacuumed here."""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from contextlib import contextmanager

import psycopg

from backend import db
from backend.env_config import require_env

log = logging.getLogger("claudit.ingest")

# Rationale — leave small runs to autovacuum: VACUUM scans the table heap, not
# just the newly dead rows. A low absolute row threshold reclaims dead
# space below autovacuum's percentage trigger without charging every
# hourly trickle a full heap scan.
VACUUM_MIN_WRITTEN = 10_000

# Rationale — estimate dead rows per reparsed file across records, tool_uses and
# records_price, plus identity upserts. A file count alone understates
# the rewrite footprint; this estimate converts it to the same row
# scale as the vacuum threshold.
REPARSE_ROWS_PER_FILE = 150

# Rationale — vacuum between batches only after this many reparsed files. Per-file
# DELETEs leave dead rows whose pages need free-space reclamation before
# later inserts, or a full reparse can approach twice the live heap.
# Batching bounds the backlog without paying a heap scan per file.
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
    global _FILES_CHURNED, _WARNING
    _FILES_CHURNED = 0
    _PHASE_ROWS.clear()
    _WARNING = None


def note_files(files_churned: int) -> None:
    global _FILES_CHURNED
    _FILES_CHURNED += files_churned


def note_phase(phase: str, rows: int) -> int:
    """Add a phase's written-row count and return `rows` unchanged, so
    the rebuild loop can keep using its own value (reprice's promote_full
    gate keys on the CHANGED count, not the accumulated total)."""
    _PHASE_ROWS[phase] = _PHASE_ROWS.get(phase, 0) + rows
    return rows


def take_phase_rows() -> dict[str, int]:
    rows = dict(_PHASE_ROWS)
    _PHASE_ROWS.clear()
    return rows


def take_warning() -> str | None:
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
    same cancellation registry used by pooled connections.
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
    """The between-batch plain VACUUM of a full reparse.

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
