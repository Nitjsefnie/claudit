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

import logging
import re
from datetime import datetime, timedelta, timezone

import pytest

from test_web_metrics import (  # noqa: F401  (fixtures re-exposed below)
    _ORIGIN, PINNED_NOW, _VALUES, _beacon, _client_fixture, _closed_ts,
    _pinned_fixture, _post, _recent_ts, _rows, _seed, _viz_fixture,
)

from backend import db, web_metrics
from backend.api_web_metrics import web_metrics_readout
from backend.api_web_metrics import _series_live
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


def test_the_fold_reads_every_row_the_prune_deletes(viz, pinned, monkeypatch):
    """A row one second outside the window is never pruned unread.

    The fold and the prune need no ordering between them for one reason: the
    fold's read window is never NEWER than the prune's delete bound, so
    whatever the prune drops was already offered to the fold. The two used to
    be the same instant — disjoint populations by construction. Issue #475
    moved them apart: the read window now reaches back to the oldest row on
    the table when a stalled pass has left the working set wider than its
    nominal width, so the fold can read rows the prune is about to delete.

    Which is what makes the boundary worth pinning. A row just OUTSIDE the
    window shares an hourly bucket with one just inside it, so the bucket
    cannot be read whole, and the fold refuses it — before #475 it stored the
    inner row alone, which is the partial-bucket defect wearing a test's
    clothes: an `n = 1` row that looks like a fact and cannot be revised.
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
    assert totals == set(), (
        "a bucket one row short of readable was stored as the rows that "
        "survived; it must be refused instead")


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
        assert span.total_seconds() <= web_metrics.RAW_KEEP_S
        assert _bucket_seconds(span) not in LATENCY_BUCKETS, (
            f"{hours}h now folds to a stored width, so a range the raw rows "
            f"cover is answered from the rollup and blended")

    _seed(_VALUES, ts=_recent_ts())
    body = client.get("/api/web-metrics?range=1d", headers=_ORIGIN).json()
    assert body["exact"] is True
    assert body["series"][0]["p50"] == 4.5
    assert body["since"], "the window actually read is reported"


def test_the_live_pass_clamps_a_window_past_the_table(viz, pinned, client):
    """The clamp, pinned where a `range` value can no longer reach it.

    `RAW_KEEP_S` now exceeds the 100-hour span at which `_bucket_seconds`
    first reaches a stored width, so every range the UI can ask for is inside
    the window and no `?range=` value exercises the clamp. That is worth
    knowing, and it is why the earlier band assertion — "past RAW_KEEP_S and
    below 100h" — is gone: it asserted a band the design has since closed, and
    a test pinning a fiction is how the next reader wastes an afternoon.

    So the clamp is pinned by CALLING the reader with a window past the
    table, which is exactly what a future retention change would do. The
    rollup path is a different question and is pinned below.
    """
    _seed(_VALUES, ts=_recent_ts())
    far_past = PINNED_NOW - timedelta(seconds=web_metrics.RAW_KEEP_S * 2)
    body = _series_live("400d", 60, far_past, PINNED_NOW)
    since = datetime.fromisoformat(body["since"].replace("Z", "+00:00"))
    assert since == PINNED_NOW - timedelta(seconds=web_metrics.RAW_KEEP_S), (
        f"the live pass answered from {since}, not from the oldest row it "
        f"keeps")
    assert body["series"][0]["n"] == len(_VALUES), (
        "the clamp moved the window but the row it read is not there")


def test_the_live_pass_over_a_window_the_table_covers_is_exact(viz, pinned,
                                                               client):
    """The control: inside the window, the answer is the whole range.

    Without this the case above would pass on an empty series, since a
    clamped window past the table reads nothing at all.
    """
    _seed(_VALUES, ts=_recent_ts())
    body = client.get("/api/web-metrics?range=1d", headers=_ORIGIN).json()
    assert body["exact"] is True
    assert body["series"][0]["n"] == len(_VALUES)
    assert body["series"][0]["p50"] == 4.5


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


@pytest.fixture(name="closing_horizon")
def _closing_horizon_fixture(monkeypatch):
    """The shipped horizon, which is `now`, and the shipped read window.

    There is nothing to collapse any more: `rollup_horizon` IS `now`, and the
    read window is `retention_cutoff`. An earlier version of this suite
    shortened the horizon to reach a closed bucket in a few folds, and that is
    exactly the fiction this case exists to catch — so the fixture now uses
    the real relationship and the case says so.
    """
    return monkeypatch


def test_the_fold_that_closes_a_bucket_stores_all_of_it(
        viz, closing_horizon):
    """The fold that CLOSES a bucket stores all of it, and it stays whole.

    Not a repair test: there is no repair. The read window reaches back two
    widths, so every closed bucket is whole when it is first read, and the
    row is never revised. An earlier version of this suite tested a
    `read_from` discriminator that let a later fold replace a partial row --
    which could not work, because the window advances with `now` and a bucket
    the window has cut off never becomes whole again. The fix was to size
    the window from the arithmetic, and this is what that has to keep doing.

    So: fold at the instant the day-bucket closes, and the row must already
    hold all 24 beacons; then fold again at +1h, +2h, +24h and +400h, with a
    backfilled beacon inside the closed bucket in between, and it must not
    move.
    """
    # The shipped value, asserted before the behaviour: the fixture above
    # derives its window FROM `RAW_KEEP_S`, so narrowing the constant narrows
    # the fixture with it and this case would stay green. What was wrong was
    # the constant, so the constant is what needs the assertion.
    #
    # A closed bucket `[s, s+W)` has s > now - 2W (see the derivation on
    # web_metrics.RAW_KEEP_S), so the read window must reach
    # back at least that far. One width instead of two leaves the newest
    # closing bucket's oldest beacons outside it, and nothing later repairs
    # that — the window advances with now, so a cut-off bucket stays cut off.
    assert web_metrics.RAW_KEEP_S >= 2 * max(LATENCY_BUCKETS), (
        f"RAW_KEEP_S = {web_metrics.RAW_KEEP_S} is less than two bucket "
        f"widths, so a fold that catches a bucket the moment it closes "
        f"stores it short and can never repair it")

    # A fold at the instant the day-bucket [base, base+24h) CLOSES, which is
    # when a fold is most likely to catch it at the window's edge: the
    # horizon is now - 2h, so `now` is the bucket's end plus that slack.
    base = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    now = base + timedelta(hours=26)
    for hour in range(24):
        _seed([float(hour)], ts=base + timedelta(hours=hour))
    _fold_at(now)
    first = _rollup_rows(viz, 86400)
    assert first, "the closing bucket was not stored at all"
    n, _p50, _p75, total = next(iter(first.values()))
    assert n == 24, (
        f"the fold that CLOSED the bucket stored {n} of its 24 beacons; the "
        f"read window reached back only one width and the oldest were cut off")
    assert total == sum(float(h) for h in range(24)), total

    # And it stays whole: folds at the boundary, an hour later, and a day
    # later, with a backfill beacon inside the closed bucket in between.
    _seed([999.0], ts=base + timedelta(hours=2))
    for later in (1, 2, 24, 400):
        _fold_at(now + timedelta(hours=later))
        after = _rollup_rows(viz, 86400)
        assert after == first, (
            f"a fold {later}h later changed a stored closed bucket: {after}")


# --- the stall (issue #475) ------------------------------------------------
#
# The window is `RAW_KEEP_S - widest` = 30h wide, so `RAW_KEEP_S` makes a
# partial read of a closed bucket impossible ON EVERY NORMAL FOLD -- and says
# so. It cannot make it impossible on a fold that does not happen: a pass
# stalled longer than the window resumes with a read window that has moved
# INTO the bucket it is about to close, stores the newest hours of it, and
# `ON CONFLICT DO NOTHING` freezes that for good, indistinguishable from a
# whole row. The rule the stall must not break is the one the window was
# sized for: stored whole, or not at all.


def test_a_stalled_fold_stores_whole_buckets(viz, closing_horizon):
    """A pass that missed its window still stores a bucket WHOLE.

    Seeded across a whole day-bucket, then folded ONCE, thirty-six hours after
    that bucket closed -- past the 30h window, so the resuming fold's
    `retention_cutoff` lands inside the bucket and reads only its tail.
    """
    base = datetime(2026, 6, 20, 0, 0, 0, tzinfo=timezone.utc)
    for hour in range(24):
        _seed([float(hour)], ts=base + timedelta(hours=hour))
    stalled = base + timedelta(hours=60)
    # The scenario, pinned before the behaviour: the shipped read window
    # really does open inside the bucket, so this case cannot pass by the
    # constant alone having been widened until the stall stops mattering.
    assert base < web_metrics.retention_cutoff(stalled) < base + timedelta(
        hours=24), (
        f"the shipped retention window opens at "
        f"{web_metrics.retention_cutoff(stalled)}, which no longer cuts into "
        f"the day bucket [{base}, +24h) -- the stall this case reproduces no "
        f"longer reaches inside it")

    _fold_at(stalled)
    rows = _rollup_rows(viz, 86400)
    assert rows, "a day bucket closed 36h ago must still be stored"
    n, _p50, _p75, total = next(iter(rows.values()))
    assert n == 24, (
        f"a fold stalled past the window stored {n} of the bucket's 24 "
        f"beacons; the read window had moved inside the bucket and the row "
        f"was frozen partial")
    assert total == sum(float(hour) for hour in range(24)), total

    # And whole stays whole: four more passes, the last a long way on.
    for later in (6, 12, 24, 400):
        _fold_at(stalled + timedelta(hours=later))
        assert _rollup_rows(viz, 86400) == rows, (
            f"a fold {later}h after the stall changed a stored day bucket")


def test_a_bucket_the_fold_cannot_read_whole_is_not_stored(viz,
                                                           closing_horizon,
                                                           caplog):
    """The other half of the rule: absent, rather than a short row.

    No stall is needed to arrange this one -- the working set simply no longer
    holds the bucket's oldest beacons, which is what a fold that read a
    narrower window than it stores would look like from the outside. A stored
    `n = 18` row is the failure, because nothing can ever tell it from a whole
    one: `DO NOTHING` will not revise it and no marker distinguishes it.
    """
    base = datetime(2026, 6, 21, 0, 0, 0, tzinfo=timezone.utc)
    for hour in range(24):
        _seed([float(hour)], ts=base + timedelta(hours=hour))
    with db.viz_conn() as conn:
        conn.execute("DELETE FROM web_metrics WHERE ts < %s",
                     (base + timedelta(hours=6),))
        conn.commit()

    with caplog.at_level(logging.INFO, logger="claudit.ingest"):
        _fold_at(base + timedelta(hours=60))
    assert _rollup_rows(viz, 86400) == {}, (
        "a bucket whose oldest beacons are gone from the working set was "
        "stored as the tail that survived -- a short row nothing can repair")

    # The positive witness, in this same control, because `== {}` on its own
    # is satisfied just as well by a fold that stored NOTHING AT ALL — a
    # broken query, a width nobody folds, an exception swallowed upstream.
    # The same run stored every hourly bucket whose beacons survived, so the
    # day bucket's absence is a refusal and not a fold that did nothing.
    hourly = _rollup_rows(viz, 3600)
    assert len(hourly) == 18, (
        f"the same fold stored {len(hourly)} hourly buckets where the beacons "
        f"survived, so the day bucket's absence is a refusal rather than a "
        f"fold that stored nothing")
    assert {n for n, _p50, _p75, _total in hourly.values()} == {1}, (
        f"the surviving beacons are one per hour bucket: {hourly}")

    # And the refusal is not silent. A bucket absent from the stored history
    # is indistinguishable from one that never had a beacon; the issue this
    # closes objected to exactly that, "permanently and without a log line".
    reported = re.search(r"(\d+) buckets refused", caplog.text)
    assert reported, (
        f"the fold refused a bucket and said nothing: {caplog.text}")
    assert int(reported.group(1)) >= 1, caplog.text


def test_a_later_fold_does_not_rewrite_a_whole_row(viz, closing_horizon):
    """The repair is one-directional, and a backfill must not rewrite it.

    The companion to the case above: if the conflict clause simply always
    updated, a beacon backfilled into an already-closed bucket would silently
    move history. It may not, and a later fold reading the same complete set
    may not either.
    """
    base = datetime(2026, 6, 2, 0, 0, 0, tzinfo=timezone.utc)
    now = base + timedelta(hours=26)
    for hour in range(24):
        _seed([float(hour)], ts=base + timedelta(hours=hour))
    _fold_at(now)
    before = _rollup_rows(viz, 86400)
    assert before and next(iter(before.values()))[0] == 24

    # A backfill into the closed bucket, and a fold much later.
    _seed([999.0], ts=base + timedelta(hours=3))
    _fold_at(now + timedelta(hours=30))
    after = _rollup_rows(viz, 86400)
    assert after == before, (
        "a beacon backfilled into a closed bucket rewrote a stored row")


def test_a_wider_range_never_serves_fewer_beacons(viz, pinned):
    """The regression a cross-model review found, pinned as an inequality.

    The reader served wide ranges from the rollup ALONE, and the rollup holds
    only closed buckets — so the newest beacons, and everything since the last
    fold, were in no bucket at all. The result was not a slightly stale
    number: a WIDER range reported FEWER beacons than a narrower one, and the
    default `all` view served 42 of 240 on a ten-day corpus.

    So the property is the whole assertion: fold hourly across a corpus, then
    walk the ranges and require the count to be non-decreasing, and the widest
    to reach every beacon.
    """
    base = PINNED_NOW - timedelta(hours=240)
    with db.viz_conn() as conn:
        for hour in range(240):
            conn.execute(
                "INSERT INTO web_metrics (ts, user_id, metric, part, region,"
                " phase, value) VALUES (%s, 1, 'dashboard_open', 'total',"
                " '', '', 1.0)", (base + timedelta(hours=hour),))
        conn.commit()
    # Fold as the scheduler does: hourly, ending now.
    for hour in range(240):
        moment = base + timedelta(hours=hour)
        web_metrics.utcnow = lambda moment=moment: moment
        rebuild_web_metrics_rollup()

    seen = []
    for rng in ("1d", "4d", "5d", "7d", "30d", "all"):
        body = web_metrics_readout(rng=rng)
        seen.append((rng, body["series"][0]["n"] if body["series"] else 0))
    counts = [n for _, n in seen]
    assert counts == sorted(counts), (
        f"a wider range served fewer beacons than a narrower one: {seen}")
    assert counts[-1] == 240, (
        f"`all` served {counts[-1]} of 240 beacons with hourly folds in "
        f"place — the tail is not being unioned back in: {seen}")


def test_since_names_the_data_served_not_the_range_asked_for(viz, pinned):
    """`since` EQUALS the oldest data served — an equality, not a bound.

    `since` existed to stop the reader believing a longer label: on the
    rollup path it used to echo the REQUESTED window while the data stopped
    short of it, which is the exact lie the field was added to prevent.

    It was first pinned with two `since <= X` assertions, and both of them
    held against the buggy code — an echo of the requested window names an
    instant OLDER than the data, so `requested <= oldest-served` is true
    exactly when the bug is present. A one-sided bound cannot catch a value
    that is wrong by being too early; only an equality, or the symmetric
    bound, can. The cross-model review ran the original assertions against
    the pre-fix head and they were green, which is how the toothless pin
    was found. This one asserts the identity, and the count assertion in
    `test_a_wider_range_never_serves_fewer_beacons` is what actually
    discriminates the data itself.
    """
    base = PINNED_NOW - timedelta(hours=240)
    with db.viz_conn() as conn:
        for hour in range(240):
            conn.execute(
                "INSERT INTO web_metrics (ts, user_id, metric, part, region,"
                " phase, value) VALUES (%s, 1, 'dashboard_open', 'total',"
                " '', '', 1.0)", (base + timedelta(hours=hour),))
        conn.commit()
    for hour in range(240):
        moment = base + timedelta(hours=hour)
        web_metrics.utcnow = lambda moment=moment: moment
        rebuild_web_metrics_rollup()

    body = web_metrics_readout(rng="all")
    since = datetime.fromisoformat(body["since"].replace("Z", "+00:00"))
    width = body["bucket_s"]
    starts = [datetime.fromisoformat(b["ts"].replace("Z", "+00:00"))
              - timedelta(seconds=width / 2) for b in body["buckets"]]
    assert since == min(starts), (
        f"`since` ({since}) is not the oldest bucket the answer rests on "
        f"({min(starts)}); it is echoing the requested window again")
    # And the symmetric bound, so a future change that reports the requested
    # window is caught from the other side as well.
    assert since >= min(starts) - timedelta(seconds=1)
