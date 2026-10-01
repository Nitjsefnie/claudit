"""Frontend performance telemetry (issue #436): the sink's vocabulary, the
beacon endpoint's gates, the percentile rollup, and the readout.

No ingest runs here — `web_metrics` is written by the sink and read by the
rollup, and neither touches `records`, so a fresh schema is the whole fixture.
"""
# pylint: disable=too-many-lines
import math
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import api, db, session as session_mod, web_metrics
from backend.ingest_rollup_web_metrics import rebuild_web_metrics_rollup
from tests import scratch_db

_ORIGIN = {"Origin": "http://testserver"}

# Values 1..8 make the population's percentiles exact and non-round, so a
# broken fold fails instead of agreeing with a plausible constant:
# PERCENTILE_CONT(0.50) = 4.5, PERCENTILE_CONT(0.75) = 6.25, SUM = 36.
_VALUES = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]


@pytest.fixture(name="viz")
def _viz_fixture(monkeypatch):
    """A fresh schema as DATABASE_URL_VIZ — no ingest, no mini R2."""
    yield from scratch_db.scratch_viz_database(monkeypatch, "webmetrics")


@pytest.fixture(name="client")
def _client_fixture(viz):
    """TestClient on the api router, auth bypassed (no middleware).

    Mirrors `tests/test_api.py`'s router-only app: the endpoint's own
    behaviour is what is under test here, and the middleware's gates get
    their own app below so they are not silently absent.
    """
    app = FastAPI()
    app.include_router(api.router)
    yield TestClient(app)


@pytest.fixture(name="gated_client")
def _gated_client_fixture(viz):
    """TestClient through the real auth middleware, as a guest.

    `make_guest_session_token` needs no database — a guest has no
    `user_session` row, and the process-local secret is the whole cookie —
    so the guest path is exercised without standing up a credential.
    """
    app = FastAPI()
    app.middleware("http")(session_mod.auth_middleware)
    app.include_router(api.router)
    client = TestClient(app)
    client.cookies.set(session_mod.SESSION_COOKIE_NAME,
                       session_mod.make_guest_session_token())
    yield client


def _beacon(**over):
    base = {"metric": "dashboard_open", "part": "total", "value": 1200.0}
    base.update(over)
    return base


def _post(client, *beacons, headers=None):
    return client.post("/api/metrics", json={"beacons": list(beacons)},
                       headers=headers or _ORIGIN)


def _rows(viz):
    with db.viz_conn() as conn:
        return conn.execute(
            "SELECT user_id, metric, part, region, phase, value "
            "FROM web_metrics ORDER BY id").fetchall()


def _seed(values, *, ts=None, **over):
    """Insert one grain's population directly, bypassing the sink.

    The values are what the rollup folds; a test that wants to assert a
    percentile states its population here rather than through HTTP.
    """
    moment = ts or datetime.now(timezone.utc) - timedelta(minutes=5)
    with db.viz_conn() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO web_metrics (ts, user_id, metric, part, "
                "region, phase, value) VALUES (%s, 7, %s, %s, %s, %s, %s)",
                [(moment, "dashboard_open", "total", "", "", v)
                 for v in values])
        conn.commit()
    return moment


# --- the vocabulary (backend/web_metrics.py) -------------------------------


def test_normalise_accepts_every_documented_pair():
    for metric, parts in web_metrics.METRIC_PARTS.items():
        for part in parts:
            raw = {"metric": metric, "part": part, "value": 1.0}
            if metric == "layout_shift":
                raw["region"] = "panel_grid"
            if metric in ("layout_shift", "longtask"):
                raw["phase"] = "pre_paint"
            assert web_metrics.normalise(raw)[:2] == (metric, part)


def test_normalise_clamps_rather_than_rejecting_a_wild_value():
    """A corrupt reading costs one row, never the batch."""
    metric, part, _r, _p, value = web_metrics.normalise(
        _beacon(value=1e12))
    assert (metric, part) == ("dashboard_open", "total")
    assert value == web_metrics.PART_LIMITS["total"][1]
    assert web_metrics.normalise(_beacon(value=-5.0))[4] == 0.0


def test_normalise_clamps_a_shift_on_its_own_scale():
    """`shift` is a Layout Instability score, not milliseconds."""
    raw = {"metric": "layout_shift", "part": "shift", "region": "other",
           "phase": "post_usable", "value": 1e9}
    assert web_metrics.normalise(raw)[4] == web_metrics.PART_LIMITS["shift"][1]


def test_normalise_refuses_a_non_finite_value():
    """Starlette's json.loads accepts NaN and Infinity; DOUBLE PRECISION
    would take them and poison a bucket's total."""
    for bad in (math.nan, math.inf, -math.inf):
        with pytest.raises(web_metrics.BeaconError, match="finite"):
            web_metrics.normalise(_beacon(value=bad))


@pytest.mark.parametrize("over, message", [
    ({"metric": "nope"}, "unknown metric"),
    ({"part": "shift"}, "unknown part"),
    ({"value": "12"}, "must be a number"),
    ({"value": True}, "must be a number"),
    ({"region": "panel_grid"}, "carries no region"),
    ({"phase": "pre_paint"}, "carries no region"),
])
def test_normalise_refuses_a_journey_carrying_observer_tags(over, message):
    with pytest.raises(web_metrics.BeaconError, match=message):
        web_metrics.normalise(_beacon(**over))


def test_normalise_requires_the_tag_each_observed_metric_carries():
    with pytest.raises(web_metrics.BeaconError, match="requires a region"):
        web_metrics.normalise(
            {"metric": "layout_shift", "part": "shift", "value": 0.1})
    with pytest.raises(web_metrics.BeaconError, match="carries no region"):
        web_metrics.normalise({"metric": "longtask", "part": "block",
                               "region": "panel_grid", "value": 1.0})
    with pytest.raises(web_metrics.BeaconError, match="requires a phase"):
        web_metrics.normalise(
            {"metric": "longtask", "part": "block", "value": 1.0})


def test_normalise_refuses_an_unknown_observer_term():
    with pytest.raises(web_metrics.BeaconError, match="unknown region"):
        web_metrics.normalise({"metric": "layout_shift", "part": "shift",
                               "region": "sidebar", "value": 0.1})
    with pytest.raises(web_metrics.BeaconError, match="unknown phase"):
        web_metrics.normalise({"metric": "longtask", "part": "block",
                               "phase": "during-sse", "value": 1.0})


def test_parse_batch_bounds_the_batch():
    with pytest.raises(web_metrics.BeaconError, match="carrying 'beacons'"):
        web_metrics.parse_batch([_beacon()])
    with pytest.raises(web_metrics.BeaconError, match="must not be empty"):
        web_metrics.parse_batch({"beacons": []})
    with pytest.raises(web_metrics.BeaconError, match="at most"):
        web_metrics.parse_batch(
            {"beacons": [_beacon()] * (web_metrics.MAX_BEACONS + 1)})
    assert len(web_metrics.parse_batch({"beacons": [_beacon()]})) == 1


# --- the sink --------------------------------------------------------------


def test_sink_stores_the_batch(viz, client):
    r = _post(client, _beacon(value=1500.0),
              {"metric": "layout_shift", "part": "shift",
               "region": "panel_grid", "phase": "pre_paint", "value": 0.0312})
    assert r.status_code == 202, r.text
    assert r.json() == {"ok": True, "stored": 2}
    assert _rows(viz) == [
        (0, "dashboard_open", "total", "", "", 1500.0),
        (0, "layout_shift", "shift", "panel_grid", "pre_paint", 0.0312),
    ]


def test_sink_records_the_session_it_came_from(viz):
    """The stored user_id is the one the middleware put on request.state."""
    app = FastAPI()

    @app.middleware("http")
    async def _fake(request, call_next):
        request.state.user_id = 4242
        return await call_next(request)

    app.include_router(api.router)
    assert _post(TestClient(app), _beacon()).status_code == 202
    assert [row[0] for row in _rows(viz)] == [4242]


def test_sink_refuses_an_unknown_term_and_stores_nothing(viz, client):
    r = _post(client, _beacon(), _beacon(metric="dashboard_open",
                                         part="shift"))
    assert r.status_code == 400
    assert "unknown part" in r.text
    assert _rows(viz) == []


def test_sink_refuses_a_non_json_body(viz, client):
    r = client.post("/api/metrics", content=b"not json", headers=_ORIGIN)
    assert r.status_code == 400
    assert _rows(viz) == []


def test_sink_refuses_a_session_over_its_row_cap(viz, client, monkeypatch):
    """A loop on the page must not become a row-count problem for the box."""
    monkeypatch.setattr(web_metrics, "MAX_ROWS_PER_USER", 2)
    assert _post(client, _beacon(), _beacon()).status_code == 202
    assert len(_rows(viz)) == 2
    r = _post(client, _beacon())
    assert r.status_code == 429
    assert len(_rows(viz)) == 2


# --- the middleware gates (the sink's own route has no gate of its own) ---


def test_guest_may_beacon(viz, gated_client):
    assert _post(gated_client, _beacon()).status_code == 202
    assert len(_rows(viz)) == 1


def test_a_cross_origin_beacon_is_403(viz, gated_client):
    r = gated_client.post("/api/metrics", json={"beacons": [_beacon()]},
                          headers={"Origin": "https://attacker.example"})
    assert r.status_code == 403
    assert "cross-origin" in r.text
    assert _rows(viz) == []


def test_a_beacon_without_a_session_is_401(viz):
    app = FastAPI()
    app.middleware("http")(session_mod.auth_middleware)
    app.include_router(api.router)
    r = _post(TestClient(app), _beacon())
    assert r.status_code == 401
    assert _rows(viz) == []


# --- the rollup ------------------------------------------------------------


def test_rollup_folds_exact_percentiles_and_the_sum(viz):
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


def test_rollup_is_idempotent(viz):
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


def test_rollup_keeps_the_grain_apart(viz):
    """Two grains in one bucket are two rows — the key is the whole grain."""
    _seed(_VALUES)
    with db.viz_conn() as conn:
        conn.execute(
            "INSERT INTO web_metrics (ts, user_id, metric, part, region, "
            "phase, value) VALUES (now(), 7, 'longtask', 'block', '', "
            "'sse_update', 250.0)")
        conn.commit()
    rebuild_web_metrics_rollup()
    with db.viz_conn() as conn:
        grains = conn.execute(
            "SELECT DISTINCT metric, part, region, phase "
            "FROM web_metrics_rollup WHERE bucket_s = 3600").fetchall()
    assert sorted(grains) == [("dashboard_open", "total", "", ""),
                              ("longtask", "block", "", "sse_update")]


def test_prune_keeps_the_window_and_drops_the_rest(viz):
    now = datetime.now(timezone.utc)
    fresh, stale = now - timedelta(hours=1), now - timedelta(
        seconds=web_metrics.RETENTION_S + 3600)
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


def test_the_fold_and_the_prune_cover_disjoint_populations(viz, monkeypatch):
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


def test_readout_serves_the_rollup_for_a_stored_width(viz, client):
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


def test_readout_rolls_the_blend_by_sample_count(viz, client):
    """Two buckets, different sizes: the p50 is weighted by n, not averaged."""
    now = datetime.now(timezone.utc)
    early = now - timedelta(hours=5)
    late = now - timedelta(minutes=5)
    _seed([100.0] * 8, ts=early)
    _seed([900.0], ts=late)
    rebuild_web_metrics_rollup()
    series = client.get("/api/web-metrics?range=7d",
                        headers=_ORIGIN).json()["series"]
    assert len(series) == 1
    row = series[0]
    assert row["n"] == 9 and row["total"] == pytest.approx(1700.0)
    # (8 * 100 + 1 * 900) / 9 — an unweighted mean would read 500.0.
    assert row["p50"] == pytest.approx((800.0 + 900.0) / 9)


def test_readout_takes_the_live_path_for_a_width_the_rollup_omits(viz, client):
    _seed(_VALUES)
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


def test_sink_rows_reach_the_rollup_end_to_end(viz, client):
    """The whole path, through HTTP, with a population the fold can be
    checked against exactly."""
    for value in _VALUES:
        assert _post(client, _beacon(value=value)).status_code == 202
    rebuild_web_metrics_rollup()
    row = client.get("/api/web-metrics?range=7d",
                     headers=_ORIGIN).json()["series"][0]
    assert (row["n"], row["p50"], row["p75"], row["total"]) == (
        8, 4.5, 6.25, 36.0)


def test_the_schema_carries_both_tables(viz):
    with psycopg.connect(db.os.environ["DATABASE_URL_VIZ"]) as conn:
        names = {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_name LIKE 'web_metrics%'").fetchall()}
    assert names == {"web_metrics", "web_metrics_rollup"}
