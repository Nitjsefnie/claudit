"""The reprice pass: recompute rate-derived stored state (issue #193).

A rate change must rePRICE stored records instead of re-parsing every
R2 object: this pass recomputes each stale row's cost from its OWN
stored token columns — no R2 access, no reparse. A row is stale when
its stored pricing_version differs from constants.PRICING_VERSION (NULL
counts as stale), and persist-time stamping (backend/ingest_persist.py)
keeps freshly written rows non-stale.

The pass recomputes RATE-DERIVED STORED STATE keyed off pricing_version
staleness: cost_usd AND records_price.long_context (a rate-derived flag,
issue #194). For the meter's models the flag is a pure function of
stored columns, and it rides the SAME selection as the cost. The
per-row update is assembled in one place (_record_updates), so a flag
column rides the same selection, guard and keyset as the cost.

Batching (issue #339): REPRICE_BATCH rows per transaction, keyset-
paginated on (file_id, line_num), one commit per batch — a failure's
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

Pair-qualified staleness (issue #351): before the keyset loop, one
GROUP BY scan classifies the stale (model, provider) pairs — counting
each pair's stale rows (issue #400) — and ONE set-based UPDATE restamps
every stale row whose stored rate_fingerprint
equals its pair's current fingerprint — the fingerprint covers every
rate input resolve() consults plus the source of the pricing modules
and of this pass (rate_fingerprint.hashed_modules), so the recomputation
for those rows is the identity and reading them into Python would spend
~20us per row advancing a marker. The keyset loop
then sees only rows whose pair's rate data moved (or whose version
spelling the SQL guard cannot prove safe), reads them, recomputes,
and stamps the current fingerprint beside the version; when the
restamp covered every counted row the loop is skipped outright, since
its terminating SELECT would otherwise walk the entire PK index only
to prove emptiness (issue #400).

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

from backend import constants, db, dims, ingest_vacuum, pricing, rate_fingerprint, timing
from backend.ingest_abort import IngestAborted

log = logging.getLogger("claudit.ingest")


# Rows per batch/transaction. Read at call time, so a test can shrink
# it (monkeypatch the module attribute).
REPRICE_BATCH = 20_000

# All three of the pass's writes land on records_price (#959 R4): a bump
# rewrites only the sidecar's narrow pages (HOT, #350), never records.
# INNER JOIN: a records row without its price row is not a candidate.
_SELECT_SQL = """
    SELECT r.file_id, r.line_num, dm.value, r.fresh_tokens,
           r.cache_creation_tokens, r.cache_read_tokens, r.output_tokens,
           r.eph5_tokens, r.eph1h_tokens, r.ts, rp.long_context,
           dp.value, rp.cost_usd, pv.value, df.value, rp.request_fee_usd,
           rp.long_context_input_mult, rp.long_context_output_mult,
           r.web_search_requests
      FROM records r JOIN records_price rp ON rp.file_id = r.file_id AND rp.line_num = r.line_num
      JOIN dim_model dm ON dm.id = r.model_id
      LEFT JOIN dim_provider dp ON dp.id = r.provider_id
      LEFT JOIN dim_pricing_version pv ON pv.id = rp.pricing_version_id
      LEFT JOIN dim_rate_fingerprint df ON df.id = rp.rate_fingerprint_id
     WHERE (r.file_id, r.line_num) > (%s, %s) AND rp.pricing_version_id IS DISTINCT FROM %s
     ORDER BY r.file_id, r.line_num
     LIMIT %s
"""

# A batch's unchanged rows: only the staleness marker moved, so one
# statement advances it for the whole set. Issue #351: the rate
# fingerprint rides the same write, so a row restamped by the keyset
# path carries the fingerprint of the data its cost was priced
# under — provable clean set-based next run. Dim FK ids (#959).
_SQL_RESTAMP = """
    UPDATE records_price rp
       SET pricing_version_id = %s, rate_fingerprint_id = d.f
      FROM unnest(%s::bigint[], %s::bigint[], %s::bigint[]) AS d(k, n, f)
     WHERE rp.file_id = d.k AND rp.line_num = d.n
"""

# A batch's moved rows: recomputed values travel as arrays, one
# statement writes the whole set through the sidecar's own PK. The new
# fingerprint rides beside the version, exactly as persist stamps it.
_SQL_REPRICE = """
    UPDATE records_price rp
       SET cost_usd = d.cost, long_context = d.flag,
           request_fee_usd = NULL,
           long_context_input_mult = d.input_mult,
           long_context_output_mult = d.output_mult,
           pricing_version_id = %s, rate_fingerprint_id = d.f
      FROM unnest(%s::bigint[], %s::bigint[], %s::float8[], %s::boolean[],
                  %s::float8[], %s::float8[], %s::bigint[])
           AS d(k, n, cost, flag, input_mult, output_mult, f)
     WHERE rp.file_id = d.k AND rp.line_num = d.n
"""


# Phase A's classification: the stale (model, provider) pairs, each
# with its stale-row count (issue #400) — few rows, one GROUP BY scan
# over two narrow columns (measured 0.66-1.4 s at 1.375M rows in
# every staleness state probed). The count lets reprice_stale skip
# the keyset loop outright when the clean restamp covered every stale
# row: the loop's terminating SELECT otherwise walks the entire PK
# index just to prove no rows remain (3.6 s at production shape, up
# to 66.9 s under load). Dims resolve the texts (#959); staleness and
# the pair's join both ride records_price.
_SQL_STALE_PAIRS = """
    SELECT dm.value, COALESCE(dp.value, ''), count(*)
      FROM records r JOIN records_price rp ON rp.file_id = r.file_id AND rp.line_num = r.line_num
      JOIN dim_model dm ON dm.id = r.model_id
      LEFT JOIN dim_provider dp ON dp.id = r.provider_id
     WHERE rp.pricing_version_id IS DISTINCT FROM %s GROUP BY 1, 2
"""

# Phase A clean-restamps rows with the current pair fingerprint and complete
# meter provenance: pricing inputs and logic fingerprint to an identity
# recomputation. Legacy metered rows without pairs use the keyset path to fill
# them. Only 1-9 digit versions <= V enter SQL; odd spellings and newer rows
# remain for Python's rollback guard (the 9-digit bound avoids int4 overflow);
# the digit test guards the cast inside a CASE; texts read back through their
# dims (#959). cand AS MATERIALIZED fences the small side: loose in a FROM
# list the planner priced this statement's coherence predicates at ~45 rows
# of 1.79M and nested records_price inside the pair loop (19+ minutes measured
# on the mirror before the fence); fenced, every join order left is linear.
_SQL_CLEAN_RESTAMP = """
    WITH cand AS MATERIALIZED (
      SELECT r.file_id, r.line_num, r.provider_id, d.p, d.f
        FROM unnest(%s::text[], %s::text[], %s::text[]) AS d(m, p, f)
        JOIN dim_model dm ON dm.value = d.m
        JOIN records r ON r.model_id = dm.id
    )
    UPDATE records_price rp
       SET pricing_version_id = %s
      FROM cand JOIN dim_rate_fingerprint df ON df.value = cand.f
     WHERE rp.file_id = cand.file_id AND rp.line_num = cand.line_num AND rp.rate_fingerprint_id = df.id
       AND COALESCE((SELECT value FROM dim_provider WHERE id = cand.provider_id), '') = cand.p
       AND rp.pricing_version_id IS DISTINCT FROM %s
       AND (SELECT CASE WHEN value ~ '^[0-9]{1,9}$' THEN value::int END
              FROM dim_pricing_version WHERE id = rp.pricing_version_id) <= %s
       AND COALESCE(rp.long_context, FALSE) = (rp.long_context_input_mult IS NOT NULL)
       AND COALESCE(rp.long_context, FALSE) = (rp.long_context_output_mult IS NOT NULL)
"""


class _StaleRow(NamedTuple):
    """One stale row's stored columns (#959: file_id keyset, dim TEXTs)."""

    file_id: int
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
    rate_fingerprint: str | None
    request_fee_usd: Decimal | None
    long_context_input_mult: float | None = None
    long_context_output_mult: float | None = None
    web_search_requests: int | None = None


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
    """Derive one stale row's rate state from its stored columns.

    Members recompute their threshold flag. Non-members keep NULL/FALSE;
    a stored TRUE is checked against the global threshold to unbill lapsed
    members (issue #833). The provider does not affect flag derivation.
    One resolution supplies token and web-search rates. A flagged row also
    stores its effective meter pair, preserving provenance across batches.
    """
    unsplit_create = max(
        0, row.cache_creation_tokens - row.eph5_tokens - row.eph1h_tokens)
    if pricing.is_long_context_model(row.model):
        long_context = (row.fresh_tokens + row.cache_creation_tokens
                        + row.cache_read_tokens
                        > pricing.long_context_threshold(row.model))
    elif row.long_context:
        # A non-member's stored TRUE can only be the Codex path's
        # threshold test (issue #194) — or a lapsed member's leftover,
        # which keeps over-billing (issue #833). Re-deriving that test
        # is the one non-member rule every format's reparse prices
        # identically: the row unbills below the global threshold and a
        # genuinely above-threshold Codex row keeps its meter.
        long_context = (row.fresh_tokens + row.cache_creation_tokens
                        + row.cache_read_tokens
                        > pricing.long_context_threshold(row.model))
    else:
        long_context = row.long_context
    # One resolution per row: the same Resolution prices the tokens and
    # names the fee (the parse path's rule).
    res = pricing.resolve(row.model, row.ts, row.provider)
    cost = pricing.compute_cost(
        row.model,
        fresh=row.fresh_tokens,
        output=row.output_tokens,
        eph5=row.eph5_tokens,
        eph1h=row.eph1h_tokens,
        unsplit_create=unsplit_create,
        read=row.cache_read_tokens,
        adjustments=pricing.CostAdjustments(
            long_context=bool(long_context),
            web_search_requests=row.web_search_requests,
        ),
        res=res,
    )
    input_mult, output_mult = (pricing.long_context_factors(row.model)
                               if long_context else (None, None))
    return {
        "cost_usd": round(cost, 6),
        "long_context": long_context,
        "long_context_input_mult": input_mult,
        "long_context_output_mult": output_mult,
        # Retire the legacy note-derived fee column without dropping it;
        # new prices come from web_search_requests times the row's rate.
        "request_fee_usd": None,
        "pricing_version": constants.PRICING_VERSION,
    }


def _row_is_unchanged(row: _StaleRow, updates: dict) -> bool:
    """Whether recomputing the row yielded its stored state, so only the
    staleness marker needs advancing. Also imported unchanged by the
    frozen differential — see _record_updates.

    The stored cost is NUMERIC(12,6): the old pass wrote
    round(cost, 6) into it, and reading that value back through
    float() yields the same double the round produced, so the equality
    is exact for every row this pass (or its predecessor) wrote.
    """
    return (float(row.cost_usd) == updates["cost_usd"]
            and row.long_context == updates["long_context"]
            and row.long_context_input_mult
            == updates["long_context_input_mult"]
            and row.long_context_output_mult
            == updates["long_context_output_mult"]
            and (row.request_fee_usd is None)
            == (updates["request_fee_usd"] is None))


def reprice_stale(should_stop: Callable[[], bool | None] | None = None  # pylint: disable=too-many-locals,too-many-branches,too-many-statements
                  ) -> int:
    """Recompute cost_usd for every stale record; return the count whose
    rate-derived data CHANGED.

    Each batch computes its rows' updates, splits them into an unchanged
    restamp set and a changed reprice set, and writes both set-based in
    the batch's transaction. The keyset cursor advances to the last row
    of each batch whether the row was restamped, repriced or skipped by
    the rollback guard, so the pass always terminates and a guard skip
    never spins. The loop runs only while rows remain after Phase A's
    clean restamp: when the restamp covered every stale row the pairs
    scan counted, the loop is skipped outright (issue #400).

    should_stop, when given, is consulted at the top of every batch
    iteration: a truthy return — or an exception it raises — unwinds the
    pass via IngestAborted. Batches already committed persist, the
    interrupted batch never opens, and the next run converges (the
    ingest stop contract, issue #103 — no new recovery logic). ingest
    passes its _check_shutdown, which raises; direct and test callers
    pass nothing, which prices the whole table in one go.

    With CLAUDIT_TIMING on, the pass emits one TIMING line whose marks
    (select, fetch, pairs, clean, recompute, restamp, moved,
    commit) accumulate across
    batches and must account for the measured total — the gap is the
    unattributed residue. Python CPU time rides the same line. A
    skipped loop leaves only the pairs/clean marks (issue #400).
    """
    ph = timing.Phases("reprice", logger=log, account=True) \
        if timing.TIMING_ON else None
    marks: dict[str, float] = {}
    cpu0 = time.process_time()
    # "failed" until the pass finishes normally: an unexpected exception
    # (a DB error) must not land in the TIMING line labelled complete.
    outcome = "failed"
    changed = 0
    restamped = 0
    skipped = 0
    batches = 0
    rows_seen = 0
    clean = 0
    after_key: tuple[int, int] = (-1, 0)
    try:
        # Phase A (issue #351): classify the stale (model, provider)
        # pairs — few rows, one GROUP BY scan that also counts each
        # pair's stale rows (issue #400) — then restamp, in ONE
        # set-based statement, every stale row whose stored fingerprint
        # still equals its pair's current fingerprint: the fp covers
        # every rate input resolve() consults plus the pricing modules'
        # source, so its recomputed cost IS its stored cost by
        # construction and only the marker needs advancing. The keyset
        # loop below then reads only the rows a fingerprint change or
        # an unprovable version spelling left behind.
        t0 = time.perf_counter()
        stale_total = 0
        with db.viz_conn() as c:
            version_id = dims.dim_id(
                c, "dim_pricing_version", constants.PRICING_VERSION)
            pair_counts = c.execute(
                _SQL_STALE_PAIRS,
                (version_id,)).fetchall()
            triples = [
                (model, provider,
                 rate_fingerprint.pair_fingerprint(
                     model, None if provider == "" else provider))
                for model, provider, _count in pair_counts]
            stale_total = sum(count for _, _, count in pair_counts)
            fp_done = time.perf_counter()
            if triples:
                clean = c.execute(
                    _SQL_CLEAN_RESTAMP,
                    ([m for m, _, _ in triples],
                     [p for _, p, _ in triples],
                     [f for _, _, f in triples],
                     version_id, version_id,
                     int(constants.PRICING_VERSION))).rowcount
            c.commit()
        if ph is not None:
            marks["pairs"] = fp_done - t0
            marks["clean"] = time.perf_counter() - fp_done
        # Issue #400: when the clean restamp covered every stale row the
        # scan counted (clean == stale_total), no stale row remains and
        # the loop's first SELECT would walk the entire PK index only to
        # prove emptiness — measured 3.6 s at production shape, up to
        # 66.9 s under load. The skip is exact: every row the SQL set
        # cannot restamp — a pair whose fingerprint moved, a NULL or
        # oddly-spelled version (NULL ~ regex is not TRUE), a plain-digit
        # version above the binary's, or a digit run past int4 — leaves
        # clean < stale_total, and the loop runs exactly as before.
        while clean != stale_total:
            if should_stop is not None and should_stop():
                outcome = "aborted"
                raise IngestAborted("shutdown requested")
            with db.viz_conn() as c:
                version_id = dims.dim_id(
                    c, "dim_pricing_version", constants.PRICING_VERSION)
                t0 = time.perf_counter()
                cur = c.execute(
                    _SELECT_SQL,
                    (after_key[0], after_key[1], version_id,
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
                fp_map = dims.dim_ids(
                    c, "dim_rate_fingerprint",
                    [rate_fingerprint.pair_fingerprint(row.model, row.provider)
                     for row in rows])
                restamp_keys: list[tuple[int, int, int]] = []
                moved: list[tuple[int, int, float, bool | None,
                                  float | None, float | None, int]] = []
                for row in rows:
                    if _stored_pricing_version_is_newer(
                            row.pricing_version, constants.PRICING_VERSION):
                        skipped += 1
                        continue
                    row_fp = rate_fingerprint.pair_fingerprint(row.model,
                                                               row.provider)
                    updates = _record_updates(row)
                    if _row_is_unchanged(row, updates):
                        restamp_keys.append((row.file_id, row.line_num,
                                             fp_map[row_fp]))
                    else:
                        moved.append((row.file_id, row.line_num,
                                      updates["cost_usd"],
                                      updates["long_context"],
                                      updates["long_context_input_mult"],
                                      updates["long_context_output_mult"],
                                      fp_map[row_fp]))
                if ph is not None:
                    marks["recompute"] = (marks.get("recompute", 0.0)
                                          + time.perf_counter() - t0)
                t0 = time.perf_counter()
                if restamp_keys:
                    c.execute(_SQL_RESTAMP,
                              (version_id,
                               [k for k, _, _ in restamp_keys],
                               [n for _, n, _ in restamp_keys],
                               [f for _, _, f in restamp_keys]))
                if ph is not None:
                    marks["restamp"] = (marks.get("restamp", 0.0)
                                        + time.perf_counter() - t0)
                t0 = time.perf_counter()
                if moved:
                    c.execute(_SQL_REPRICE,
                              (version_id,
                               [r[0] for r in moved],
                               [r[1] for r in moved],
                               [r[2] for r in moved],
                               [r[3] for r in moved],
                               [r[4] for r in moved],
                               [r[5] for r in moved],
                               [r[6] for r in moved]))
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
            after_key = (last.file_id, last.line_num)
        outcome = "complete"
    finally:
        if ph is not None:
            for label, seconds in marks.items():
                ph.mark(label, seconds)
            ph.done(batches=batches, rows=rows_seen, changed=changed,
                    clean_rows=clean, outcome=outcome,
                    cpu=f"{time.process_time() - cpu0:.1f}s")
    log.info(
        "reprice: %d record(s) repriced (rate-derived data changed), "
        "%d restamped only", changed, restamped + clean)
    if skipped:
        log.info(
            "reprice: skipped %d record(s) priced by a NEWER "
            "PRICING_VERSION (rollback guard)", skipped)
    # The return stays CHANGED-only — promote_full and the cache
    # invalidation key on it — but the restamp sets wrote records_price
    # rows too, and the post-ingest vacuum's count must see them.
    ingest_vacuum.note_phase("reprice", clean + restamped)
    return changed
