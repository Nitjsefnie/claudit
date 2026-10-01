"""Web-metrics percentile rollups: one stored row per CLOSED display bucket.

The only thing that distinguishes this pass from the seven hour-keyed rollups
in `ingest_rollup_hourly` is where its source rows come from. `usage_rollup`
and its siblings are derived from `records` and scoped by the run's dirty
hours; these are derived from `web_metrics`, which no ingest phase writes, so
there is no dirty-file scope to narrow by. The scope is the retention window
instead, and it is bounded: the raw table is pruned to
`web_metrics.RAW_KEEP_S` and beacons are a few rows per page view.

**A bucket is stored whole, and only a fold that saw more of it may revise
it.** The fold that CLOSES a bucket writes it; the next finds it and leaves it
alone. Everything below is that one rule seen from three sides.

*Why closed, and not "rebuild the window".* A bucket is a percentile over a
population, so it is only a fact once the population has stopped changing. An
open bucket is rewritten on every pass with a strictly larger population and a
reader sees a number move under them. Worse — and this is the defect a review
caught on the first version of this pass — a fold that reached back over
beacons the prune had already taken rebuilt each bucket from a strictly
narrower slice, so every stored percentile ended up describing the bucket's
LAST HOUR, at every width, permanently. The 6-, 12- and 24-hour rollups were
keeping a third, a quarter and under a quarter of their beacons.

*Why the raw table outlives the horizon, by TWO widths.* A bucket becomes
complete at exactly the instant its last beacon is pruned, so a raw table
pruned AT the horizon never has a closable bucket's rows on the fold that
closes it. One width of slack is not enough either, and that was the second
version of this defect: a closed bucket `[s, s+W)` starts after
`now - 2W`, so a read window one width back leaves the newest
closing bucket's oldest beacons outside it — the fold stores it short, and
NOTHING repairs it, because the window advances with `now` and a bucket the
window has cut off stays cut off. Three skipped hourly folds were enough to
strand the 24-hour bucket, the width the default `all` view reads, with 22 of
its 24 beacons, silently and permanently. Hence
`RAW_KEEP_S = 2 * widest bucket + fold interval`, which makes a
partial read of a closed bucket impossible at every fold that HAPPENS.

*Why the fold still checks the span it read (issue #475).* Two widths is
arithmetic about folds that run, and it says nothing about a pass that does
NOT run: the window advances with `now` while the beacons stay put, so a fold
stalled longer than the thirty hours between `now - RAW_KEEP_S` and a
bucket's start resumes with its window inside that bucket, stores its tail,
and `DO NOTHING` freezes the short row beside whole ones for ever. So the
span is checked, not assumed — a bucket the window did not cover is not
stored at all — and the window reaches back to the oldest beacon on the
table while a stalled pass has left the working set wider than nominal, so
the refusal is rare and a later pass usually stores the bucket whole.

*Why the DELETE and the INSERT share one expression.* A bucket is either
stored whole or not at all. If the delete bound and the insert filter were
computed differently, one fold would delete a bucket the insert could not fully
rebuild — the same data loss, one bucket to the left.

Percentiles do not compose, so one row is stored per display-bucket width,
matching `latency_rollup` and for the same reason. `total` is the one stored
column that composes, and it is what the layout-shift and long-task readouts
need: a cumulative layout shift is a sum of shift values, not a percentile of
them.

What this pass does NOT hold is the newest beacons: a bucket closes only
once its span has passed, so everything since the last fold is in the raw
table and in no bucket yet. That is not a gap either — `/api/web-metrics`
unions the raw tail back in — but the union is a BLEND of a stored bucket's
percentile with a live one over a different population, so it is reported
with `exact: false` and the panel says so on its face. A range the raw working
set covers outright is served from the raw table alone and IS exact. Both
facts are the reader's, in `backend/api_web_metrics.py`.
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


def _fold(conn, bucket_width: int, horizon, read_from) -> int:
    """Store every bucket of this width that has CLOSED, and drop the rest.

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
      keeps the refusal rare (issue #475).
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
           -- stored at all and the next pass -- whose window reaches further
           -- back while the beacons are still there -- stores it whole.
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
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def rebuild_web_metrics_rollup() -> int:
    """Fold the retained beacons into every stored bucket, then prune.

    Returns rows written. Runs in its own transaction on the viz pool: it
    touches neither `records` nor the run's derived-state fingerprint, so a
    failure here costs the panel a cycle of freshness, not the ingest.
    """
    now = web_metrics.utcnow()
    horizon = web_metrics.rollup_horizon(now)
    cutoff = web_metrics.retention_cutoff(now)
    written = 0
    with db.viz_conn() as conn:
        # The window may reach further back than the cutoff when a stalled
        # pass left the working set wider than its nominal width, and it is
        # deliberately NOT what the prune below deletes at: pruning at the
        # fold's own read window would pin the working set at its widest and
        # the next pass would read just as far again, for ever (issue #475).
        read_from = web_metrics.fold_read_from(conn, now)
        for bucket_width in LATENCY_BUCKETS:
            written += _fold(conn, bucket_width, horizon, read_from)
        # Pruned after the fold, in the same transaction, at the retention
        # cutoff. The two instants differ by design and the difference is the
        # slack that lets a bucket close.
        conn.execute(
            "DELETE FROM web_metrics WHERE ts < %s", (cutoff,))
        conn.commit()
    log.info("rebuild_web_metrics_rollup: %d rows", written)
    return written
