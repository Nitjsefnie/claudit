"""The `/api/metrics` beacon sink and the `/api/web-metrics` readout.

Two routes, deliberately thin. The sink's decisions all live in
`backend.web_metrics` (the closed vocabulary, the clamps, the per-user cap);
this module is the HTTP shape around them. The readout is the same
rollup-or-live fork `/api/reply-latency` uses, for the same reason, and the
caveat the fork carries is written out at `_pool`.

Auth is `session.auth_middleware` by path prefix, not a decorator (see
`backend/api.py`), which is what makes the sink's two gates free:

* **Same-origin.** `check_origin` runs on every non-GET/HEAD/OPTIONS request,
  so a beacon POST is refused cross-origin exactly like `/login` is.
* **A session.** Also the middleware, and deliberately NOT relaxed here: an
  open unauthenticated write endpoint on a dashboard is not a trade worth
  making for a few rows of telemetry. A guest session qualifies, and
  `_guest_denied` leaves `/api/metrics` and `/api/web-metrics` alone, because
  a beacon names a journey and not a project, a session or a file — there is
  nothing in the payload for a guest to reach.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from starlette.concurrency import run_in_threadpool

from backend import db, web_metrics
from backend.api_common import _bucket_seconds, _iso, _parse_range
from backend.cache import cache_response
from backend.constants import LATENCY_BUCKETS

router = APIRouter()


@router.post("/metrics", status_code=202)
async def post_metrics(request: Request) -> dict:
    """Accept one batch of browser performance beacons.

    202 rather than 200: the rows are stored, but the percentile rollup they
    feed is rebuilt by the next ingest, so nothing downstream of this response
    is final. A `sendBeacon` caller never reads the status either way.

    Every validation failure is a 400 naming the field — see
    `web_metrics.BeaconError`'s docstring for why a vocabulary violation is
    loud while a wild value is clamped.
    """
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(400, "body must be JSON") from None
    try:
        beacons = web_metrics.parse_batch(payload)
    except web_metrics.BeaconError as error:
        raise HTTPException(400, str(error)) from None

    user_id = int(getattr(request.state, "user_id", None) or 0)
    stored = await run_in_threadpool(_write, user_id, beacons)
    return {"ok": True, "stored": stored}


def _write(user_id: int, beacons: list[tuple]) -> int:
    """The sink's write, on the threadpool the blocking psycopg call needs."""
    with db.viz_conn() as conn:
        if web_metrics.over_cap(conn, user_id, web_metrics.utcnow()):
            # Refused rather than clamped: a session already holding this
            # many beacons is a loop, not a busy user, and the panel reads
            # the rollup either way.
            raise HTTPException(
                429, "too many stored beacons for this session")
        stored = web_metrics.store(conn, user_id, beacons)
        conn.commit()
    return stored


@router.get("/web-metrics")
@cache_response
def web_metrics_readout(
    rng: str = Query("7d", alias="range"),
) -> dict:
    """Journey timings and observed blocking, as percentiles.

    `range` picks the display bucket exactly as every other panel does, and a
    width the rollup does not store takes a live pass — `latency_rollup`
    leaves 300 out for the same reason, and this endpoint falls back the same
    way.
    """
    delta = _parse_range(rng)
    since, bucket_s = (datetime.now(timezone.utc) - delta,
                       _bucket_seconds(delta))
    compute = (_series_from_rollup if bucket_s in LATENCY_BUCKETS
               else _series_live)
    return compute(rng, bucket_s, since)


def _row(ts, metric, part, region, phase, n, p50, p75, total) -> dict:
    return {
        "ts": _iso(ts) if ts is not None else None, "metric": metric,
        "part": part, "region": region, "phase": phase, "n": int(n or 0),
        "p50": float(p50 or 0.0), "p75": float(p75 or 0.0),
        "total": float(total or 0.0),
    }


def _pool(rows: Iterable) -> list[dict]:
    """Pool a grain's rows into one range-level readout.

    `n` and `total` are exact — they are sums. The two percentiles are an
    n-weighted MEAN of whatever percentiles the rows carry, which composes
    exactly when the rows are themselves percentiles of a range (the live
    pass) and only approximately when they are percentiles of a bucket (the
    rollup pass). That is the same approximation `api_common._accumulate_buckets`
    makes for a scheduled-rate fold, and it is honest here: the buckets are
    uniform in width and a browser metric is stationary enough over a range
    that a blend within a few percent of the truth is the whole value. The
    alternative — keeping every raw beacon forever — buys a precision no panel
    reads.
    """
    pooled: dict[tuple, list[Any]] = {}
    for metric, part, region, phase, n, p50, p75, total in rows:
        n = int(n or 0)
        acc = pooled.setdefault(
            (metric, part, region, phase), [0, 0.0, 0.0, 0.0])
        acc[0] += n
        acc[1] += float(p50 or 0.0) * n
        acc[2] += float(p75 or 0.0) * n
        acc[3] += float(total or 0.0)
    return [
        {"metric": metric, "part": part, "region": region, "phase": phase,
         "n": n, "p50": p50 / n if n else 0.0,
         "p75": p75 / n if n else 0.0, "total": total}
        for (metric, part, region, phase), (n, p50, p75, total)
        in sorted(pooled.items())
    ]


def _assemble(rng: str, bucket_s: int, bucket_rows: list, series_rows: list,
              exact: bool) -> dict:
    """The response body: per-bucket rows and the range-level readout.

    `exact` says whether `series` holds true percentiles of the whole range
    (the live pass) or an n-weighted blend of per-bucket ones (the rollup
    pass). It is in the payload rather than only in this module's docstrings
    because a consumer that cannot tell them apart would be drawing an
    approximation as if it were a measurement.
    """
    return {
        "range": rng, "bucket_s": bucket_s, "exact": exact,
        # `series_rows` carries the same columns as `bucket_rows` with a
        # NULL bucket on the live pass, so both go through one shape.
        "series": _pool(row[1:] for row in series_rows),
        "buckets": [
            _row(b, m, p, rg, ph, n, p50, p75, t)
            for (b, m, p, rg, ph, n, p50, p75, t) in bucket_rows
        ],
    }


def _series_from_rollup(rng: str, bucket_s: int, since: datetime) -> dict:
    """Read the stored per-width percentiles and pool them (see `_pool`).

    Percentiles cannot be summed across buckets, so unlike the other rollups
    this one is precomputed PER display-bucket width — the widths are
    epoch-aligned and there are only a handful (`constants.LATENCY_BUCKETS`).
    A range filter then just selects buckets.
    """
    with db.viz_conn() as conn:
        rows = conn.execute(
            db.sql_text(f"""
            SELECT bucket, metric, part, region, phase, n, p50, p75, total
            FROM web_metrics_rollup
            WHERE bucket_s = %s
              AND bucket >= to_timestamp(
                    floor(EXTRACT(EPOCH FROM %s::timestamptz) / {bucket_s})
                    * {bucket_s} + {bucket_s} / 2.0)
            ORDER BY bucket, metric, part, region, phase
            """), (bucket_s, since)).fetchall()
    return _assemble(rng, bucket_s, rows, rows, exact=False)


def _series_live(rng: str, bucket_s: int, since: datetime) -> dict:
    """Exact percentiles straight off the retained beacons.

    Only reachable for a width the rollup does not store — the 24h view's
    5-minute buckets, which the API folds finer still. Reads the raw table,
    so the population is bounded by the retention window rather than by the
    range, which is exactly why this is the fallback and not the default.
    """
    with db.viz_conn() as conn:
        buckets = conn.execute(
            db.sql_text(f"""
            WITH src AS (
              SELECT to_timestamp(
                       floor(EXTRACT(EPOCH FROM ts) / {bucket_s})
                       * {bucket_s} + {bucket_s} / 2.0) AS bucket,
                     metric, part, region, phase, value
                FROM web_metrics
               WHERE ts >= %s
            )
            SELECT bucket, metric, part, region, phase, COUNT(*) AS n,
                   PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY value) AS p50,
                   PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY value) AS p75,
                   SUM(value) AS total
              FROM src GROUP BY 1, 2, 3, 4, 5
             ORDER BY bucket, metric, part, region, phase
            """), (since,)).fetchall()
        series = conn.execute(
            db.sql_text("""
            SELECT NULL::timestamptz, metric, part, region, phase,
                   COUNT(*) AS n,
                   PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY value) AS p50,
                   PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY value) AS p75,
                   SUM(value) AS total
              FROM web_metrics WHERE ts >= %s
             GROUP BY 2, 3, 4, 5
             ORDER BY metric, part, region, phase
            """), (since,)).fetchall()
    return _assemble(rng, bucket_s, buckets, series, exact=True)
