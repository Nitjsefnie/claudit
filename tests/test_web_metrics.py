"""Frontend performance telemetry (issue #436): the sink's vocabulary, the
beacon endpoint's gates, the percentile rollup, and the readout.

No ingest runs here — `web_metrics` is written by the sink and read by the
rollup, and neither touches `records`, so a fresh schema is the whole fixture.
"""
# pylint: disable=too-many-lines
import math
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# The shipped client's own driver, reused: it already fakes every browser
# global src/perf.js touches, and a second harness here would be a second
# thing to keep in step with the file. pylint reads a bare sibling module as
# third-party, hence its position above the first-party block.
from test_perf_js import _run

from backend import api, cache, db, session as session_mod, web_metrics
from backend.constants import LATENCY_BUCKETS
from tests import scratch_db

_ORIGIN = {"Origin": "http://testserver"}

# Values 1..8 make the population's percentiles exact and non-round, so a
# broken fold fails instead of agreeing with a plausible constant:
# PERCENTILE_CONT(0.50) = 4.5, PERCENTILE_CONT(0.75) = 6.25, SUM = 36.
_VALUES = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]


#: The fold's clock, pinned. Every rollup assertion below reads its instants
#: from here, because the band a seed must land in is exactly one fold
#: interval wide and a live clock puts it on the boundary.
PINNED_NOW = datetime(2026, 5, 14, 9, 20, 0, tzinfo=timezone.utc)


def _closed_ts() -> datetime:
    """An instant whose bucket has CLOSED at EVERY stored width.

    A bucket is closed once its span has passed (`web_metrics.rollup_horizon`
    is `now`, because the sink stamps every beacon server-side), so the widest
    width is the last to close and it sets the distance: a seed a full widest
    bucket plus a margin back is closed at every width at once, which is what
    lets one instant serve a test that folds at all four.

    A live-path assertion wants the opposite, and says so with `_recent_ts`.
    """
    return PINNED_NOW - timedelta(
        seconds=max(LATENCY_BUCKETS) + 1800)


#: The fold's clock, pinned. Every rollup assertion below reads its instants
#: from here, because the band a seed must land in is exactly one bucket
#: width wide and a live clock puts it on the boundary.
PINNED_NOW = datetime(2026, 5, 14, 9, 20, 0, tzinfo=timezone.utc)


@pytest.fixture(name="pinned")
def _pinned_fixture(monkeypatch):
    """Pin the fold's clock so `PINNED_NOW` and the fold agree exactly."""
    monkeypatch.setattr(web_metrics, "utcnow", lambda: PINNED_NOW)
    return PINNED_NOW


def _recent_ts() -> datetime:
    """An instant still inside the raw window and inside an OPEN bucket."""
    return PINNED_NOW - timedelta(minutes=5)


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

    The default instant is a CLOSED bucket, not a recent one: a bucket is
    stored by the fold that closes it, so a beacon five minutes old is
    correctly absent from the rollup and lives in the live pass. Pass `ts`
    explicitly for a test that is about the live path.
    """
    moment = ts or _closed_ts()
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
    """A loop on the page must not become a row-count problem for the box.

    The router-only client has no session, so it is a GUEST and the ceiling
    that applies is the guest one — which is the asymmetry the next two
    cases pin.
    """
    monkeypatch.setattr(web_metrics, "MAX_ROWS_PER_GUEST", 2)
    assert _post(client, _beacon(), _beacon()).status_code == 202
    assert len(_rows(viz)) == 2
    r = _post(client, _beacon())
    assert r.status_code == 429
    assert len(_rows(viz)) == 2


def test_a_guest_ceiling_is_lower_than_a_named_one_s(viz):
    """Asymmetry is about trust, not worth: a guest is anonymous.

    Every anonymous session shares `user_id = 0`, so without a lower ceiling
    one caller can spend the whole anonymous budget and crowd out everyone
    else's — the opposite of what a cap is for.
    """
    assert web_metrics.cap_for(web_metrics.GUEST_USER_ID) == \
        web_metrics.MAX_ROWS_PER_GUEST
    assert web_metrics.cap_for(7) == web_metrics.MAX_ROWS_PER_USER
    assert web_metrics.MAX_ROWS_PER_GUEST < web_metrics.MAX_ROWS_PER_USER


def test_the_guest_cap_is_enforced_against_the_guest(
        viz, gated_client, monkeypatch):
    """And it is the ceiling actually applied to an anonymous POST."""
    monkeypatch.setattr(web_metrics, "MAX_ROWS_PER_GUEST", 2)
    assert _post(gated_client, _beacon(), _beacon()).status_code == 202
    assert _post(gated_client, _beacon()).status_code == 429
    assert len(_rows(viz)) == 2


def test_the_readout_discloses_the_guest_share(viz, pinned, client):
    """Disclosure, not exclusion.

    Guests are deliberately IN the panel's population — this host is
    guest-heavy and a panel blind to its own traffic is the worse failure.
    But a skewed panel has to look skewed, so the payload carries the count
    the panel renders as "guests: N of M".
    """
    _seed(_VALUES, ts=_recent_ts())
    body = client.get("/api/web-metrics?range=1d", headers=_ORIGIN).json()
    assert body["beacons"] >= 1
    assert 0 <= body["guests"] <= body["beacons"]
    with db.viz_conn() as conn:
        conn.execute("UPDATE web_metrics SET user_id = 0")
        conn.commit()
    # The readout is `@cache_response`, so a second identical request would
    # return the FIRST one's payload and the disclosure would look broken.
    cache.response_cache.clear()
    after = client.get("/api/web-metrics?range=1d", headers=_ORIGIN).json()
    assert after["guests"] == after["beacons"], (
        "every row is anonymous and the payload does not say so")


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


# --- the schema, and the shipped client against this sink ------------------

def test_the_schema_carries_both_tables(viz):
    with psycopg.connect(db.os.environ["DATABASE_URL_VIZ"]) as conn:
        names = {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_name LIKE 'web_metrics%'").fetchall()}
    assert names == {"web_metrics", "web_metrics_rollup"}


# --- the shipped client, against THIS sink's vocabulary -------------------
#
# The client and the sink each close over the same five metrics and their
# parts, regions and phases, and nothing has ever made them agree: the
# client's own suite pins it against a restatement of the table written in
# the test file, which is a second copy that drifts. These run the REAL
# src/perf.js through node, collect every beacon it can emit, and hand each
# one to `web_metrics.normalise` — so the sink is the authority and a term
# the backend would reject is a test failure here rather than a 400 that
# discards the whole batch in a browser nobody is watching.


@pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available")
def test_every_beacon_the_client_can_emit_is_inside_the_sink_contract():
    out = _run("""
      // All three journeys, each with a measured fetch, so every
      // (metric, part) pair the client can produce is exercised.
      for (const name of ['dashboard_open', 'inspector_open', 'signin']) {
        window.perf.openJourney(name);
        clock += 100;
        window.perf.closeFetch(name);
        clock += 300;
        window.perf.closeJourney(name);
      }
      window.perf.markUsable();
      window.perf.sseUpdate();
      const inGrid = {
        getAttribute: n => (n === 'data-perf-region' ? 'panel_grid' : null),
        parentElement: { getAttribute: () => null, parentElement: null },
      };
      __emit('layout-shift', [
        { value: 0.04, hadRecentInput: false, sources: [{node: inGrid}] },
      ]);
      __emit('layout-shift', [
        { value: 0.01, hadRecentInput: false, sources: [] },
      ]);
      __emit('longtask', [{ duration: 250 }]);
      __tick();
      console.log(JSON.stringify({ beacons: beacons() }));
    """, sendBeacon=True, observers=["layout-shift", "longtask"])

    rows = out["beacons"]
    assert rows, "the driver emitted nothing, so this proves nothing"
    # Every metric the client can name, and every part of each.
    assert {r["metric"] for r in rows} == {
        "dashboard_open", "inspector_open", "signin",
        "layout_shift", "longtask"}
    for row in rows:
        try:
            web_metrics.normalise(row)
        except web_metrics.BeaconError as error:
            pytest.fail(
                f"the shipped client emits a beacon the sink refuses: "
                f"{row!r} -- {error}")
    # The sink accepts terms the client never emits, which is fine and
    # expected, so this is a floor on coverage and not an equality.
    assert len(rows) == 3 * 3 + 2 + 1


_ROOT = Path(__file__).resolve().parents[1]


def _client_table(name: str) -> tuple[str, ...]:
    """The closed set `src/perf.js` declares, read out of the shipped file."""
    source = (_ROOT / "src" / "perf.js").read_text(encoding="utf-8")
    match = re.search(rf"const {name} = \[([^\]]*)\]", source)
    assert match, f"src/perf.js no longer declares a {name} table"
    return tuple(re.findall(r"'([^']+)'", match.group(1)))


def test_the_client_and_the_sink_name_the_same_regions_and_phases():
    """The behavioural check above cannot see a set that DIVERGED.

    `perf.js` walks past a `data-perf-region` it does not recognise and
    falls back to 'other', so widening its own table emits no beacon the
    sink would refuse -- the check stays green while the two vocabularies
    quietly stop meaning the same thing. Compare the declarations instead,
    and read them out of the shipped files rather than restating them.
    """
    assert _client_table("REGIONS") == web_metrics.REGIONS
    # The client declares no PHASES table -- it assigns the three literals
    # directly -- so they are collected from the assignment sites.
    perf = (_ROOT / "src" / "perf.js").read_text(encoding="utf-8")
    assert set(re.findall(r"phase = '([^']+)'", perf)) == set(web_metrics.PHASES), (
        "the phases src/perf.js can enter do not match the sink's")
    # Journeys are call-site arguments rather than a table, so they are read
    # off the file's own use of them.
    app = (_ROOT / "src" / "app.jsx").read_text(encoding="utf-8")
    opened = set(re.findall(r"openJourney\('([^']+)'\)", app))
    assert opened <= set(web_metrics.JOURNEYS), (
        "app.jsx opens a journey the sink does not accept")
    # `signin` is the one the app.jsx does NOT open: it is ADOPTED inside
    # perf.js from the marker the sign-in page left in sessionStorage,
    # because it starts on a document that no longer exists by the time the
    # signed-in page runs. Pinning the asymmetry is the point -- if a fourth
    # journey ever appears, or the third moves to a call site, this says so.
    assert set(web_metrics.JOURNEYS) - opened == {"signin"}, (
        "a journey the sink accepts has no opener: either the sign-in "
        "journey moved to a call site, or the sink names one nothing starts")
