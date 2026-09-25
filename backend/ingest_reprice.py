"""The reprice pass: recompute rate-derived stored state (issue #193).

A rate change must rePRICE stored records instead of re-parsing every
R2 object: this pass recomputes each stale row's cost from its OWN
stored token columns — no R2 access, no reparse. A row is stale when
its stored pricing_version differs from constants.PRICING_VERSION (NULL
counts as stale), and persist-time stamping (backend/ingest_persist.py)
keeps freshly written rows non-stale.

The pass recomputes RATE-DERIVED STORED STATE keyed off pricing_version
staleness. cost_usd today; issue #194 will make records.long_context a
pure function of stored columns, and that flag rides the SAME selection
— a rate-derived flag recomputed beside cost. The per-row update is
therefore assembled in one place (_record_updates), and the UPDATE's
SET list is generated from _SET_COLUMNS, so a flag column joins the
batch without reworking the batching, the keyset or the guard.

Batching: REPRICE_BATCH rows per transaction, keyset-paginated on
(file_key, line_num), one commit per batch — a failure's blast radius is
one batch, and OFFSET pagination (which would rescan) is never used.

Rollback guard, mirroring ingest._stored_version_is_newer (issue #118):
a stored pricing_version that parses as an int and is GREATER than
constants.PRICING_VERSION was priced by a NEWER build, and updating it
would clobber pricing this binary cannot reproduce. Such rows are never
updated — they are counted, logged, and the keyset advances past them.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import NamedTuple

from backend import constants, db, pricing

log = logging.getLogger("claudit.ingest")

# Rows per batch/transaction. Read at call time, so a test can shrink
# it (monkeypatch the module attribute).
REPRICE_BATCH = 20_000

# The columns a repriced row's SET list carries. Identifiers only — the
# SQL text is assembled from these once at import; values always travel
# as %s/%(name)s parameters.
_SET_COLUMNS: tuple[str, ...] = ("cost_usd", "pricing_version")

_SELECT_SQL = """
    SELECT file_key, line_num, model, fresh_tokens, cache_creation_tokens,
           cache_read_tokens, output_tokens, eph5_tokens, eph1h_tokens,
           ts, long_context, provider, pricing_version
      FROM records
     WHERE (file_key, line_num) > (%s, %s)
       AND pricing_version IS DISTINCT FROM %s
     ORDER BY file_key, line_num
     LIMIT %s
"""

_UPDATE_SQL = (
    "UPDATE records SET "
    + ", ".join(f"{column} = %({column})s" for column in _SET_COLUMNS)
    + " WHERE file_key = %(file_key)s AND line_num = %(line_num)s"
)


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

    cost_usd today (issue #193). When #194 teaches the pass to recompute
    a rate-derived flag (records.long_context as a pure function of
    stored columns), the flag's column and value join here and ride the
    same batch: the SET list is generated from _SET_COLUMNS, so neither
    the batching, the keyset nor the guard changes.
    """
    unsplit_create = max(
        0, row.cache_creation_tokens - row.eph5_tokens - row.eph1h_tokens)
    cost = pricing.compute_cost(
        row.model,
        fresh=row.fresh_tokens,
        output=row.output_tokens,
        eph5=row.eph5_tokens,
        eph1h=row.eph1h_tokens,
        unsplit_create=unsplit_create,
        read=row.cache_read_tokens,
        ts=row.ts,
        long_context=bool(row.long_context),
        provider=row.provider,
    )
    return {
        "cost_usd": round(cost, 6),
        "pricing_version": constants.PRICING_VERSION,
    }


def reprice_stale() -> int:
    """Recompute cost_usd for every stale record; return the count.

    The keyset cursor advances to the last row of each batch whether the
    row was updated or skipped by the rollback guard, so the pass always
    terminates and a guard skip never spins.
    """
    repriced = 0
    skipped = 0
    after_key: tuple[str, int] = ("", 0)
    while True:
        with db.viz_conn() as c:
            raw_rows = c.execute(
                _SELECT_SQL,
                (after_key[0], after_key[1], constants.PRICING_VERSION,
                 REPRICE_BATCH),
            ).fetchall()
            if not raw_rows:
                break
            rows = [_StaleRow(*raw) for raw in raw_rows]
            updates = []
            for row in rows:
                if _stored_pricing_version_is_newer(
                        row.pricing_version, constants.PRICING_VERSION):
                    skipped += 1
                    continue
                updates.append({
                    **_record_updates(row),
                    "file_key": row.file_key,
                    "line_num": row.line_num,
                })
            if updates:
                with c.cursor() as cur:
                    cur.executemany(_UPDATE_SQL, updates)
            c.commit()
        repriced += len(updates)
        last = rows[-1]
        after_key = (last.file_key, last.line_num)
    if skipped:
        log.info(
            "reprice: skipped %d record(s) priced by a NEWER "
            "PRICING_VERSION (rollback guard)", skipped)
    return repriced
