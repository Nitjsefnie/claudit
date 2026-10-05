"""The `/api/metrics` beacon sink and the `/api/web-metrics` readout.

Two routes, deliberately thin. The sink's decisions all live in
`backend.web_metrics` (the closed vocabulary, the clamps, the caps); this
module is the HTTP shape around them.

The readout forks on ONE question — does the requested range FIT the raw
working set? — and the answer decides everything. If it does, the range is
read from `web_metrics` outright and is exact. If it does not, it is read
from stored buckets PLUS the raw tail those buckets do not yet cover, and the
result is a blend; `exact: false` in the payload says so, and the panel says
it on its face. What the blend costs is written out at `_pool`.

Auth is `session.auth_middleware` by path prefix, not a decorator (see
`backend/api.py`), which is what makes the sink's two gates free:

* **Same-origin.** `check_origin` runs on every non-GET/HEAD/OPTIONS request,
  so a beacon POST is refused cross-origin exactly like `/login` is.
* **A session.** Also the middleware, and deliberately NOT relaxed here: an
  open unauthenticated write endpoint on a dashboard is not a trade worth
  making for a few rows of telemetry. A guest session qualifies, and
  `_guest_denied` leaves both routes alone, because a beacon names a journey
  and not a project, a session or a file — there is nothing in the payload
  for a guest to reach.

The READOUT is gated anyway, and by a different question (#629): it is the
site's own engineering telemetry — journey timings, layout shift, long
tasks, beacon counts — not product data an ordinary user should read, so
`web_metrics_route` serves it to operators only. The sink is untouched:
beacons keep arriving from every session, guests included, and keep being
stored, which is what makes the operator's readout worth having.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
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
def web_metrics_route(
    request: Request, rng: str = Query("7d", alias="range"),
) -> dict:
    """The operator gate, then the readout (#629).

    The split is the same one `/api/dashboard` and `/api/cache` already
    draw, and for the same reason: this handler is per-request and
    uncached, and it hands off to the `@cache_response` function below,
    whose cache key is its query parameters alone. Putting `request` here
    rather than on the cached function is what keeps a `Request` object
    out of that key — a key carrying one is different on every request,
    so the readout would never hit the cache and every key would be its own
    entry.

    The gate reads only `request.state.is_operator`, which the auth
    middleware resolved from the auth DB's per-user `web_operator`
    (see `session.is_operator`). No query parameter and no client-side flag
    reaches it: the browser asking for the data changes nothing, which is
    the whole difference between this and hiding a panel.
    """
    if not bool(getattr(request.state, "is_operator", False)):
        raise HTTPException(403, "operator only")
    return web_metrics_readout(rng=rng)


@cache_response
def web_metrics_readout(
    rng: str = Query("7d", alias="range"),
) -> dict:
    """Journey timings and observed blocking, as percentiles.

    `range` picks the display bucket exactly as every other panel does, and
    the pass is chosen by what that bucket width affords. The rule is the one
    the performance field guide states for precomputed percentiles:
    computing the percentile once per stored bucket width is exact, but
    COMBINING several buckets' percentiles into one range figure is not. So a
    stored width is served from the rollup and flagged `exact: false`, and
    every other width is served from the raw beacons and flagged exact.

    In practice that makes the live pass the default, and not by accident:
    every range the retention window can cover folds to a width finer than an
    hour, which the rollup does not store, because `_bucket_seconds` only
    reaches 3600 at a span of 100 hours or more. The raw table is small — a
    few rows per page view — for exactly as long as it is kept.

    The live pass clamps its window to the retention cutoff and reports the
    window it really read in `since`, so a hand-typed range the rollup cannot
    serve and the prune has already eaten cannot answer with a shorter
    history under a longer label. The panel reads `since` and shows it.
    """
    delta = _parse_range(rng)
    # web_metrics' own clock, not datetime.now(): the rollup pass and this
    # endpoint have to agree on "now" for a range to select the buckets the
    # fold stored, and one named function is what lets a test pin both.
    now = web_metrics.utcnow()
    since, bucket_s = now - delta, _bucket_seconds(delta)
    # The fork is by whether the range FITS the raw window, not by bucket
    # width. Width was the older test and it chose wrong: a four-day range
    # folds to a 15-minute bucket, is not a stored width, and was therefore
    # served from 54 hours of raw rows for a 96-hour question — clamped, with
    # the answer describing a third of the range it was asked about. A range
    # the raw table cannot cover is served from the rollup plus its tail,
    # whatever width it happens to fold to.
    if delta.total_seconds() <= web_metrics.RAW_KEEP_S:
        return _series_live(rng, bucket_s, since, now)
    return _series_from_rollup(rng, _stored_width(bucket_s), since)


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
              exact: bool, since: datetime, conn=None) -> dict:
    """The response body: per-bucket rows and the range-level readout.

    `exact` says whether `series` holds true percentiles of the whole range
    (the live pass) or an n-weighted blend of per-bucket ones (the rollup
    pass). It is in the payload rather than only in this module's docstrings
    because a consumer that cannot tell them apart would be drawing an
    approximation as if it were a measurement. `since` is the window the
    answer was actually read over, which is not always the window asked for.
    """
    guests, total_rows = (0, 0)
    if conn is not None:
        guests, total_rows = web_metrics.guest_share(conn, since)
    return {
        "range": rng, "bucket_s": bucket_s, "exact": exact,
        "since": _iso(since),
        # Disclosure, not exclusion: an anonymous session shares one
        # user_id, so a panel whose numbers are mostly one anonymous caller
        # has to look skewed rather than read as a population.
        "guests": guests, "beacons": total_rows,
        # `series_rows` carries the same columns as `bucket_rows` with a
        # NULL bucket on the live pass, so both go through one shape.
        "series": _pool(row[1:] for row in series_rows),
        "buckets": [
            _row(b, m, p, rg, ph, n, p50, p75, t)
            for (b, m, p, rg, ph, n, p50, p75, t) in bucket_rows
        ],
    }


def _stored_width(requested: int) -> int:
    """The narrowest STORED width that can carry a range, never a bare one.

    A width's rollup accumulates only what its own folds could read, and a
    fold reads `RAW_KEEP_S` behind `now`. So a width the rollup does not
    STORE has no history at all, and a stored width finer than the range
    needs has barely started — a 15-minute bucket's rollup is a couple of
    hours deep. Serving a four-day range from that answered a 96-hour
    question with 54 hours of data, which is a clamp wearing a bucket's
    clothes.

    So: the narrowest stored width at least as wide as asked for, and the
    widest stored width when none is. Coarser buckets mean a coarser series,
    which the caller can see in `bucket_s`; silently answering from a width
    it does not hold is not an option.
    """
    for width in LATENCY_BUCKETS:
        if width >= requested:
            return width
    return LATENCY_BUCKETS[-1]


def _series_from_rollup(rng: str, bucket_s: int, since: datetime) -> dict:
    """Stored buckets UNION the raw tail, then pooled (see `_pool`).

    The rollup holds only CLOSED buckets, and a bucket closes when its span
    has passed — so the newest beacons, and everything since the last fold,
    are in `web_metrics` and in no bucket yet. Reading the rollup ALONE
    drops them, and the drop is not subtle: a wider range selects more stored
    buckets and still reports FEWER beacons than a narrower one, and the
    default `all` view served 42 of 240 on a ten-day corpus. That is what a
    cross-model review of this branch found, and it contradicted a claim in
    this module's own header.

    So the tail is unioned back in, from the end of the newest stored bucket
    to now. The union is a blend — a stored bucket's percentile beside a live
    percentile over a different population — which is what `exact: false` has
    always meant, and the reason the flag exists.
    """
    now = web_metrics.utcnow()
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
        newest_row = conn.execute(
            "SELECT MAX(bucket) FROM web_metrics_rollup WHERE bucket_s = %s",
            (bucket_s,)).fetchone()
        newest = newest_row[0] if newest_row else None
        # The tail begins where the stored history stops — and never before
        # the requested range, or a narrow range would pick up older buckets
        # it did not ask for.
        # The newest stored bucket's END, not its start: its span is already
        # counted in the row, and starting at the start double-counts it.
        tail_from = (newest + timedelta(seconds=bucket_s / 2)
                     if newest is not None else since)
        tail_from = max(tail_from, since,
                        web_metrics.retention_cutoff(now))
        tail = conn.execute(
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
            """), (tail_from,)).fetchall()
    # `since` is the OLDEST data this answer actually rests on, not the
    # window that was asked for: a range the rollup cannot reach is served
    # from whatever history exists, and the panel shows the difference. The
    # previous version echoed the requested window, which is the exact lie
    # `since` was added to prevent.
    served = [r[0] for r in rows] or [tail_from]
    oldest = min(served) - timedelta(seconds=bucket_s / 2)
    return _assemble(rng, bucket_s, list(rows) + list(tail),
                     list(rows) + list(tail), False, oldest, conn)


def _series_live(rng: str, bucket_s: int, since: datetime,
                 now: datetime) -> dict:
    """Exact percentiles straight off the beacons, over the window that has them.

    The window is clamped to the retention cutoff, because the raw table is
    pruned to it and a wider `since` would silently answer with a shorter
    history than the caller asked for. The clamped instant goes back in the
    payload so the panel can say what it is showing.

    This is the pass for a range the working set covers, and the ONLY one of
    the two that is exact. The raw table is small — a few rows per page view
    — for as long as it is kept, which is what makes that affordable.
    """
    window = max(since, web_metrics.retention_cutoff(now))
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
            """), (window,)).fetchall()
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
            """), (window,)).fetchall()
    return _assemble(rng, bucket_s, buckets, series, True, window, conn)
