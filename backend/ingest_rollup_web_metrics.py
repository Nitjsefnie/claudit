"""Web-metrics percentile rollups, rebuilt over the retained beacon window.

The only thing that distinguishes this pass from the seven hour-keyed rollups
in `ingest_rollup_hourly` is where its source rows come from. `usage_rollup`
and its siblings are derived from `records` and scoped by the run's dirty
hours; these are derived from `web_metrics`, which no ingest touches, so there
is no dirty-file scope to narrow by. Two consequences shape the pass:

* **The scope is the retention window, not the run.** Every stored bucket
  overlapping `web_metrics.RETENTION_S` is replaced from the raw rows inside
  it. That is a bounded amount of work — the raw table is pruned to the same
  window, so the fold never sees more than a couple of days of beacons, and
  beacons are a few rows per page view — and it is correct without a watermark:
  a beacon that lands after the pass that would have folded it is folded by the
  next one, and the window is wide enough that the delay is at most an hour.

* **The prune is not the fold's problem.** The fold reads exactly
  `ts >= cutoff` and the prune deletes exactly `ts < cutoff`, so the two
  populations are disjoint and no ordering between them can lose a row. They
  share one transaction anyway, so a crash leaves at worst raw rows the next
  pass re-folds — never a bucket that will not be recomputed.

Percentiles do not compose, so one row is stored per display-bucket width,
matching `latency_rollup` and for the same reason. `total` is the one stored
column that composes, and it is what the layout-shift and long-task readouts
need: a cumulative layout shift is a sum of shift values, not a percentile of
them.
"""
from __future__ import annotations

import logging

from backend import db, web_metrics
from backend.constants import LATENCY_BUCKETS

log = logging.getLogger("claudit.ingest")

# The five grain columns, spelled once: the DELETE and the INSERT have to agree
# on them exactly, and a mismatch would leave a stale row behind a fresh one.
_GRAIN = "metric, part, region, phase"


def _fold(conn, bucket_width: int, cutoff) -> int:
    """Replace every stored bucket of this width overlapping the window."""
    conn.execute(
        db.sql_text(f"""
        DELETE FROM web_metrics_rollup
         WHERE bucket_s = {bucket_width}
           AND bucket >= to_timestamp(
                 floor(EXTRACT(EPOCH FROM %s::timestamptz) / {bucket_width})
                 * {bucket_width} + {bucket_width} / 2.0)
        """), (cutoff,))
    cur = conn.execute(
        db.sql_text(f"""
        INSERT INTO web_metrics_rollup
          (bucket_s, bucket, metric, part, region, phase, n, p50, p75, total)
        SELECT {bucket_width},
               to_timestamp(
                 floor(EXTRACT(EPOCH FROM w.ts) / {bucket_width})
                 * {bucket_width} + {bucket_width} / 2.0) AS bucket,
               {_GRAIN},
               COUNT(*),
               PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY w.value),
               PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY w.value),
               SUM(w.value)
          FROM web_metrics w
         WHERE w.ts >= %s
         GROUP BY 2, 3, 4, 5, 6
        """), (cutoff,))
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def rebuild_web_metrics_rollup() -> int:
    """Fold the retained beacons into every stored bucket, then prune.

    Returns rows written. Runs in its own transaction on the viz pool: it
    touches neither `records` nor the run's derived-state fingerprint, so a
    failure here costs the panel a cycle of freshness, not the ingest.
    """
    now = web_metrics.utcnow()
    cutoff = web_metrics.retention_cutoff(now)
    written = 0
    with db.viz_conn() as conn:
        for bucket_width in LATENCY_BUCKETS:
            written += _fold(conn, bucket_width, cutoff)
        # Pruned after the fold, in the same transaction. The order is a
        # readability choice, not a correctness one — see the module
        # docstring's note on the two disjoint windows.
        conn.execute(
            "DELETE FROM web_metrics WHERE ts < %s", (cutoff,))
        conn.commit()
    log.info("rebuild_web_metrics_rollup: %d rows", written)
    return written
