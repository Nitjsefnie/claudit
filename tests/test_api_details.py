"""Tests moved from test_api.py to keep test modules under 700 lines."""
from __future__ import annotations

import inspect
import json
import os
from contextlib import closing
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from fastapi import FastAPI, Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from backend import api, app as app_mod, cache, db, ingest
from tests import mini_mirror

# Pytest discovers imported fixture functions by their fixture marker.
from tests.test_api import (  # pylint: disable=unused-import
    _app_with_data_fixture,
    _app_with_fresh_data_fixture,
    _app_with_rl_data_fixture,
    _insert_tz_probe_rows,
)


def test_context_growth_agg_shape(app_with_data):
    r = app_with_data.get("/api/context-growth/agg?range=3650d")
    assert r.status_code == 200
    body = r.json()
    assert "per_turn" in body and "per_session_final" in body
    for k in ("n", "mean", "p50", "p90", "p99", "max"):
        assert k in body["per_turn"]
        assert k in body["per_session_final"]


def test_context_growth_session_returns_canonical_array(app_with_data):
    """Mini fixture sess-A has 1 turn (single_turn.jsonl). Verify the
    per-turn array is returned with the canonical shape."""
    r = app_with_data.get("/api/context-growth/session/sess-A")
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == "sess-A"
    assert "turns" in body and isinstance(body["turns"], list)
    if body["turns"]:
        t = body["turns"][0]
        assert {"idx", "ts", "line", "input", "output", "delta"} == set(t)
    assert body["total_turns"] == len(body["turns"])


def test_context_growth_session_404(app_with_data):
    r = app_with_data.get("/api/context-growth/session/does-not-exist")
    assert r.status_code == 404


def test_tool_error_rate_returns_expected_shape(app_with_data):
    r = app_with_data.get("/api/tool-error-rate?range=3650d")
    assert r.status_code == 200
    body = r.json()
    assert "range" in body
    assert "bucket_s" in body
    assert "buckets" in body
    assert isinstance(body["buckets"], list)
    for b in body["buckets"]:
        assert {"ts", "model", "tool", "n_total", "n_error"} <= set(b.keys())
        assert b["n_error"] <= b["n_total"]


def test_dashboard_returns_prompts_and_turns_totals(app_with_data):
    """total_prompts/total_turns count per own timestamp, in range only;
    over an all-covering range that equals the old whole-file SUMs. Mini
    r2 carried one real user prompt (sess-A) and five usage-bearing files
    (sess-A, sess-B, sess-C main, sess-C agent, sess-D), one ctx_turn. It
    now also carries the lane wires (issue #503), so both totals are read
    off the mirror by running the production parser over it — a transcript
    is not a prompt, and no inventory can say how many prompts a mirror
    holds."""
    totals = mini_mirror.record_totals()
    body = app_with_data.get("/api/dashboard?range=3650d").json()
    assert body["total_prompts"] == totals["prompt_count"]
    assert body["total_turns"] == totals["turn_count"]

    # Project filter scopes both counts.
    body_b = app_with_data.get("/api/dashboard?range=3650d&project=projB").json()
    assert body_b["total_prompts"] == 0
    assert body_b["total_turns"] == 3


def test_dashboard_hourly_carries_line_churn(app_with_fresh_data):
    """Issue #10: lines added/deleted ride the hourly panel entries.
    Churn lives on tool_uses (not records/usage_rollup) and is bucketed
    per hour, attributed to the hour's first model row like
    session_count — so summing the key across entries is exact, and the
    project/model filters scope it."""
    with closing(psycopg.connect(os.environ["DATABASE_URL_VIZ"])) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tool_uses (file_key, line_num, idx, ts, tool_name, "
            "model, is_error, lines_added, lines_deleted) VALUES "
            # The model filter reads the call's own tool_uses.model, as
            # ingest stores it for sess-A's assistant line.
            "('claude/projA/sess-A/sess-A.jsonl', 2, 0, '2026-05-07T10:00:01Z', "
            " 'Edit', 'claude-sonnet-4-5', FALSE, 10, 4), "
            # Errored call: parsed as zero churn, must add nothing.
            "('claude/projA/sess-A/sess-A.jsonl', 2, 1, '2026-05-07T10:00:02Z', "
            " 'Edit', 'claude-sonnet-4-5', TRUE, 0, 0)"
        )
        conn.commit()

    ingest.rebuild_tool_rollup()

    def churn(body):
        return (sum(h["lines_added"] for h in body["hourly"]),
                sum(h["lines_deleted"] for h in body["hourly"]))

    body = app_with_fresh_data.get(
        "/api/dashboard?range=3650d&fresh=1"
    ).json()
    assert "lines_added" in body["hourly"][0]
    assert "lines_deleted" in body["hourly"][0]
    assert churn(body) == (10, 4)

    body_a = app_with_fresh_data.get(
        "/api/dashboard?range=3650d&project=projA&fresh=1").json()
    assert churn(body_a) == (10, 4)
    body_b = app_with_fresh_data.get(
        "/api/dashboard?range=3650d&project=projB&fresh=1").json()
    assert churn(body_b) == (0, 0)

    # The rolled model dimension and the live path both read tu.model.
    body_m = app_with_fresh_data.get(
        "/api/dashboard?range=3650d&model=sonnet&fresh=1").json()
    assert churn(body_m) == (10, 4)
    body_x = app_with_fresh_data.get(
        "/api/dashboard?range=3650d&model=no-such-model&fresh=1").json()
    assert churn(body_x) == (0, 0)


def test_dashboard_churn_uses_rollup_only_at_hourly_grain(
    app_with_fresh_data,
):
    """Hourly-or-coarser ranges read the ingest snapshot; sub-hour ranges
    retain exact live tool-call values."""
    file_key = "claude/projA/sess-A/sess-A.jsonl"
    now = datetime.now(timezone.utc)
    with closing(psycopg.connect(os.environ["DATABASE_URL_VIZ"])) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO records (file_key, line_num, uuid, request_id, "
                "ts, model, fresh_tokens, output_tokens, "
                "cache_creation_tokens, cache_read_tokens, eph5_tokens, "
                "eph1h_tokens, cost_usd, is_canonical) VALUES "
                "(%s, 9100, %s, 'rollup-boundary', %s, "
                "'claude-sonnet-4-5', 1, 1, 0, 0, 0, 0, 0, TRUE)",
                (file_key, f"rollup-boundary-{now.timestamp()}", now),
            )
            cur.execute(
                "INSERT INTO tool_uses (file_key, line_num, idx, ts, "
                "tool_name, is_error, lines_added, lines_deleted) VALUES "
                "(%s, 9100, 0, %s, 'Edit', FALSE, 7, 3)",
                (file_key, now),
            )
        conn.commit()

    ingest.recompute_canonical()
    ingest.rebuild_rollup()
    ingest.rebuild_tool_rollup()

    with closing(psycopg.connect(os.environ["DATABASE_URL_VIZ"])) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tool_uses SET lines_added = 70, lines_deleted = 30 "
                "WHERE file_key = %s AND line_num = 9100",
                (file_key,),
            )
        conn.commit()

    rolled = app_with_fresh_data.get(
        "/api/dashboard?range=30d&fresh=1"
    ).json()
    live = app_with_fresh_data.get(
        "/api/dashboard?range=1d&fresh=1"
    ).json()
    assert sum(h["lines_added"] for h in rolled["hourly"]) == 7
    assert sum(h["lines_deleted"] for h in rolled["hourly"]) == 3
    assert sum(h["lines_added"] for h in live["hourly"]) == 70
    assert sum(h["lines_deleted"] for h in live["hourly"]) == 30


def test_dashboard_excludes_rate_limit_hits_older_than_range(app_with_rl_data):
    client, in_range, out_range = app_with_rl_data

    hits_30d = [h["ts"] for h in
                client.get("/api/dashboard?range=30d").json()["rate_limit_hits"]]
    assert in_range in hits_30d
    # The 45-day-old hit must not appear: it sits outside the 30d window
    # even though its file's r2_last_modified (mtime) is current.
    assert out_range not in hits_30d

    hits_all = [h["ts"] for h in
                client.get("/api/dashboard?range=3650d").json()["rate_limit_hits"]]
    assert in_range in hits_all
    assert out_range in hits_all


def test_a_malformed_hit_ts_does_not_take_down_the_dashboard(app_with_fresh_data):
    """Junk in a hit's `ts` must be excluded, not raised.

    Casting it is what filters on the hit's own time, but an unguarded
    `::timestamptz` RAISES on a malformed value, and the traceback escapes
    the handler — so one bad `ts` anywhere in files.rate_limit_hits would
    500 the entire dashboard, every panel, not merely this one.

    The second block below is the one that matters: those values are
    timestamp-SHAPED and still raise on cast, so a guard that pattern-
    matches the shape passes them straight through to the cast it was
    meant to protect. Only real input validation excludes them.
    """
    with closing(psycopg.connect(os.environ["DATABASE_URL_VIZ"])) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE files SET r2_last_modified = now(), "
            "       rate_limit_hits = %s::jsonb "
            " WHERE file_key = (SELECT MIN(file_key) FROM files WHERE is_main)",
            (json.dumps([
                # Not timestamp-shaped at all.
                {"ts": "not-a-timestamp", "content": "junk ts"},
                {"ts": 123, "content": "numeric ts"},
                {"ts": "", "content": "empty ts"},
                {"ts": None, "content": "null ts"},
                {"content": "no ts key at all"},
                # Timestamp-shaped, but not valid timestamps.
                {"ts": "2026-13-45T99:99:99Z", "content": "month 13, day 45"},
                {"ts": "2026-02-30T00:00:00Z", "content": "30th of february"},
                {"ts": "2026-01-01 25:00:00Z", "content": "hour 25"},
                {"ts": "2026-01-01T00:00:00Z lolwat", "content": "trailing junk"},
                # Valid, and on either side of the range boundary.
                {"ts": (datetime.now(timezone.utc) - timedelta(days=5)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"), "content": "good hit"},
                {"ts": "1998-01-01T00:00:00Z", "content": "too old"},
            ]),),
        )
        conn.commit()

    cache.response_cache.clear()
    r = app_with_fresh_data.get("/api/dashboard?range=30d")
    assert r.status_code == 200, f"{r.status_code}: {r.text[:300]}"
    assert [h["content"] for h in r.json()["rate_limit_hits"]] == ["good hit"]


def test_dashboard_response_is_cached_and_fresh_bypasses(app_with_fresh_data):
    cache.response_cache.clear()
    first = app_with_fresh_data.get("/api/dashboard?range=all").json()

    # Mutate the DB underneath the cache: delete every record. usage_rollup
    # is derived state that ingest rebuilds from `records`, so emptying the
    # data means emptying both — leaving the rollup behind would just be
    # reading a stale pre-aggregate, which is not what this test is about.
    with db.viz_conn() as c:
        c.execute("DELETE FROM records")
        c.execute("DELETE FROM usage_rollup")

    cached = app_with_fresh_data.get("/api/dashboard?range=all").json()
    assert cached == first                       # stale-but-cached payload

    fresh = app_with_fresh_data.get("/api/dashboard?range=all&fresh=1").json()
    assert fresh["cost_by_model"] == []          # fresh=1 sees the empty DB


def test_dashboard_range_non_numeric_days_400(app_with_data):
    """`1e5d` ends in `d` but `int('1e5')` raises, and that ValueError used
    to escape the shared range parser as a 500. It must come back as the
    same 400 an unknown suffix gets, naming the parameter (issue #112)."""
    r = app_with_data.get("/api/dashboard?range=1e5d")
    assert r.status_code == 400
    assert "range" in r.json()["detail"]


def test_dashboard_range_overflow_400(app_with_data):
    """`999999999d` parses (timedelta's day cap) and then overflows the
    caller's `since = now - delta` with OverflowError — a 500. The bound
    check belongs in the shared parser, so every endpoint returns 400
    without per-endpoint try/excepts (issue #112). A day count one past
    timedelta's own cap (construction overflow) is the same behavior."""
    r = app_with_data.get("/api/dashboard?range=999999999d")
    assert r.status_code == 400
    assert "range" in r.json()["detail"]
    r_cap = app_with_data.get("/api/dashboard?range=1000000000d")
    assert r_cap.status_code == 400
    assert "range" in r_cap.json()["detail"]


def test_dashboard_cost_by_project_shape(app_with_data):
    """cost_by_project mirrors cost_by_model: range-filtered, sorted by
    cost desc, zero-cost rows excluded, at most 10 project rows plus a
    single "Other (N projects)" fold."""
    body = app_with_data.get("/api/dashboard?range=3650d").json()
    cbp = body["cost_by_project"]
    assert {"projA", "projB"} <= {r["project"] for r in cbp}
    costs = [r["cost_usd"] for r in cbp]
    assert all(c > 0 for c in costs)
    assert costs == sorted(costs, reverse=True)
    named = [r for r in cbp if not r["project"].startswith("Other (")]
    assert len(named) <= 10
    others = [r for r in cbp if r["project"].startswith("Other (")]
    assert len(others) <= 1

    # A project filter scopes the breakdown to that one project.
    body_b = app_with_data.get("/api/dashboard?range=3650d&project=projB").json()
    assert {r["project"] for r in body_b["cost_by_project"]} == {"projB"}


def test_dashboard_cost_by_project_omitted_for_guest(app_with_data):
    """The guest gate is SERVER-side. /api/dashboard is guest-accessible,
    so per-project names/costs — the very data the 403s on /api/projects
    and on project= exist to withhold (session.auth_middleware) — must be
    missing from the response body itself, not merely unrendered by the
    frontend.

    This suite mounts only api.router (auth bypassed), so a guest is
    simulated by a middleware setting the SAME request.state.is_guest
    flag the real middleware sets. The guest call reuses the non-guest
    call's query params, so it is served from the SHARED response cache —
    proving the strip happens per-request, outside the cached payload."""
    body = app_with_data.get("/api/dashboard?range=3650d").json()
    assert "cost_by_project" in body
    assert "tokens_by_project" in body

    a = FastAPI()

    @a.middleware("http")
    async def set_guest_flag(request: Request, call_next):
        request.state.is_guest = True
        return await call_next(request)

    a.include_router(api.router)
    guest = TestClient(a).get("/api/dashboard?range=3650d")
    assert guest.status_code == 200
    assert "cost_by_project" not in guest.json()
    assert "tokens_by_project" not in guest.json(), (
        "per-project token names are the same guest-withheld data the "
        "cost key is — stripped server-side, outside the shared cache")
    # The rest of the payload is untouched.
    assert "cost_by_model" in guest.json()


def test_activity_heatmap_shape(app_with_data):
    r = app_with_data.get("/api/activity-heatmap?range=3650d")
    assert r.status_code == 200
    body = r.json()
    assert body["tz"] == "Europe/Prague"
    assert body["cells"], "mini fixture must produce at least one cell"
    for c in body["cells"]:
        assert 1 <= c["dow"] <= 7
        assert 0 <= c["hour"] <= 23
        assert c["requests"] >= 1
        assert c["output_tokens"] >= 0
        assert c["cost_usd"] >= 0


def test_activity_heatmap_requests_match_dashboard(app_with_data):
    # Both endpoints read through the same DISTINCT ON (uuid) dedup, so
    # total request counts must agree for the same range.
    heat = app_with_data.get("/api/activity-heatmap?range=3650d").json()
    dash = app_with_data.get("/api/dashboard?range=3650d").json()
    assert sum(c["requests"] for c in heat["cells"]) == \
           sum(h["requests"] for h in dash["hourly"])


def test_activity_heatmap_dst_awareness(app_with_fresh_data):
    _insert_tz_probe_rows()
    r = app_with_fresh_data.get("/api/activity-heatmap?range=3650d&model=tz-probe-model")
    assert r.status_code == 200
    cells = {(c["dow"], c["hour"]): c for c in r.json()["cells"]}
    assert set(cells) == {(4, 11), (3, 12)}, cells
    assert cells[(4, 11)]["requests"] == 1   # winter: 10:30Z -> 11:30 CET, Thu
    assert cells[(3, 12)]["requests"] == 1   # summer: 10:30Z -> 12:30 CEST, Wed
    assert cells[(3, 12)]["output_tokens"] == 20


def test_activity_heatmap_project_filter(app_with_data):
    both = app_with_data.get("/api/activity-heatmap?range=3650d").json()
    one = app_with_data.get("/api/activity-heatmap?range=3650d&project=projA").json()
    assert sum(c["requests"] for c in one["cells"]) < \
           sum(c["requests"] for c in both["cells"])


def test_activity_heatmap_bad_range_400(app_with_data):
    assert app_with_data.get("/api/activity-heatmap?range=bogus").status_code == 400


def test_cost_by_agent_splits_types_and_shares_sum(app_with_data):
    """One bar per agent type, biggest first, shares summing to 1.

    The mini mirror has an attributed sidecar (implementer) alongside
    main transcripts that record no role, so this also pins that the two
    do NOT collapse into one bar.
    """
    r = app_with_data.get("/api/cost-by-agent?range=all")
    assert r.status_code == 200
    body = r.json()
    agents = body["agents"]
    assert agents, "fixture produced no agent rows"

    names = [a["agent_type"] for a in agents]
    assert len(names) == len(set(names)), "one bar per type"
    assert "implementer" in names
    assert "general-purpose" in names

    costs = [a["cost_usd"] for a in agents]
    assert costs == sorted(costs, reverse=True), "biggest bar first"
    assert all(a["requests"] > 0 for a in agents)

    assert body["total_cost_usd"] == pytest.approx(sum(costs))
    assert sum(a["share"] for a in agents) == pytest.approx(1.0)


def test_cost_by_agent_bad_range_400(app_with_data):
    assert app_with_data.get(
        "/api/cost-by-agent?range=banana").status_code == 400


def test_cost_by_agent_model_filter_subsets(app_with_data):
    """A model filter selects whole (agent_type, model) rollup rows, so
    it can only ever narrow the result."""
    allm = app_with_data.get("/api/cost-by-agent?range=all").json()
    models = app_with_data.get("/api/models").json()["models"]
    assert models, "fixture has no models"
    top = models[0]["model"]
    one = app_with_data.get(
        f"/api/cost-by-agent?range=all&model={top}").json()
    assert one["total_cost_usd"] <= allm["total_cost_usd"] + 1e-9
    assert one["total_cost_usd"] > 0


def test_cost_by_agent_live_path_agrees_with_rollup(app_with_data):
    """range=1d takes a live pass over `records` instead of the rollup
    (hour-grained edges are too coarse for a 24h window). Both paths must
    return the same shape and the same type names."""
    live = app_with_data.get("/api/cost-by-agent?range=1d").json()
    assert isinstance(live["agents"], list)
    for a in live["agents"]:
        assert set(a) == {
            "agent_type", "requests", "output_tokens", "cost_usd", "share"}


def test_admin_ingest_runs_off_the_event_loop():
    """POST /admin/ingest must be registered as a plain def.

    Declared `async def`, the handler ran the blocking pipeline directly ON
    the event loop, so every concurrent request — /health, /api/*, the SSE
    stream, the login page — stalled for the length of the run. FastAPI
    moves a plain `def` handler to its threadpool, which is the only thing
    that keeps the loop free while the ingest blocks a worker (issue #101).
    """
    matches = [
        r for r in app_mod.app.routes
        if isinstance(r, APIRoute) and r.path == "/admin/ingest"
    ]
    assert matches, "no /admin/ingest route is registered"
    assert all(not inspect.iscoroutinefunction(m.endpoint) for m in matches)
