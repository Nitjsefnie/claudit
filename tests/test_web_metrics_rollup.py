"""The web-metrics rollup and the readout it serves (issue #436).

Split from `test_web_metrics.py` by subject, not by size: that module is
what a BEACON MAY SAY, this one is what the fold DOES WITH what was said.
The seam is real -- a vocabulary change and a fold change are different
bugs, and a suite that mixes them makes a failure ambiguous.

The shared fixtures and helpers come from the vocabulary module rather than
being restated, for the reason `tests/conftest.py` already puts in
`sys.path`: a fresh schema and a mini R2 mirror are expensive, and two
copies of them are two things to keep in step.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from test_web_metrics import (  # noqa: F401  (fixtures re-exposed below)
    _ORIGIN, PINNED_NOW, _VALUES, _beacon, _client_fixture, _closed_ts,
    _pinned_fixture, _post, _recent_ts, _rows, _seed, _viz_fixture,
)

from backend import db, web_metrics
from backend.api_common import _bucket_seconds
from backend.constants import LATENCY_BUCKETS
from backend.ingest_rollup_web_metrics import rebuild_web_metrics_rollup

# The three shared fixtures, re-declared as thin delegates. A pytest fixture
# is not an importable name -- `name=` binds the fixture, not the symbol --
# so what crosses the seam is the generator, and each is re-wrapped here
# rather than restated. The alternative, dropping the decorator upstream, makes
# the other module's fixtures invisible to a reader looking for them.


@pytest.fixture(name="viz")
def _viz(monkeypatch):
    yield from _viz_fixture(monkeypatch)


@pytest.fixture(name="pinned")
def _pinned(monkeypatch):
    yield from _pinned_fixture(monkeypatch)


@pytest.fixture(name="client")
def _client(viz):
    yield from _client_fixture(viz)


# --- the rollup ------------------------------------------------------------


def test_rollup_folds_exact_percentiles_and_the_sum(viz, pinned):
    _seed(_VALUES)
    assert rebuild_web_metrics_rollup() > 0
    with db.viz_conn() as conn:
        rows = conn.execute(
            "SELECT bucket_s, n, p50, p75, total FROM web_metrics_rollup "
            "WHERE bucket_s = 3600").fetchall()
    assert rows, "the hourly width must have a row"
    # Every width holds the same population, so every row is the same fold.
    assert {int(n) for _w, n, _a, _b, _t in rows} == {len(_VALUES)}
    assert {round(p50, 6) for _w, _n, p50, _b, _t in rows} == {4.5}
    assert {round(p75, 6) for _w, _n, _a, p75, _t in rows} == {6.25}
    assert {round(total, 6) for _w, _n, _a, _b, total in rows} == {36.0}


# --- closed-bucket semantics (the reviewer's B1) ---------------------------
#
# A bucket is stored once, whole, by the fold that CLOSES it, and never
# revised. Before this was the rule, a fold rewrote every bucket the window
# touched with a strictly narrower slice, so each stored percentile ended up
# describing the bucket's LAST HOUR -- at every width. The suite below pins
# the rule; the first one is the one that was broken.


@pytest.fixture(name="short_horizon")
def _short_horizon_fixture(monkeypatch):
    """Collapse the retention window so a test can reach a closed bucket.

    The real horizon is two days, which needs two days of hourly folds to
    close a bucket. The RULE is what is under test, not the yardstick, so
    both ends move together: the horizon two hours back and the raw table
    five, which still leaves the full-bucket slack RAW_KEEP_S exists for.
    """
    monkeypatch.setattr(web_metrics, "rollup_horizon",
                        lambda now: now - timedelta(hours=2))
    # The read window stays WIDE on purpose: it is a separate knob from the
    # horizon, and narrowing it is what would prune the very beacons a test
    # seeded. Only the horizon is collapsed, because that is what decides
    # which buckets close and therefore how many folds a test needs.
    monkeypatch.setattr(web_metrics, "retention_cutoff",
                        lambda now: now - timedelta(days=30))


def _fold_at(moment):
    web_metrics.utcnow = lambda: moment
    return rebuild_web_metrics_rollup()


def _rollup_rows(viz, width):
    with db.viz_conn() as conn:
        return {
            row[0]: (int(row[1]), float(row[2]), float(row[3]), float(row[4]))
            for row in conn.execute(
                "SELECT bucket, n, p50, p75, total FROM web_metrics_rollup "
                "WHERE bucket_s = %s AND metric = 'dashboard_open'",
                (width,)).fetchall()}


def test_a_stored_bucket_is_never_revised(viz, short_horizon):
    """A closed bucket is written once. Later beacons must not move it.

    This is the exact shape of the defect: the old fold rewrote the bucket on
    every pass with a narrower slice, so its median walked forward to whatever
    the last slice happened to contain.
    """
    base = datetime(2026, 3, 2, 0, 0, 0, tzinfo=timezone.utc)
    _seed([0.0], ts=base)                      # 00:00-00:59
    _fold_at(base + timedelta(hours=3, minutes=30))   # closes the 00:00 hour
    closed = _rollup_rows(viz, 3600)
    assert len(closed) == 1
    bucket, before = next(iter(closed.items()))
    assert before == (1, 0.0, 0.0, 0.0), before

    # More beacons land in the same already-closed hour -- a late beacon, a
    # backfill, a clock that moved. The stored row must not see them.
    _seed([23.0, 23.0, 23.0], ts=base + timedelta(minutes=10))
    _fold_at(base + timedelta(hours=5))
    assert _rollup_rows(viz, 3600)[bucket] == before, (
        "a stored bucket was revised by beacons that arrived after it closed")


def test_no_open_bucket_is_ever_stored(viz, short_horizon):
    """Every stored row's whole span is behind the horizon that wrote it.

    An open bucket is still accumulating, so a percentile over it is a number
    that changes under the reader between one fold and the next. Asserted
    across every width, because the old defect hit all four.
    """
    base = datetime(2026, 3, 2, 0, 0, 0, tzinfo=timezone.utc)
    for hour in range(8):
        _seed([float(hour)], ts=base + timedelta(hours=hour))
    now = base + timedelta(hours=9)
    horizon = now - timedelta(hours=2)
    _fold_at(now)
    for width in LATENCY_BUCKETS:
        for bucket, (_n, _p50, _p75, _total) in _rollup_rows(
                viz, width).items():
            span_end = bucket.timestamp() + width / 2
            assert span_end <= horizon.timestamp(), (
                f"width {width} stored the bucket ending "
                f"{datetime.fromtimestamp(span_end, timezone.utc)}, past the "
                f"horizon {horizon} -- it was still open")


def test_a_closing_bucket_holds_every_beacon_in_its_span(viz, short_horizon):
    """The population a bucket stores is its WHOLE span, not its last slice.

    Six hours of beacons into a six-hour bucket, at a width where the old
    fold stored only the tail.
    """
    base = datetime(2026, 3, 2, 0, 0, 0, tzinfo=timezone.utc)
    for hour in range(6):
        _seed([float(hour)], ts=base + timedelta(hours=hour))
    now = base + timedelta(hours=9)
    _fold_at(now)
    six_hour = _rollup_rows(viz, 21600)
    assert six_hour, "the six-hour bucket never closed"
    n, p50, _p75, total = next(iter(six_hour.values()))
    assert n == 6, f"stored {n} of the bucket's 6 beacons"
    assert total == 15.0, total
    assert p50 == 2.5, p50


def test_rollup_is_idempotent(viz, pinned):
    _seed(_VALUES)
    first = rebuild_web_metrics_rollup()
    with db.viz_conn() as conn:
        before = conn.execute(
            "SELECT bucket_s, bucket, metric, part, n, p50, p75, total "
            "FROM web_metrics_rollup ORDER BY 1, 2").fetchall()
    rebuild_web_metrics_rollup()
    with db.viz_conn() as conn:
        after = conn.execute(
            "SELECT bucket_s, bucket, metric, part, n, p50, p75, total "
            "FROM web_metrics_rollup ORDER BY 1, 2").fetchall()
    assert first > 0
    assert before == after, "a second pass must replace, not duplicate"


def test_rollup_keeps_the_grain_apart(viz, pinned):
    """Two grains in one bucket are two rows — the key is the whole grain."""
    _seed(_VALUES)
    with db.viz_conn() as conn:
        conn.execute(
            "INSERT INTO web_metrics (ts, user_id, metric, part, region, "
            "phase, value) VALUES (%s, 7, 'longtask', 'block', '', "
            "'sse_update', 250.0)", (_closed_ts(),))
        conn.commit()
    rebuild_web_metrics_rollup()
    with db.viz_conn() as conn:
        grains = conn.execute(
            "SELECT DISTINCT metric, part, region, phase "
            "FROM web_metrics_rollup WHERE bucket_s = 3600").fetchall()
    assert sorted(grains) == [("dashboard_open", "total", "", ""),
                              ("longtask", "block", "", "sse_update")]


def test_prune_keeps_the_window_and_drops_the_rest(viz, pinned):
    now = PINNED_NOW
    fresh, stale = _closed_ts(), now - timedelta(
        seconds=web_metrics.RAW_KEEP_S + 3600)
    _seed(_VALUES, ts=fresh)
    _seed([99.0], ts=stale)
    rebuild_web_metrics_rollup()
    assert [row[5] for row in _rows(viz)] == _VALUES
    with db.viz_conn() as conn:
        totals = conn.execute(
            "SELECT DISTINCT total FROM web_metrics_rollup "
            "WHERE bucket_s = 3600").fetchall()
    assert [float(t) for (t,) in totals] == [36.0], (
        "a pruned row must not survive in a rollup bucket either")


def test_the_fold_and_the_prune_cover_disjoint_populations(viz, pinned, monkeypatch):
    """A row one second outside the window is in neither side of the pass.

    This is why the fold and the prune need no ordering between them: the
    fold reads exactly `ts >= cutoff` and the prune deletes exactly
    `ts < cutoff`, so nothing deleted was ever a row the fold owed a bucket.
    Pinned at the boundary rather than in the middle of the window, where a
    fold that quietly reached further back would go unnoticed.
    """
    pinned = datetime(2026, 1, 2, 0, 10, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(web_metrics, "utcnow", lambda: pinned)
    cutoff = web_metrics.retention_cutoff(pinned)
    _seed([4242.0], ts=cutoff - timedelta(seconds=1))
    _seed([1.0], ts=cutoff)
    rebuild_web_metrics_rollup()
    assert [row[5] for row in _rows(viz)] == [1.0]
    with db.viz_conn() as conn:
        totals = {float(t) for (t,) in conn.execute(
            "SELECT total FROM web_metrics_rollup "
            "WHERE bucket_s = 3600").fetchall()}
    assert totals == {1.0}, "the row outside the window gets no bucket"


# --- the readout -----------------------------------------------------------


def test_readout_serves_the_rollup_past_the_retention_window(viz, pinned, client):
    _seed(_VALUES)
    rebuild_web_metrics_rollup()
    r = client.get("/api/web-metrics?range=7d", headers=_ORIGIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bucket_s"] == 3600
    assert body["exact"] is False, "a blend of bucket percentiles"
    assert body["series"] == [
        {"metric": "dashboard_open", "part": "total", "region": "",
         "phase": "", "n": 8, "p50": 4.5, "p75": 6.25, "total": 36.0},
    ]
    assert body["buckets"] and body["buckets"][0]["ts"]


def test_a_range_the_raw_beacons_still_cover_is_exact(viz, pinned, client):
    """The live pass is the default, and the arithmetic behind it is pinned.

    Every range the retention window can cover folds to a width finer than an
    hour, which the rollup does not store — `_bucket_seconds` only reaches
    the narrowest stored width (3600) at a span of 100 hours. So "the rollup
    does not store this width" and "the raw rows still cover this range" are
    the same statement today, and that identity is what makes the answer
    exact without anyone having to choose. Pin it: if a stored width is ever
    added below 3600, or the retention window pushed past a day, this fails
    and the fork in `web_metrics_readout` has to be reconsidered.
    """
    for hours in (1, 6, 12, 24, 36, 48):
        span = timedelta(hours=hours)
        assert span.total_seconds() <= web_metrics.RETENTION_S
        assert _bucket_seconds(span) not in LATENCY_BUCKETS, (
            f"{hours}h now folds to a stored width, so a range the raw rows "
            f"cover is answered from the rollup and blended")

    _seed(_VALUES, ts=_recent_ts())
    body = client.get("/api/web-metrics?range=1d", headers=_ORIGIN).json()
    assert body["exact"] is True
    assert body["series"][0]["p50"] == 4.5
    assert body["since"], "the window actually read is reported"


def test_a_range_past_the_raw_window_is_clamped_to_it(viz, pinned, client):
    """A range wider than the raw table is answered over the window it has.

    The raw table keeps `RAW_KEEP_S`, and a range longer than that has rows
    the live pass can never see. Answering with the shorter history under the
    longer label would be a lie the payload cannot carry, so the window comes
    back in `since` and the panel is told to show it.

    The range must be past `RAW_KEEP_S` AND fold to a width the rollup does
    not store, which is a narrow band -- `_bucket_seconds` only reaches the
    narrowest stored width (3600) at a 100-hour span, and `RAW_KEEP_S` is 97.
    The band is the width of `FOLD_INTERVAL_S`, and that it is this narrow is
    itself the assertion worth making: it is what says how much raw data the
    live pass has to work with.
    """
    span_h = web_metrics.RAW_KEEP_S / 3600 + 1
    assert web_metrics.RETENTION_S / 3600 < span_h < 100, span_h
    _seed(_VALUES, ts=_recent_ts())
    body = client.get(f"/api/web-metrics?range={span_h:.0f}h",
                      headers=_ORIGIN).json()
    assert body["bucket_s"] not in (3600, 21600, 43200, 86400)
    since = datetime.fromisoformat(body["since"].replace("Z", "+00:00"))
    assert since == PINNED_NOW - timedelta(seconds=web_metrics.RAW_KEEP_S), (
        f"the live pass read from {since}, not from the oldest row it has")
    assert body["series"][0]["n"] == len(_VALUES)


def test_readout_rolls_the_blend_by_sample_count(viz, pinned, client):
    """Two rollup buckets, different sizes: the p50 is weighted by n.

    An UNWEIGHTED mean of the two buckets' p50s reads 500.0 here, and
    (8 * 100 + 1 * 900) / 9 is 344.4. The populations sit six hours apart so
    they land in different stored buckets of the six-hour width.
    """
    base = _closed_ts()
    _seed([100.0] * 8, ts=base)
    _seed([900.0], ts=base + timedelta(hours=6))
    rebuild_web_metrics_rollup()
    rolled = client.get(
        "/api/web-metrics?range=7d", headers=_ORIGIN).json()["series"]
    assert len(rolled) == 1
    assert rolled[0]["n"] == 9
    assert rolled[0]["total"] == pytest.approx(1700.0)
    assert rolled[0]["p50"] == pytest.approx((800.0 + 900.0) / 9), (
        "the blend is n-weighted, not a mean of means")


def test_the_live_pass_over_the_same_population_is_exact(viz, pinned, client):
    """The same values inside the window the live pass owns.

    Nine samples, and the exact p50 is 100 -- which is what makes the rollup's
    344.4 above visibly an approximation rather than a different answer to
    the same question. That difference is the reason for the `exact` flag.
    """
    _seed([100.0] * 8, ts=_recent_ts())
    _seed([900.0], ts=_recent_ts() + timedelta(minutes=5))
    live = client.get("/api/web-metrics?range=1d",
                      headers=_ORIGIN).json()["series"]
    assert len(live) == 1
    assert live[0]["n"] == 9 and live[0]["total"] == pytest.approx(1700.0)
    assert live[0]["p50"] == pytest.approx(100.0)


def test_readout_is_exact_for_every_range_inside_the_window(viz, pinned, client):
    """The 24h view folds finer than the rollup stores, and is exact.

    Seeded RECENT, because this is the live path: a beacon old enough to be
    folded is outside a 24h window that asks for the last 24 hours.
    """
    _seed(_VALUES, ts=_recent_ts())
    body = client.get("/api/web-metrics?range=24h", headers=_ORIGIN).json()
    assert body["bucket_s"] not in (3600, 21600, 43200, 86400)
    assert body["exact"] is True
    assert body["series"][0]["p50"] == 4.5
    assert body["series"][0]["p75"] == 6.25


def test_readout_is_empty_before_any_beacon(viz, client):
    body = client.get("/api/web-metrics?range=7d", headers=_ORIGIN).json()
    assert body["series"] == [] and body["buckets"] == []


def test_readout_rejects_a_bad_range(viz, client):
    assert client.get("/api/web-metrics?range=9x",
                      headers=_ORIGIN).status_code == 400


def test_sink_rows_reach_the_readout_end_to_end(viz, pinned, client):
    """The whole path, through HTTP, with a population checked exactly.

    `POST /api/metrics` stamps `ts` server-side, so a beacon it writes is
    always recent -- and a recent beacon is correctly absent from the rollup,
    which holds only closed buckets. So the path this proves is the LIVE
    one, and asking for 24h is asking for exactly that. A rollup fed by the
    sink is `test_rollup_folds_exact_percentiles_and_the_sum` plus this.
    """
    for value in _VALUES:
        assert _post(client, _beacon(value=value)).status_code == 202
    body = client.get("/api/web-metrics?range=24h", headers=_ORIGIN).json()
    assert body["exact"] is True
    row = body["series"][0]
    assert (row["n"], row["p50"], row["p75"], row["total"]) == (
        8, 4.5, 6.25, 36.0)
