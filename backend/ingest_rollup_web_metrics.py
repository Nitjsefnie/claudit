"""Closed, whole web-metrics percentile buckets; no dirty-file scope.

Source rows are beacons, never records, so scope is the bounded raw retention
window. Store one row per display width because percentiles do not compose;
total does compose, for cumulative shift and long-task totals.

Only closed buckets read in full may be stored or revised. Partial windows
would permanently freeze tail-only percentiles. RAW_KEEP_S includes TWO widest
bucket widths plus a fold interval so a newly closed bucket is fully readable.
For stalled folds, read back to the oldest surviving beacon and still refuse
incomplete spans. DELETE and INSERT must use the same span bound
so deletion cannot discard a bucket the INSERT cannot rebuild.

The readout unions uncovered raw beacons with stored buckets, labels the blend
exact: false, and uses raw rows alone when the requested range fits.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from backend import db, web_metrics
from backend.constants import LATENCY_BUCKETS

log = logging.getLogger("claudit.ingest")

# The five grain columns, spelled once: the DELETE and the INSERT have to agree
# on them exactly, and a mismatch would leave a stale row behind a fresh one.
_GRAIN = "metric, part, region, phase"


def _fold(conn, bucket_width: int, horizon, read_from) -> tuple[int, int]:
    """Store every bucket of this width that has CLOSED, and drop the rest.

    Returns `(rows written, buckets refused)` — see `_refused` for the second.

    A bucket is `[s, s + W)` with `s = floor(epoch / W) * W`, stored at its
    midpoint. It is complete when its whole span is behind `horizon`, i.e.
    `s + W <= horizon`, i.e. `s + W/2 <= horizon - W/2` — and that is exactly
    the bound below, in the midpoint the table stores. A bucket past the
    bound is still growing, so storing it would publish a percentile over a
    population that is about to change under the reader.

    Four facts fix the placement, and none of them is a tuning choice:

    * **Store only closed buckets.** An open bucket is rewritten on every
      fold with a strictly larger population, so a reader sees a moving
      number; worse, a fold whose window reached behind the raw cutoff would
      rebuild it from rows the prune had already taken.
    * **Store only buckets the window READS WHOLE.** `read_from` is the
      oldest row on the table, and a bucket starting below it has beacons the
      fold cannot see — so the row would be a tail, not a bucket. There is no
      marker distinguishing the two and `DO NOTHING` never revises, so a tail
      would sit in the history for ever looking like a fact. Refusing is the
      honest half of the rule; `web_metrics.fold_read_from` is the half that
      keeps the refusal rare.
    * **The DELETE bound is the same expression as the INSERT's filter.** A
      bucket is either stored whole or not at all, and the two cannot
      disagree about which.
    * **`read_from` is a full bucket width plus a fold interval behind the
      horizon**, which is what makes a bucket closing on THIS fold readable
      in full rather than merely probably readable.
    """
    closed_through = horizon - timedelta(seconds=bucket_width / 2)
    conn.execute(
        db.sql_text(f"""
        DELETE FROM web_metrics_rollup
         WHERE bucket_s = {bucket_width} AND bucket > %s
        """), (closed_through,))
    cur = conn.execute(
        db.sql_text(f"""
        WITH bands AS (
          SELECT to_timestamp(
                   floor(EXTRACT(EPOCH FROM w.ts) / {bucket_width})
                   * {bucket_width} + {bucket_width} / 2.0) AS bucket,
                 {_GRAIN},
                 COUNT(*) AS n,
                 PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY w.value) AS p50,
                 PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY w.value) AS p75,
                 SUM(w.value) AS total
            FROM web_metrics w
           WHERE w.ts >= %s
           GROUP BY 1, 2, 3, 4, 5
        )
        INSERT INTO web_metrics_rollup
          (bucket_s, bucket, metric, part, region, phase, n, p50, p75, total)
        SELECT {bucket_width}, bucket, metric, part, region, phase,
               n, p50, p75, total
          FROM bands
         WHERE bucket <= %s
           -- The span the window has to cover, in the midpoint the table
           -- stores: `epoch(bucket) - W/2 >= epoch(read_from)` IS
           -- `s >= read_from`. A bucket below it cannot be read whole, and a
           -- short row is indistinguishable from a whole one, so it is not
           -- stored at all. A LATER pass -- whose window reaches further back
           -- while the beacons are still there -- stores it whole; and if
           -- they are gone by then it stays absent, which is a gap a reader
           -- can see and a short row is not.
           AND EXTRACT(EPOCH FROM bucket) - {bucket_width} / 2.0
               >= EXTRACT(EPOCH FROM %s)
        -- A closed bucket is written once and never revised, so a fold that
        -- finds one already stored is a fold whose window reached back over
        -- a boundary it has no business re-reading. The stored row is the
        -- one that was complete when it was written; skipping is what makes
        -- the "stored whole, or not at all" invariant hold.
        ON CONFLICT (bucket_s, bucket, metric, part, region, phase)
        DO NOTHING
        """), (read_from, closed_through, read_from))
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0, \
        _refused(conn, bucket_width, horizon, read_from)


def _refused(conn, bucket_width: int, horizon, read_from) -> int:
    """Count buckets of this width refused by the span bound.

    Log the count in /health so absent stored buckets are distinguishable
    from buckets with no beacons. A refusal is expected, not an error.

    A refused bucket starts below and overlaps read_from; its surviving
    beacons lie in [read_from, read_from + W). Entirely older buckets have
    no surviving rows because pruning is never earlier than read_from.
    Scan that one-width beacon range rather than the full working set;
    keep the closed-bucket predicate if the horizon changes."""
    row = conn.execute(
        db.sql_text(f"""
        SELECT COUNT(DISTINCT to_timestamp(
                        floor(EXTRACT(EPOCH FROM w.ts) / {bucket_width})
                        * {bucket_width} + {bucket_width} / 2.0))
          FROM web_metrics w
         WHERE w.ts >= %s AND w.ts < %s
           AND floor(EXTRACT(EPOCH FROM w.ts) / {bucket_width})
               * {bucket_width} < EXTRACT(EPOCH FROM %s)
           AND floor(EXTRACT(EPOCH FROM w.ts) / {bucket_width})
               * {bucket_width} + {bucket_width} <= EXTRACT(EPOCH FROM %s)
        """), (read_from, read_from + timedelta(seconds=bucket_width),
               read_from, horizon)).fetchone()
    return int(row[0]) if row and row[0] else 0


def rebuild_web_metrics_rollup() -> int:
    """Fold the retained beacons into every stored bucket, then prune.

    Returns rows written. Runs in its own transaction on the viz pool: it
    touches neither `records` nor the run's derived-state fingerprint, so a
    failure here costs the panel a cycle of freshness, not the ingest.
    """
    now = web_metrics.utcnow()
    horizon = web_metrics.rollup_horizon(now)
    cutoff = web_metrics.retention_cutoff(now)
    written = refused = 0
    with db.viz_conn() as conn:
        # The window may reach further back than the cutoff when a stalled
        # pass left the working set wider than its nominal width, and it is
        # deliberately NOT what the prune below deletes at: pruning at the
        # fold's own read window would pin the working set at its widest and
        # the next pass would read just as far again, for ever.
        read_from = web_metrics.fold_read_from(conn, now)
        for bucket_width in LATENCY_BUCKETS:
            stored, turned_away = _fold(conn, bucket_width, horizon,
                                        read_from)
            written += stored
            refused += turned_away
        # Pruned after the fold, in the same transaction, at the retention
        # cutoff. The two instants differ by design and the difference is the
        # slack that lets a bucket close.
        conn.execute(
            "DELETE FROM web_metrics WHERE ts < %s", (cutoff,))
        conn.commit()
    # The refused count is the half a reader would otherwise have to guess
    # at: a bucket absent from the stored history is indistinguishable from
    # one that never had a beacon, and this is where that becomes visible.
    log.info("rebuild_web_metrics_rollup: %d rows, %d buckets refused",
             written, refused)
    return written
