"""Frontend performance telemetry: the closed vocabulary and the sink write
(issue #436).

`TIMING` lines (CLAUDIT_TIMING) cover ingest phases and `latency_rollup`
covers model reply latency, so the served page itself was unmeasured. This
module owns the browser side: what a beacon may say, and how it is stored.

Three deliberate choices, each load-bearing downstream:

* **Closed vocabularies, enforced here, not by a CHECK constraint.** The sink
  is the only writer and refuses an unknown term with a 400. A rollup row is
  keyed on `(metric, part, region, phase)`, so an unchecked TEXT column would
  let one buggy caller mint a new grain — and a new stored row — per request,
  and `web_metrics_rollup` would grow without bound. A constraint would
  duplicate a set that has to change with the frontend's instrumentation
  anyway; this module is imported by both the sink and the reader, so the two
  cannot drift.

* **`(metric, part)` is checked as a PAIR, not two independent sets.** Every
  journey reports the same three parts (the fetch wait, the client work, the
  interaction-to-rendered total), and the other two metrics have exactly one
  part each. Pairing is what makes "client work minus the fetch wait" a
  question the rollup can answer instead of a guess.

* **The value is CLAMPED, never rejected, but only once it is a number.** A
  beacon is telemetry: a corrupt reading should cost that one row, never the
  rest of the batch and never a console error on a page the user is reading.
  A *vocabulary* violation is the opposite — it is a bug in our own frontend,
  and dropping it silently would hide it, so it is a 400.

The raw table is append-only and pruned to `RETENTION_S` by the rollup pass
(`backend.ingest_rollup_web_metrics.py`) once the row is in every stored
bucket; the rollup is what outlives the prune.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from backend import db
from backend.constants import LATENCY_BUCKETS

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

#: Named page regions a shift can be attributed to. The frontend walks up from
#: the shift's own node to the nearest element carrying `data-perf-region`,
#: so an unattributed shift lands in `other` rather than being dropped.
REGIONS = ("panel_grid", "inspector", "signin", "other")

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

#: How long a raw beacon is kept, and what that buys.
#:
#: `RETENTION_S` is the HORIZON: how far back the rollup reaches, twice the
#: widest stored bucket so the widest display range has a full bucket's worth
#: of slack either side of its leading edge.
#:
#: `RAW_KEEP_S` is longer, and the difference is the whole correctness of the
#: fold: it makes a PARTIAL read of a closed bucket impossible, at any fold,
#: so a stored bucket is whole by construction rather than by luck.
#:
#: A bucket `[s, s+W)` is closed once `s + W <= horizon`, and `horizon` is
#: `now - RETENTION_S`. On the `W` lattice the smallest such `s` therefore
#: satisfies
#:
#:     s > (now - RETENTION_S - W) - W  =  now - RETENTION_S - 2W
#:
#: So EVERY closed bucket starts after `now - RETENTION_S - 2W`, and the read
#: window has to reach back to there. One width of slack is not enough, which
#: is the trap this constant exists to close: with `R + W` the newest closing
#: bucket's oldest beacons sit just outside the window, the fold stores it
#: short, and nothing later can repair it -- the window advances with `now`,
#: so a bucket the window has cut off stays cut off for good. The earlier
#: value had exactly that shape and stranded a 24-hour bucket with 22 of its
#: 24 beacons after three skipped hourly folds, permanently and silently.
#:
#: Two widths, plus a fold interval of slack for the boundary itself.
RETENTION_S = 2 * max(LATENCY_BUCKETS)

#: The interval at which the fold runs, and the slack kept past the width that
#: makes the invariant above hold. Not load-bearing for correctness -- the
#: two widths are -- but it keeps the oldest closed bucket comfortably inside
#: the window rather than exactly on its edge.
FOLD_INTERVAL_S = 6 * 3600

#: What the raw table actually keeps, and where `prune` deletes below.
RAW_KEEP_S = RETENTION_S + 2 * max(LATENCY_BUCKETS) + FOLD_INTERVAL_S

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


def over_cap(conn, user_id: int, now: datetime) -> bool:
    """True when this user already holds `MAX_ROWS_PER_USER` recent rows."""
    since = now - timedelta(seconds=RETENTION_S)
    row = conn.execute(
        db.sql_text("""
        SELECT 1 FROM web_metrics
         WHERE user_id = %s AND ts >= %s
         LIMIT 1 OFFSET %s
        """),
        (user_id, since, MAX_ROWS_PER_USER - 1),
    ).fetchone()
    return row is not None


def store(conn, user_id: int,
          beacons: list[tuple[str, str, str, str, float]]) -> int:
    """Insert the batch in one statement. Returns rows written."""
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


def rollup_horizon(now: datetime) -> datetime:
    """The instant a rollup bucket must END behind to be complete.

    A bucket is stored once, whole, and never revised: it is written by the
    fold that closes it and the raw rows it needs are gone by the next one.
    """
    return now - timedelta(seconds=RETENTION_S)


def utcnow() -> datetime:
    """The sink's clock, named so a test can pin it."""
    return datetime.now(timezone.utc)
