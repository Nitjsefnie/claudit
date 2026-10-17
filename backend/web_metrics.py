"""Browser telemetry vocabulary, validation and sink write.

The sink is the only writer and refuses unknown terms: unchecked TEXT would
allow unbounded rollup grains. A SQL CHECK would duplicate the frontend vocabulary;
share it with readers and validate metric and part as a pair. Finite out-of-range
readings that are float-representable are clamped; non-finite numbers reject the batch. Vocabulary violations
are frontend bugs and return 400.

Raw rows form a RAW_KEEP_S working set. Pruning follows fold attempts and may
delete a refused closed bucket's raw rows without storing that bucket.
Rollups are durable and unpruned; wider-range reads must union the uncovered raw tail back in.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from backend import db
from backend.constants import LATENCY_BUCKETS

#: The user_id every anonymous session shares. Mirrors
#: `backend.session.GUEST_USER_ID`, named here rather than imported so the
#: vocabulary module keeps no dependency on the session layer.
GUEST_USER_ID = 0

#: The three journeys the frontend times, and the two measurement classes it
#: observes on every page. `dashboard_open` / `inspector_open` / `signin` are
#: interaction-to-rendered; `layout_shift` and `longtask` are the continuous
#: PerformanceObserver sessions that ride along with them.
JOURNEYS = ("dashboard_open", "inspector_open", "signin")
OBSERVED = ("layout_shift", "longtask")
METRICS = JOURNEYS + OBSERVED

#: The three parts of every journey. `fetch` is the wait on the network,
#: `client` is what the browser did with the answer (Babel evaluating the
#: bundle, React rendering the panels), and `total` is the sum the user felt.
#: Reporting the first two beside the third is what "disambiguate client work
#: from the fetch wait" means: the panel can show a total that regressed while
#: the client share of it did not, or the reverse.
TIMING_PARTS = ("fetch", "client", "total")

#: (metric, part) pairs the sink accepts. Every journey carries the three
#: timing parts; `layout_shift` carries one dimensionless score and
#: `longtask` one blocking duration.
METRIC_PARTS = {
    **{journey: TIMING_PARTS for journey in JOURNEYS},
    "layout_shift": ("shift",),
    "longtask": ("block",),
}

#: The eight fixed regions: the grid, the Inspector, the sign-in page, the
#: top bar, the project picker strip, the range preset strip, the summary
#: strip, and the catch-all. The client declares exactly this set as its
#: own REGIONS table; the panel terms below are declared client-side as the
#: PANELS title list instead.
FIXED_REGIONS = ("panel_grid", "inspector", "signin", "topbar",
                 "project_picker", "range_picker", "summary_strip", "other")

#: Attribute shifts to the nearest data-perf-region, deferring panel_grid
#: because it wraps every panel. The nearest data-panel names a shift with
#: panel_ plus its snake_case title; use other only outside all regions.
#: Panel terms are a closed vocabulary: free-form selectors would grow the
#: rollup grain unboundedly. The derivation test checks JSX panel titles
#: against these terms, so adding or renaming a panel requires its term.
REGIONS = FIXED_REGIONS + (
    "panel_input_tokens",
    "panel_output_tokens",
    "panel_thinking_output",
    "panel_cache_create",
    "panel_cache_read",
    "panel_total_tokens",
    "panel_cost_usd",
    "panel_lines_added",
    "panel_lines_deleted",
    "panel_cost_by_model",
    "panel_tokens_by_model",
    "panel_token_breakdown_by_tokens",
    "panel_token_breakdown_by_cost",
    "panel_cost_by_agent_type",
    "panel_tokens_by_agent_type",
    "panel_cost_by_project",
    "panel_tokens_by_project",
    "panel_tool_error_rate",
    "panel_page_performance",
    "panel_context_chart",
    "panel_session_burn_rate",
    "panel_response_sizes",
    "panel_tool_usage_ratio",
    "panel_reply_latency",
    "panel_activity_heatmap",
    "panel_activity_heatmap_legend",
    "panel_tokens_by_context_size",
    "panel_cost_by_context_size",
    "panel_prompt_cache_ttl_split",
    "panel_context_growth_comparison",
)

#: When in the page's life a measurement was taken. One vocabulary for both
#: observed classes: a shift during the first render and a long task blocking
#: it are both `pre_paint`; either after the page became usable is
#: `post_usable`; either driven by an SSE-driven panel update is
#: `sse_update`.
PHASES = ("pre_paint", "post_usable", "sse_update")

#: One request's worth of beacons. The frontend sends one to three; the cap
#: bounds a single call's insert without a rate limiter in front of a route
#: whose caller is our own page.
MAX_BEACONS = 50

#: Ceiling on rows one user may hold inside the retention window. The sink is
#: a same-origin authenticated POST, so a script on the page could loop it;
#: without this the table grows until the hourly prune, and a guest session
#: costs the operator the same as a real one. Counted, not estimated, so the
#: cap is what it says it is.
MAX_ROWS_PER_USER = 20_000

#: The same ceiling for an ANONYMOUS session, and a good deal lower.
#: Asymmetry here is about trust, not worth: a guest is anonymous by
#: construction, so nothing about its beacons is attributable and no
#: reputation stands behind them, while a named user's row count is bounded
#: by the fact that they will notice a runaway. Guests also share `user_id =
#: 0` with every other guest, so one caller can otherwise spend the whole
#: anonymous budget and crowd out everyone else's.
MAX_ROWS_PER_GUEST = 2_000

#: rollup_horizon is now: the sink stamps timestamps server-side, so a
#: closed bucket cannot gain beacons. Closing a retention window early
#: would omit newest beacons and make wide ranges report a subset.
#:
#: RAW_KEEP_S covers two widest bucket widths plus a fold interval. That
#: keeps newly closed buckets fully readable even across prune/fold
#: boundaries; storing a partial bucket would freeze incomplete percentiles.
#: A stalled fold can outgrow that window: fold_read_from reaches back to
#: surviving beacons and the span check refuses buckets still incomplete.
#:
#: Rollups are durable and unpruned. Raw rows are a bounded working set;
#: readers must union the uncovered raw tail into wider-range results.
FOLD_INTERVAL_S = 6 * 3600
RAW_KEEP_S = 2 * max(LATENCY_BUCKETS) + FOLD_INTERVAL_S

#: Value ceiling per part. A timing part is milliseconds and a beacon is
#: bounded by human patience; an hour is far past anything a page legitimately
#: takes and well inside the DOUBLE PRECISION range, so a corrupt or hostile
#: value cannot poison a rollup's `total`. `shift` is a dimensionless Layout
#: Instability score: web.dev's own "poor" threshold is 0.1 and the API caps a
#: single entry at 10, so 1000 is three orders of magnitude of headroom.
#: Clamped, not rejected — see the module docstring.
PART_LIMITS = {
    "fetch": (0.0, 3_600_000.0),
    "client": (0.0, 3_600_000.0),
    "total": (0.0, 3_600_000.0),
    "block": (0.0, 3_600_000.0),
    "shift": (0.0, 1000.0),
}


class BeaconError(ValueError):
    """A beacon the sink refuses. Names the offending field."""


def _term(raw: dict, field: str) -> str:
    value = raw.get(field, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise BeaconError(f"{field} must be a string")
    return value


def _check_tags(metric: str, region: str, phase: str) -> None:
    """Refuse a region/phase that does not belong to its metric.

    A journey names itself and carries neither; a layout shift is attributed
    to a region and to a phase; a long task is a phase with no region. The
    rule is per metric rather than per term because the interesting failures
    are the mismatched ones — a shift with no region, or a journey carrying
    both — and a flat "is this term known" check would pass every one of them.
    """
    if metric in JOURNEYS:
        if region or phase:
            raise BeaconError(f"metric {metric!r} carries no region or phase")
        return
    if metric == "layout_shift":
        if not region:
            raise BeaconError("layout_shift requires a region")
        if not phase:
            raise BeaconError("layout_shift requires a phase")
        return
    if region:
        raise BeaconError("longtask carries no region")
    if not phase:
        raise BeaconError("longtask requires a phase")


def normalise(raw: object) -> tuple[str, str, str, str, float]:
    """`(metric, part, region, phase, value)` for one beacon, or refuse.

    Vocabulary violations raise; an out-of-range value is clamped. `region`
    and `phase` default to '' — a journey carries neither, and an observed
    row must carry the one its metric requires.
    """
    if not isinstance(raw, dict):
        raise BeaconError("each beacon must be an object")

    metric = _term(raw, "metric")
    if metric not in METRICS:
        raise BeaconError(f"unknown metric: {metric!r}")
    part = _term(raw, "part")
    if part not in METRIC_PARTS[metric]:
        raise BeaconError(f"unknown part {part!r} for metric {metric!r}")

    region, phase = _term(raw, "region"), _term(raw, "phase")
    if region and region not in REGIONS:
        raise BeaconError(f"unknown region: {region!r}")
    if phase and phase not in PHASES:
        raise BeaconError(f"unknown phase: {phase!r}")
    _check_tags(metric, region, phase)

    value = raw.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BeaconError("value must be a number")
    if not math.isfinite(value):
        # Starlette's json.loads accepts NaN and Infinity; a beacon carrying
        # either would reach DOUBLE PRECISION and poison a bucket's total.
        raise BeaconError("value must be finite")
    low, high = PART_LIMITS[part]
    return metric, part, region, phase, min(max(float(value), low), high)


def parse_batch(payload: object) -> list[tuple[str, str, str, str, float]]:
    """Every beacon in one `{"beacons": [...]}` request body, normalised.

    The envelope is required rather than inferred: the frontend is the only
    caller and always sends it, and one unambiguous shape is one less thing for
    the sink to guess about. The whole batch is refused on the first bad
    beacon — a mixed verdict would cost the good rows too, and a 400 naming
    the field is what a developer needs to see.
    """
    if not isinstance(payload, dict) or "beacons" not in payload:
        raise BeaconError("body must be an object carrying 'beacons'")
    beacons = payload["beacons"]
    if not isinstance(beacons, list):
        raise BeaconError("beacons must be a list")
    if not beacons:
        raise BeaconError("beacons must not be empty")
    if len(beacons) > MAX_BEACONS:
        raise BeaconError(f"at most {MAX_BEACONS} beacons per request")
    return [normalise(beacon) for beacon in beacons]


def cap_for(user_id: int) -> int:
    return MAX_ROWS_PER_GUEST if user_id == GUEST_USER_ID else MAX_ROWS_PER_USER


def over_cap(conn, user_id: int, now: datetime) -> bool:
    """True when this session already holds its ceiling in rows.

    Counted over the whole raw window, which is what the ceiling is about: the
    table is a working set, so a count against anything shorter would let a
    looping client refill a pruned window forever.
    """
    since = now - timedelta(seconds=RAW_KEEP_S)
    row = conn.execute(
        db.sql_text("""
        SELECT 1 FROM web_metrics
         WHERE user_id = %s AND ts >= %s
         LIMIT 1 OFFSET %s
        """),
        (user_id, since, cap_for(user_id) - 1),
    ).fetchone()
    return row is not None


def guest_share(conn, since: datetime) -> tuple[int, int]:
    """(guest rows, all rows) over the window the answer rests on.

    Guests are NOT excluded from the readout — this host is guest-heavy, and
    a panel blind to its own traffic is the worse failure. But an anonymous
    session is a single shared `user_id`, so a panel whose numbers are mostly
    one anonymous caller has to LOOK skewed rather than read as a population.
    This is the number that lets it.
    """
    row = conn.execute(
        db.sql_text("""
        SELECT COUNT(*) FILTER (WHERE user_id = %s), COUNT(*)
          FROM web_metrics WHERE ts >= %s
        """), (GUEST_USER_ID, since)).fetchone()
    return int(row[0] or 0), int(row[1] or 0)


def store(conn, user_id: int,
          beacons: list[tuple[str, str, str, str, float]]) -> int:
    cur = conn.cursor()
    cur.executemany(
        """
        INSERT INTO web_metrics (user_id, metric, part, region, phase, value)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        [(user_id, *beacon) for beacon in beacons],
    )
    return len(beacons)


def retention_cutoff(now: datetime) -> datetime:
    """The instant `prune` deletes below: the oldest row still on the table.

    Also the oldest instant the live pass can read, so a range wider than
    this is clamped to it and reports the window it really used.
    """
    return now - timedelta(seconds=RAW_KEEP_S)


def fold_read_from(conn, now: datetime) -> datetime:
    """Oldest instant the fold may read: the retention cutoff or oldest beacon.

    After a stall, the nominal cutoff can fall inside a closed bucket and
    freeze tail-only percentiles with ON CONFLICT DO NOTHING. Reach back
    to the oldest surviving beacon; refuse any bucket still unreadable
    in full. Missing beacons cannot be recovered by widening the window.

    Prune at the retention cutoff, never this widened read bound, so the
    next pass returns to its nominal working set. A longer stall requires
    a wider read; no fixed performance ceiling is implied."""
    cutoff = retention_cutoff(now)
    row = conn.execute("SELECT MIN(ts) FROM web_metrics").fetchone()
    oldest = row[0] if row else None
    if oldest is None or oldest > cutoff:
        return cutoff
    return oldest


def rollup_horizon(now: datetime) -> datetime:
    """The instant a rollup bucket must END behind to be complete.

    `now`, and the reason is the sink rather than a policy choice: it stamps
    every beacon's `ts` server-side, so a bucket whose span has passed can
    never still gain a member. A bucket is stored once, whole, and never
    revised -- the fold that closes it writes it, the next leaves it alone.
    """
    return now


def utcnow() -> datetime:
    """The sink's clock, named so a test can pin it."""
    return datetime.now(timezone.utc)
