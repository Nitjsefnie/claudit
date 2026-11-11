"""Closed, whole web-metrics percentile buckets; no dirty-file scope.

Source rows are beacons, never records, so scope is the bounded raw retention
window. Store one row per display width because percentiles do not compose;
total does compose, for cumulative shift and long-task totals.

A fold revises closed buckets it reads whole from the oldest beacon on; an
earlier one (pruned, or holding that beacon) stays as stored, as partial
windows would freeze tail-only percentiles. RAW_KEEP_S includes TWO widest
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


def _fold(conn, bucket_width: int, horizon, read_from,
          oldest) -> tuple[int, int, int]:
    """Store every bucket of this width that has CLOSED, and drop the rest.

    Returns `(rows written, rows removed, buckets refused)`.

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
      fold cannot see — so the row would be a tail, not a bucket. Nothing
      marks a tail and no later fold reads it whole, so it would sit in the
      history for ever looking like a fact. Refusing is the
      honest half of the rule; `web_metrics.fold_read_from` is the half that
      keeps the refusal rare.
    * **The DELETEs and the INSERT share one filter**, so a bucket is
      stored whole or not at all.
    * **`read_from` is a full bucket width plus a fold interval behind the
      horizon**, which is what makes a bucket closing on THIS fold readable
      in full rather than merely probably readable.
    """
    half = timedelta(seconds=bucket_width / 2)
    closed_through = horizon - half
    conn.execute(
        db.sql_text(f"""
        DELETE FROM web_metrics_rollup
         WHERE bucket_s = {bucket_width} AND bucket > %s
        """), (closed_through,))
    params = {"read_from": read_from, "closed": closed_through,
              "covered": read_from + half,
              "revisable": oldest + half if oldest else None}
    covered = "bucket <= %(closed)s AND bucket >= %(covered)s"
    match = " AND ".join(f"b.{c} = r.{c}" for c in ("bucket",
                                                    *_GRAIN.split(", ")))
    row = conn.execute(
        db.sql_text(f"""
        WITH bands AS (
          SELECT to_timestamp(
                   floor(EXTRACT(EPOCH FROM w.ts) / {bucket_width})
                   * {bucket_width} + {bucket_width} / 2.0) AS bucket,
                 {_GRAIN},
                 COUNT(*) AS n,
                 PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY w.value) AS p50,
                 PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY w.value) AS p75,
                 SUM(w.value ORDER BY w.value) AS total
            FROM web_metrics w
           WHERE w.ts >= %(read_from)s
           GROUP BY 1, 2, 3, 4, 5
        ), emptied AS (
          -- A revision (this DELETE, the DO UPDATE) also needs the bucket to
          -- start at or after the oldest beacon: every prune took all rows
          -- below its cutoff, so such a bucket lost none even if the clock
          -- stepped back below an earlier cutoff. No beacon: NULL, no revision.
          DELETE FROM web_metrics_rollup r
           WHERE bucket_s = {bucket_width} AND {covered}
             AND bucket >= %(revisable)s
             AND NOT EXISTS (SELECT 1 FROM bands b WHERE {match})
          RETURNING 1
        ), written AS (
        INSERT INTO web_metrics_rollup
          (bucket_s, bucket, metric, part, region, phase, n, p50, p75, total)
        SELECT {bucket_width}, bucket, metric, part, region, phase,
               n, p50, p75, total
          FROM bands
           -- Closed, and the span the window has to cover, in the midpoint
           -- the table stores (an index bound): `bucket >= read_from + W/2`
           -- IS `s >= read_from`. A bucket below it cannot be read whole, and a
           -- short row is indistinguishable from a whole one, so it is
           -- neither stored nor rebuilt. A LATER pass -- whose window reaches
           -- further back while the beacons are still there -- stores it
           -- whole; and if they are gone by then it stays absent, which is a
           -- gap a reader can see and a short row is not.
         WHERE {covered}
        -- Rewrite only a row whose raw rows changed. total sums in value
        -- order: float addition depends on order, and the scan's does not
        -- stay fixed, so an unchanged bucket must re-sum bit-identically.
        ON CONFLICT (bucket_s, bucket, metric, part, region, phase)
        DO UPDATE SET (n, p50, p75, total) = (EXCLUDED.n, EXCLUDED.p50,
                                              EXCLUDED.p75, EXCLUDED.total)
         WHERE web_metrics_rollup.bucket >= %(revisable)s
           AND (web_metrics_rollup.n, web_metrics_rollup.p50,
                web_metrics_rollup.p75, web_metrics_rollup.total)
               IS DISTINCT FROM (EXCLUDED.n, EXCLUDED.p50,
                                 EXCLUDED.p75, EXCLUDED.total)
        RETURNING 1
        )
        SELECT (SELECT COUNT(*) FROM written), (SELECT COUNT(*) FROM emptied)
        """), params).fetchone()
    return int(row[0]) if row else 0, int(row[1]) if row else 0, \
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

    Returns rows written or removed. Runs in its own transaction on the viz pool: it
    touches neither `records` nor the run's derived-state fingerprint, so a
    failure here costs the panel a cycle of freshness, not the ingest.
    """
    now = web_metrics.utcnow()
    horizon = web_metrics.rollup_horizon(now)
    cutoff = web_metrics.retention_cutoff(now)
    written = removed = refused = 0
    with db.viz_conn() as conn:
        # The window may reach further back than the cutoff when a stalled
        # pass left the working set wider than its nominal width, and it is
        # deliberately NOT what the prune below deletes at: pruning at the
        # fold's own read window would pin the working set at its widest and
        # the next pass would read just as far again, for ever.
        row = conn.execute("SELECT MIN(ts) FROM web_metrics").fetchone()
        oldest = row[0] if row else None
        read_from = web_metrics.fold_read_from(oldest, now)
        for bucket_width in LATENCY_BUCKETS:
            stored, dropped, turned_away = _fold(
                conn, bucket_width, horizon, read_from, oldest)
            written += stored
            removed += dropped
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
    log.info("rebuild_web_metrics_rollup: %d rows, %d removed, "
             "%d buckets refused", written, removed, refused)
    return written + removed
