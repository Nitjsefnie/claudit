"""Stored rate-state derivation and set-based writes shared by ingest and reprice."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import NamedTuple

from backend import constants, dims, pricing, rate_fingerprint


SELECT_RATE_STATE = """
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
"""

# A batch's moved rows: recomputed values travel as arrays, one
# statement writes the whole set through the sidecar's own PK. The new
# fingerprint rides beside the version, exactly as persist stamps it.
SQL_REPRICE = """
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


class RateRow(NamedTuple):
    """One row's stored columns: file_id keyset and dimension texts."""

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


def stored_pricing_version_is_newer(stored: str | None,
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


def record_updates(row: RateRow) -> dict:
    """Derive one row's rate state from its stored columns.

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


def rate_update(row: RateRow, fingerprint_id: int) -> tuple:
    """The sidecar UPDATE's array row, derived once from the stored inputs."""
    updates = record_updates(row)
    return (row.file_id, row.line_num, updates["cost_usd"],
            updates["long_context"], updates["long_context_input_mult"],
            updates["long_context_output_mult"], fingerprint_id)


def write_rate_updates(conn, version_id: int, updates: list[tuple]) -> int:
    """Write a derived set inside the caller's transaction; never commit here."""
    if not updates:
        return 0
    return conn.execute(SQL_REPRICE, (version_id, *map(list, zip(*updates)))).rowcount


def reprice_adopted(conn, keys: list[tuple[int, int]]) -> int:
    """Reprice the adopted set under its new stored models in the same transaction."""
    if not keys:
        return 0
    rows = [RateRow(*raw) for raw in conn.execute(
        SELECT_RATE_STATE + """
         WHERE (r.file_id, r.line_num) IN (
           SELECT * FROM unnest(%s::bigint[], %s::bigint[]))
        """, tuple(map(list, zip(*keys)))).fetchall()]
    fingerprints = [rate_fingerprint.pair_fingerprint(row.model, row.provider)
                    for row in rows]
    fp_ids = dims.dim_ids(conn, "dim_rate_fingerprint", fingerprints)
    version_id = dims.dim_id(conn, "dim_pricing_version", constants.PRICING_VERSION)
    return write_rate_updates(conn, version_id, [
        rate_update(row, fp_ids[fp]) for row, fp in zip(rows, fingerprints)])
