"""The reprice pass: recompute rate-derived stored state (issue #193).

A rate change must rePRICE stored records instead of re-parsing every
R2 object: this pass recomputes each stale row's cost from its OWN
stored token columns — no R2 access, no reparse. A row is stale when
its stored pricing_version differs from constants.PRICING_VERSION (NULL
counts as stale), and persist-time stamping (backend/ingest_persist.py)
keeps freshly written rows non-stale.

The pass recomputes RATE-DERIVED STORED STATE keyed off pricing_version
staleness: cost_usd AND records.long_context (a rate-derived flag,
issue #194). For the meter's models the flag is a pure function of
stored columns, and it rides the SAME selection as the cost. The
per-row update is assembled in one place (_record_updates), so a flag
column rides the same selection, guard and keyset as the cost.

Batching (issue #339): REPRICE_BATCH rows per transaction, keyset-
paginated on (file_key, line_num), one commit per batch — a failure's
blast radius is one batch, and OFFSET pagination (which would rescan)
is never used. Each batch's rows are written SET-BASED: a row whose
recomputed state equals its stored state is only re-stamped with the
current PRICING_VERSION (one UPDATE joining unnest() over the batch's
keys), and a row whose cost or long-context flag moved is written by
one UPDATE ... FROM unnest(...) carrying the batch's recomputed values.
Neither write re-states one row per statement: a batch's rows cost
at most two UPDATE statements however large REPRICE_BATCH is, so the
hourly PRICING bumps that move no stored pair's rates issue O(batches)
statements instead of one UPDATE per row.

The return count is rows whose rate-derived data CHANGED — the restamp
advances the staleness marker without touching user-visible state, so
the ingest's promote_full, response-cache invalidation and ingest_done
broadcast (which key on this count) fire only when repricing moved a
cost or a flag. Rows written, changed or merely restamped, still all
carry the current version on success.

Rollback guard, mirroring ingest._stored_version_is_newer (issue #118):
a stored pricing_version that parses as an int and is GREATER than
constants.PRICING_VERSION was priced by a NEWER build, and updating it
would clobber pricing this binary cannot reproduce. Such rows are never
updated — they are counted, logged, and the keyset advances past them.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import NamedTuple

from backend import constants, db, pricing, timing

log = logging.getLogger("claudit.ingest")


class IngestAborted(Exception):
    """Internal signal: the shutdown event was seen between bounded steps.

    Defined HERE, below backend.ingest in the import graph, so the
    reprice pass can raise it without importing ingest back — a cycle
    pylint refuses. backend.ingest re-exports it, so
    `ingest.IngestAborted` resolves exactly as before. run_ingest_locked
    catches it SEPARATELY from the generic fatal path: the row is closed
    as aborted, but the rebuild, the cache invalidation and the
    ingest_done broadcast are all skipped — an aborted run must not tell
    clients data changed.
    """


# Rows per batch/transaction. Read at call time, so a test can shrink
# it (monkeypatch the module attribute).
REPRICE_BATCH = 20_000

_SELECT_SQL = """
    SELECT file_key, line_num, model, fresh_tokens, cache_creation_tokens,
           cache_read_tokens, output_tokens, eph5_tokens, eph1h_tokens,
           ts, long_context, provider, cost_usd, pricing_version
      FROM records
     WHERE (file_key, line_num) > (%s, %s)
       AND pricing_version IS DISTINCT FROM %s
     ORDER BY file_key, line_num
     LIMIT %s
"""

# A batch's unchanged rows: the staleness marker is the only stored
# state that moved, so one statement advances it for the whole set.
_SQL_RESTAMP = """
    UPDATE records r
       SET pricing_version = %s
      FROM unnest(%s::text[], %s::bigint[]) AS d(k, n)
     WHERE r.file_key = d.k
       AND r.line_num = d.n
"""

# A batch's moved rows: recomputed values travel as arrays, one
# statement writes the whole set through the same PK join.
_SQL_REPRICE = """
    UPDATE records r
       SET cost_usd = d.cost, long_context = d.flag, pricing_version = %s
      FROM unnest(%s::text[], %s::bigint[], %s::float8[], %s::boolean[])
           AS d(k, n, cost, flag)
     WHERE r.file_key = d.k
       AND r.line_num = d.n
"""


class _StaleRow(NamedTuple):
    """One stale record's stored columns, positionally bound."""

    file_key: str
    line_num: int
    model: str
    fresh_tokens: int
    cache_creation_tokens: int
    cache_read_tokens: int
    output_tokens: int
    eph5_tokens: int
    eph1h_tokens: int
    ts: datetime | None
    long_context: bool | None
    provider: str | None
    cost_usd: Decimal
    pricing_version: str | None


def _stored_pricing_version_is_newer(stored: str | None,
                                     current: str) -> bool:
    """Whether a stored pricing_version was written by a NEWER build.

    A value that does not parse as an int cannot be shown newer, so the
    ordinary staleness decision applies — exactly the rule
    ingest._stored_version_is_newer applies to parser_version (issue
    #118): a rollback's older binary must not rewrite rows it cannot
    write whole.
    """
    if stored is None:
        return False
    try:
        return int(stored) > int(current)
    except (TypeError, ValueError):
        return False


def _record_updates(row: _StaleRow) -> dict:
    """The columns one record's reprice writes, from its stored values —
    THE one assembly point.

    cost_usd and the long-context flag together (issue #194): for the
    meter's models the flag is a pure function of stored columns —
    fresh + cache_creation + cache_read against the threshold — and
    rides the same selection, batch, keyset and guard as the cost. The
    re-derivation is for LANE rows only (issue #249): the meter is
    applied by parse_codex, never by the Claude path, so NULL is the
    Claude format's parse-stored marker and a row whose flag is NULL
    keeps it — pricing flat — whatever its model and tally; re-deriving
    it there would diverge from what a reparse stores. A row of a model
    whose card carries no meter, and a row naming a provider host (which
    prices by that host's card, not this one), likewise keeps its stored
    flag untouched: a Claude record's NULL stays NULL and a Kimi
    record's FALSE stays FALSE.
    """
    unsplit_create = max(
        0, row.cache_creation_tokens - row.eph5_tokens - row.eph1h_tokens)
    if (row.long_context is not None
            and row.model in pricing.LONG_CONTEXT_MODELS
            and not row.provider):
        long_context = (row.fresh_tokens + row.cache_creation_tokens
                        + row.cache_read_tokens
                        > pricing.LONG_CONTEXT_THRESHOLD)
    else:
        long_context = row.long_context
    cost = pricing.compute_cost(
        row.model,
        fresh=row.fresh_tokens,
        output=row.output_tokens,
        eph5=row.eph5_tokens,
        eph1h=row.eph1h_tokens,
        unsplit_create=unsplit_create,
        read=row.cache_read_tokens,
        ts=row.ts,
        long_context=bool(long_context),
        provider=row.provider,
    )
    return {
        "cost_usd": round(cost, 6),
        "long_context": long_context,
        "pricing_version": constants.PRICING_VERSION,
    }


def _row_is_unchanged(row: _StaleRow, updates: dict) -> bool:
    """Whether recomputing the row yielded its stored state, so only the
    staleness marker needs advancing.

    The stored cost is NUMERIC(12,6): the old pass wrote
    round(cost, 6) into it, and reading that value back through
    float() yields the same double the round produced, so the equality
    is exact for every row this pass (or its predecessor) wrote.
    """
    return (float(row.cost_usd) == updates["cost_usd"]
            and row.long_context == updates["long_context"])


def reprice_stale(should_stop: Callable[[], bool | None] | None = None  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
                  ) -> int:
    """Recompute cost_usd for every stale record; return the count whose
    rate-derived data CHANGED.

    Each batch computes its rows' updates, splits them into an unchanged
    restamp set and a changed reprice set, and writes both set-based in
    the batch's transaction. The keyset cursor advances to the last row
    of each batch whether the row was restamped, repriced or skipped by
    the rollback guard, so the pass always terminates and a guard skip
    never spins.

    should_stop, when given, is consulted at the top of every batch
    iteration: a truthy return — or an exception it raises — unwinds the
    pass via IngestAborted. Batches already committed persist, the
    interrupted batch never opens, and the next run converges (the
    ingest stop contract, issue #103 — no new recovery logic). ingest
    passes its _check_shutdown, which raises; direct and test callers
    pass nothing, which prices the whole table in one go.

    With CLAUDIT_TIMING on, the pass emits one TIMING line whose marks
    (select, fetch, recompute, restamp, moved, commit) accumulate across
    batches and must account for the measured total — the gap is the
    unattributed residue. Python CPU time rides the same line.
    """
    ph = timing.Phases("reprice", logger=log, account=True) \
        if timing.TIMING_ON else None
    marks: dict[str, float] = {}
    cpu0 = time.process_time()
    outcome = "complete"
    changed = 0
    restamped = 0
    skipped = 0
    batches = 0
    rows_seen = 0
    after_key: tuple[str, int] = ("", 0)
    try:
        while True:
            if should_stop is not None and should_stop():
                outcome = "aborted"
                raise IngestAborted("shutdown requested")
            with db.viz_conn() as c:
                t0 = time.perf_counter()
                cur = c.execute(
                    _SELECT_SQL,
                    (after_key[0], after_key[1], constants.PRICING_VERSION,
                     REPRICE_BATCH),
                )
                if ph is not None:
                    marks["select"] = (
                        marks.get("select", 0.0) + time.perf_counter() - t0)
                t0 = time.perf_counter()
                raw_rows = cur.fetchall()
                if ph is not None:
                    marks["fetch"] = (
                        marks.get("fetch", 0.0) + time.perf_counter() - t0)
                if not raw_rows:
                    break
                rows_seen += len(raw_rows)
                t0 = time.perf_counter()
                rows = [_StaleRow(*raw) for raw in raw_rows]
                restamp_keys: list[tuple[str, int]] = []
                moved: list[tuple[str, int, float, bool | None]] = []
                for row in rows:
                    if _stored_pricing_version_is_newer(
                            row.pricing_version, constants.PRICING_VERSION):
                        skipped += 1
                        continue
                    updates = _record_updates(row)
                    if _row_is_unchanged(row, updates):
                        restamp_keys.append((row.file_key, row.line_num))
                    else:
                        moved.append((row.file_key, row.line_num,
                                      updates["cost_usd"],
                                      updates["long_context"]))
                if ph is not None:
                    marks["recompute"] = (marks.get("recompute", 0.0)
                                          + time.perf_counter() - t0)
                t0 = time.perf_counter()
                if restamp_keys:
                    c.execute(_SQL_RESTAMP,
                              (constants.PRICING_VERSION,
                               [k for k, _ in restamp_keys],
                               [n for _, n in restamp_keys]))
                if ph is not None:
                    marks["restamp"] = (marks.get("restamp", 0.0)
                                        + time.perf_counter() - t0)
                t0 = time.perf_counter()
                if moved:
                    c.execute(_SQL_REPRICE,
                              (constants.PRICING_VERSION,
                               [r[0] for r in moved],
                               [r[1] for r in moved],
                               [r[2] for r in moved],
                               [r[3] for r in moved]))
                if ph is not None:
                    marks["moved"] = (marks.get("moved", 0.0)
                                      + time.perf_counter() - t0)
                t0 = time.perf_counter()
                c.commit()
                if ph is not None:
                    marks["commit"] = (marks.get("commit", 0.0)
                                       + time.perf_counter() - t0)
            changed += len(moved)
            restamped += len(restamp_keys)
            batches += 1
            last = rows[-1]
            after_key = (last.file_key, last.line_num)
    finally:
        if ph is not None:
            for label, seconds in marks.items():
                ph.mark(label, seconds)
            ph.done(batches=batches, rows=rows_seen, changed=changed,
                    outcome=outcome, cpu=f"{time.process_time() - cpu0:.1f}s")
    log.info(
        "reprice: %d record(s) repriced (rate-derived data changed), "
        "%d restamped only", changed, restamped)
    if skipped:
        log.info(
            "reprice: skipped %d record(s) priced by a NEWER "
            "PRICING_VERSION (rollback guard)", skipped)
    return changed
