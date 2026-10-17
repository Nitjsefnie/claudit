"""Recompute rate-derived stored state without R2 access or reparsing.

Staleness is a pricing-version mismatch (NULL included). Preserve rows
whose integer version is newer than this binary. Derive cost, meter
flag and factor provenance from each row's own stored columns.

Classify stale model/provider pairs once and restamp matching complete
fingerprints set-wise. Fingerprints cover rate inputs and pricing/reprice
logic, proving recomputation is identity. Skip the keyset scan when
that restamp covered every counted stale row.

Remaining rows use (file_id, line_num) keyset batches, one transaction
and at most two set-based UPDATEs per batch: unchanged restamps and
changed values. Advance past guard skips; never OFFSET-scan. Committed
batches survive failure and the next run converges.

Return only rows whose rate-derived data changed, so marker-only
restamps do not trigger full rebuild, cache invalidation or broadcast.
All written rows receive the current version and pair fingerprint."""
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

# All writes target records_price: repricing rewrites narrow sidecar
# pages with HOT updates, never records. INNER JOIN excludes records
# without a price row.
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

# Restamp unchanged rows in one statement, advancing version and rate
# fingerprint together so the next run can prove them clean set-wise.
# Both stamps are dim FK ids.
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


# Classify stale model/provider pairs and count each pair in one
# GROUP BY. If clean restamping covers all counted rows, skip the
# keyset scan that would traverse the PK just to prove emptiness.
# Resolve texts through dims; staleness and pair joins use records_price.
_SQL_STALE_PAIRS = """
    SELECT dm.value, COALESCE(dp.value, ''), count(*)
      FROM records r JOIN records_price rp ON rp.file_id = r.file_id AND rp.line_num = r.line_num
      JOIN dim_model dm ON dm.id = r.model_id
      LEFT JOIN dim_provider dp ON dp.id = r.provider_id
     WHERE rp.pricing_version_id IS DISTINCT FROM %s GROUP BY 1, 2
"""

# Clean-restamp only matching fingerprints with complete meter
# provenance; incomplete pairs go through the keyset path. SQL accepts
# only 1-9 digit versions <= V, with the cast inside CASE to avoid int4
# overflow; Python guards other spellings and newer versions.
# cand AS MATERIALIZED fences the small side: underestimated coherence
# predicates otherwise permit quadratic nested loops over records_price.
# Read texts through dims.
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
    """One stale row's stored columns: file_id keyset and dimension texts."""

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
    ingest._stored_version_is_newer applies to parser_version: a rollback's older binary must not rewrite rows it cannot
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
    members. The provider does not affect flag derivation.
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
        # threshold test or a lapsed member's leftover
        # flag that would keep over-billing. Re-deriving that test
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
        # Clear the note-derived fee column: bill web_search_requests at the
        # record's rate instead, preserving the column for storage compatibility.
        "request_fee_usd": None,
        "pricing_version": constants.PRICING_VERSION,
    }


def _row_is_unchanged(row: _StaleRow, updates: dict) -> bool:
    """Whether stored state equals recomputation, requiring only restamping.

    The frozen differential imports this unchanged beside _record_updates.
    Cost is stored as NUMERIC(12,6); round(cost, 6) read back through float
    produces the same double, so equality is exact for persisted values."""
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
    scan counted, the loop is skipped outright.

    should_stop, when given, is consulted at the top of every batch
    iteration: a truthy return — or an exception it raises — unwinds the
    pass via IngestAborted. Batches already committed persist, the
    interrupted batch never opens, and the next run converges (the
    ingest stop contract). ingest
    passes its _check_shutdown, which raises; direct and test callers
    pass nothing, which prices the whole table in one go.

    With CLAUDIT_TIMING on, the pass emits one TIMING line whose marks
    (select, fetch, pairs, clean, recompute, restamp, moved,
    commit) accumulate across
    batches and must account for the measured total — the gap is the
    unattributed residue. Python CPU time rides the same line. A
    skipped loop leaves only the pairs/clean marks.
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
        # Phase A: classify the stale (model, provider)
        # pairs — few rows, one GROUP BY scan that also counts each
        # pair's stale rows — then restamp, in ONE
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
        # When clean == stale_total, no stale row remains: skip the PK scan
        # that would only prove emptiness. Changed fingerprints, NULL/odd
        # versions, newer versions and digit runs past int4 leave clean below
        # the count and therefore take the guarded keyset path.
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
