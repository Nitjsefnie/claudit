"""Effective startup policy at HTTP, payload, bootstrap and warm boundaries."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone

from orjson import dumps
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from backend import api, api_dashboard, api_export, cache, flags, ingest_warm, session
from backend.app import root_index
from backend.feature_payloads import features_script, project_dashboard
from backend.flags import Config
from backend.flags import resolve_config
from tests.test_api import _app_with_rl_data_fixture

__all__ = ["_app_with_rl_data_fixture"]

DASHBOARD_CONSUMERS = (
    "usage_timeseries", "thinking_output", "line_churn", "model_comparison",
    "token_breakdown", "project_comparison", "cache_ttl", "response_sizes",
    "burn_rate", "summary", "sessions",
)
MODEL_CONSUMERS = (
    "model_comparison", "token_breakdown", "context_growth", "context_comparison",
    "tool_usage", "reply_latency", "page_performance", "tool_error_rate",
    "agent_comparison", "activity_heatmap", "inspector",
)
HOURLY_CONSUMERS = (
    "usage_timeseries", "thinking_output", "line_churn", "model_comparison",
    "token_breakdown", "cache_ttl", "burn_rate", "summary",
)
ENDPOINTS = (
    ("/tool-usage", "tool_usage"), ("/tool-error-rate", "tool_error_rate"),
    ("/reply-latency", "reply_latency"), ("/activity-heatmap", "activity_heatmap"),
    ("/cost-by-context", "context_comparison"), ("/cost-by-agent", "agent_comparison"),
    ("/web-metrics", "page_performance"), ("/context-growth/traces", "context_growth"),
    ("/context-growth/agg", "cache"), ("/context-growth/session/sess-A", "inspector"),
    ("/cache", "cache"), ("/sessions", "sessions"),
    ("/sessions/sess-A", "inspector"), ("/sessions/sess-A/transcript", "inspector"),
    ("/sessions/sess-A/sidecar", "inspector"), ("/projects", "project_picker"),
    ("/export", "export_png"),
)


def rich_rows() -> dict:
    hour = datetime(2025, 1, 2, 3, tzinfo=timezone.utc)
    return {
        "hourly": [(hour, "sample-model", False, "sample-provider",
                    11, 22, 33, 44, 55, 6, 1.25, 2, 1)],
        "response_sizes": [(hour, "sample-model", 2, 100, 200)],
        "churn": [(hour, 7, 8)], "total_sessions_row": (1,),
        "cost_by_project": [("sample-project", 1.25)],
        "tokens_by_project": [("sample-project", 165)],
        "file_counts_row": (1, 2, 3, 4, 5, 6),
        "sessions": [("sample-session", 100, 200, 2, 11, 22, 77, 55,
                      1.25, "sample-model", ["sample-model"])],
        "ctx_end": {"sample-session": 123},
        "rate_limits": [("sample-session", {"ts": "2025-01-02T03:00:00Z",
                                            "content": "limit reached"})],
    }


EXPECTED_DEFAULT = b'{"range":"all","project":null,"bucket_s":3600,"hourly":[{"hour":"2025-01-02T03:00:00+00:00","model":"sample-model","long_context":false,"provider":"sample-provider","input_tokens":11,"output_tokens":22,"cache_5m_tokens":33,"cache_1h_tokens":44,"cache_read_tokens":55,"thinking_tokens":6,"cost_usd":1.25,"requests":2,"session_count":1,"lines_added":7,"lines_deleted":8}],"token_types":["input_tokens","output_tokens","thinking_tokens","cache_5m_tokens","cache_1h_tokens","cache_read_tokens"],"cost_by_model":[{"model":"sample-model","cost_usd":1.25}],"cost_by_model_provider":[{"model":"sample-model","provider":"sample-provider","cost_usd":1.25,"total_tokens":165}],"cost_by_project":[{"project":"sample-project","cost_usd":1.25}],"tokens_by_project":[{"project":"sample-project","total_tokens":165}],"rate_limit_hits":[{"session_id":"sample-session","ts":"2025-01-02T03:00:00Z","content":"limit reached"}],"hide_rate_limits":false,"sessions":[{"session_id":"sample-session","start_ts":100.0,"end_ts":200.0,"requests":2,"input_tokens":11,"output_tokens":22,"cache_create_tokens":77,"cache_read_tokens":55,"cost_usd":1.25,"model":"sample-model","models_used":["sample-model"],"ctx_at_end":123}],"total_sessions":1,"main_w_usage":1,"main_empty":2,"subagent_files":3,"subagent_only_sessions":4,"total_prompts":5,"total_turns":6,"response_sizes":[{"ts":"2025-01-02T03:00:00+00:00","model":"sample-model","n":2,"p50":100.0,"p90":200.0}]}'


@pytest.fixture(name="policy")
def policy_fixture(monkeypatch):
    original = flags.CONFIG

    def apply(*disabled: str, env: dict | None = None):
        config = resolve_config(env if env is not None else {
            f"CLAUDIT_FEATURE_{key.upper()}": "0" for key in disabled
        })
        monkeypatch.setattr(flags, "CONFIG", config)
        cache.response_cache.clear()
        return config

    apply()
    yield apply
    monkeypatch.setattr(flags, "CONFIG", original)
    cache.response_cache.clear()


def build() -> dict:
    return api_dashboard._dashboard_build(  # pylint: disable=protected-access
        rich_rows(), "all", None, 3600)


def test_default_dashboard_bytes_and_shape(policy, app_with_rl_data):
    baseline = build()
    assert dumps(baseline) == EXPECTED_DEFAULT
    assert baseline["hourly"] and baseline["sessions"] and baseline["response_sizes"]
    policy(env={f"CLAUDIT_FEATURE_{key.upper()}": "1"
                for key in flags.CONFIG.features})
    assert dumps(build()) == EXPECTED_DEFAULT
    client, in_range, _ = app_with_rl_data
    body = client.get("/api/dashboard?range=3650d&fresh=1").json()
    assert in_range in [hit["ts"] for hit in body["rate_limit_hits"]]
    assert body["hourly"] and body["sessions"] and body["response_sizes"]


@pytest.mark.parametrize(("path", "feature"), ENDPOINTS)
def test_disabled_endpoint_returns_404_before_work(
    path, feature, policy, monkeypatch, app_with_rl_data,
):
    calls = []
    client, _, _ = app_with_rl_data
    original = api.db.viz_conn

    @contextmanager
    def connection():
        calls.append("db")
        with original() as conn:
            yield conn

    async def render(argv, out_path):
        calls.append("render")
        with open(out_path, "wb") as output:
            output.write(b"png")

    monkeypatch.setattr(api.db, "viz_conn", connection)
    monkeypatch.setattr(api_export, "_render_export", render)
    url = "/api" + path + "?range=all&path=sess-A.jsonl"
    # The operator-only endpoint has its permission tested separately.
    if feature == "page_performance":
        @client.app.middleware("http")
        async def operator(request, call_next):
            request.state.is_operator = True
            return await call_next(request)

    response = client.get(url)
    assert response.status_code == 200
    assert calls
    calls.clear()
    policy(feature)

    def cache_forbidden(*args, **kwargs):
        pytest.fail("disabled endpoint reached response cache")

    monkeypatch.setattr(cache.response_cache, "get_entry", cache_forbidden)
    assert client.get(url).status_code == 404
    assert not calls


@pytest.mark.parametrize(("feature", "keys"), [
    ("model_comparison", ("cost_by_model", "cost_by_model_provider")),
    ("project_comparison", ("cost_by_project", "tokens_by_project")),
    ("response_sizes", ("response_sizes",)),
    ("rate_limits", ("rate_limit_hits",)),
    ("summary", ("total_sessions", "main_w_usage", "main_empty", "subagent_files",
                 "subagent_only_sessions", "total_prompts", "total_turns")),
    ("thinking_output", ("thinking_tokens",)),
    ("line_churn", ("lines_added", "lines_deleted")),
])
def test_shared_dashboard_fields_follow_consumer_union(feature, keys, policy):
    on = build()
    hourly = feature in ('thinking_output', 'line_churn')
    if hourly:
        assert all(on['hourly'][0][key] > 0 for key in keys)
    else:
        assert all(on[key] for key in keys)
    if feature == 'thinking_output':
        assert 'thinking_tokens' in on['token_types']
    policy(feature)
    off = build()
    assert all(key not in (off['hourly'][0] if hourly else off) for key in keys)
    assert off['hourly'] and off['sessions']
    assert off['hide_rate_limits'] is (feature == 'rate_limits')
    if hourly:
        assert off['hourly'][0]['input_tokens'] > 0
    if feature == 'thinking_output':
        assert 'thinking_tokens' not in off['token_types']


def test_sessions_and_burn_union(policy):
    on = build()
    assert on["sessions"][0]["ctx_at_end"] == 123
    assert on["sessions"][0]["models_used"]
    policy("burn_rate", "context_growth")
    sessions = build()["sessions"]
    assert "ctx_at_end" not in sessions[0]
    assert "models_used" not in sessions[0]
    for key in ("session_id", "input_tokens", "cost_usd", "requests"):
        assert sessions[0][key] == on["sessions"][0][key]
    policy("sessions")
    assert build()["sessions"] == on["sessions"]
    policy("sessions", "burn_rate")
    assert "sessions" not in build()


def test_all_hourly_consumers_off_preserves_standalone_data(policy):
    on = build()
    assert on["hourly"] and on["token_types"]
    policy(*HOURLY_CONSUMERS)
    off = build()
    assert "hourly" not in off and "token_types" not in off
    for key in ("response_sizes", "cost_by_project", "tokens_by_project"):
        assert off[key] == on[key] and off[key]
    assert off["sessions"][0] == {key: value for key, value in on["sessions"][0].items()
                                  if key != "ctx_at_end"}


@pytest.mark.parametrize("query", ["project=projA", "project="])
def test_project_picker_off_refuses_filters_and_keeps_comparison(
    query, policy, app_with_rl_data, monkeypatch,
):
    client, _, _ = app_with_rl_data
    assert client.get("/api/projects?range=all").json()["projects"]
    assert client.get("/api/dashboard?range=all&" + query).status_code == 200
    comparisons = client.get("/api/dashboard?range=all").json()
    assert comparisons["cost_by_project"] and comparisons["tokens_by_project"]
    policy("project_picker")
    off = client.get("/api/dashboard?range=all").json()
    assert off["cost_by_project"] == comparisons["cost_by_project"]
    assert off["tokens_by_project"] == comparisons["tokens_by_project"]

    def forbidden():
        pytest.fail("disabled project request reached database")

    monkeypatch.setattr(api.db, "viz_conn", forbidden)
    assert client.get("/api/projects?range=all").status_code == 404
    assert client.get("/api/dashboard?range=all&" + query).status_code == 404


def test_all_dashboard_consumers_off_does_not_disable_independent_panel_routes(
    policy, app_with_rl_data,
):
    client, _, _ = app_with_rl_data
    assert client.get("/api/dashboard?range=all").json()["hourly"]
    assert client.get("/api/tool-usage?range=all").json()["buckets"]
    assert client.get("/api/models").json()["models"]
    policy(*DASHBOARD_CONSUMERS)
    assert client.get("/api/dashboard?range=all").status_code == 404
    assert client.get("/api/tool-usage?range=all").json()["buckets"]
    assert client.get("/api/models").json()["models"]
    policy(*MODEL_CONSUMERS)
    assert client.get("/api/models").status_code == 404


def test_warming_skips_disabled_targets(policy, monkeypatch):
    calls = []
    monkeypatch.setenv("CLAUDIT_WARM_CACHE", "1")
    monkeypatch.setattr(cache, "warm", lambda fn, **kw: calls.append((fn, kw)))
    ingest_warm.warm_common()
    on = list(calls)
    assert len(on) == 1 + 8 * len(ingest_warm.WARM_RANGES)
    assert on[0] == (api.list_models, {})
    assert (api.tool_usage, {"rng": "all"}) in on
    calls.clear()
    policy("tool_usage", "context_growth", "project_picker")
    ingest_warm.warm_common()
    assert calls == [(fn, kw) for fn, kw in on if fn not in (
        api.tool_usage, ingest_warm.context_growth_traces, api.list_projects)]
    calls.clear()
    policy(*DASHBOARD_CONSUMERS, *MODEL_CONSUMERS)
    ingest_warm.warm_common()
    assert not any(fn in (api.list_models, api_dashboard.dashboard) for fn, _ in calls)
    calls.clear()
    monkeypatch.setenv("CLAUDIT_WARM_CACHE", "0")
    ingest_warm.warm_common()
    assert not calls


def test_filtered_cache_and_guest_copy_are_isolated(policy, app_with_rl_data):
    client, in_range, _ = app_with_rl_data
    on = client.get("/api/dashboard?range=all").json()
    assert in_range in [h["ts"] for h in on["rate_limit_hits"]]
    assert on["cost_by_project"] and on["tokens_by_project"]
    policy("rate_limits")
    cold = client.get("/api/dashboard?range=all").json()
    warm = client.get("/api/dashboard?range=all").json()
    assert "rate_limit_hits" not in cold and warm == cold
    policy()
    assert client.get("/api/dashboard?range=all").json()["rate_limit_hits"]
    guest = Request({"type": "http", "state": {"is_guest": True}})
    body = api_dashboard.dashboard_route(guest, rng="all", project=None, model=None, fresh=0)
    assert "cost_by_project" not in body and "tokens_by_project" not in body
    assert client.get("/api/dashboard?range=all").json()["cost_by_project"]
    before = deepcopy(on)
    projected = project_dashboard(on, resolve_config({"CLAUDIT_FEATURE_BURN_RATE": "0"}))
    projected["sessions"][0]["models_used"].clear()
    projected["hourly"][0]["input_tokens"] = -1
    assert on == before
    assert project_dashboard(on, resolve_config({})) is on


def test_features_bootstrap_only_for_nondefault_config(policy):
    request = Request({"type": "http", "state": {
        "is_guest": True, "is_operator": True, "csp_nonce": "sample-nonce",
    }})
    on = bytes(asyncio.run(root_index(request)).body).decode()
    assert "window.FEATURES =" not in on
    policy("rate_limits")
    off = bytes(asyncio.run(root_index(request)).body).decode()
    assert '"rate_limits": false' in off
    assert off.index("window.FEATURES =") < off.index('src="/src/feature-flags.js')
    for html in (on, off):
        assert "window.IS_GUEST = true" in html and "window.IS_OPERATOR = true" in html
        assert "window.BRAND =" in html and 'nonce="sample-nonce"' in html
        assert '/src/feature-flags.js?v=' in html


def test_per_session_context_omits_extras_but_keeps_trace(policy, app_with_rl_data):
    client, _, _ = app_with_rl_data
    on = client.get("/api/context-growth/session/sess-A").json()
    assert on["total_turns"] and on["turns"] and on["file_key"]
    policy(env={"CLAUDIT_COLLECT_FILE_EXTRAS": "0"})
    off = client.get("/api/context-growth/session/sess-A").json()
    assert "total_turns" not in off
    assert off == {k: v for k, v in on.items() if k != "total_turns"}


@pytest.mark.asyncio
async def test_disabled_sse_returns_404_before_subscription(policy, monkeypatch):
    """Run the assembled ASGI path with a bounded client disconnect."""
    subscriptions = []
    unsubscribed = []

    def subscribe():
        queue = asyncio.Queue()
        subscriptions.append(queue)
        return queue

    monkeypatch.setattr(api.events, "subscribe", subscribe)
    monkeypatch.setattr(api.events, "unsubscribe", unsubscribed.append)
    app = FastAPI()
    app.include_router(api.router)

    async def request():
        connected = asyncio.Event()
        messages = []

        async def receive():
            await connected.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)
            if message["type"] == "http.response.body":
                connected.set()

        scope = {"type": "http", "method": "GET", "path": "/api/events",
                 "query_string": b"", "headers": [], "scheme": "http",
                 "server": ("localhost", 80), "http_version": "1.1",
                 "asgi": {"spec_version": "2.0"}}
        await asyncio.wait_for(app(scope, receive, send), timeout=2)
        return messages

    on = await request()
    assert on[0]["status"] == 200
    assert any(b": connected" in msg.get("body", b"") for msg in on)
    assert len(subscriptions) == 1 and unsubscribed == subscriptions
    subscriptions.clear()
    unsubscribed.clear()
    policy("live_updates")
    off = await request()
    assert off[0]["status"] == 404
    assert not subscriptions and not unsubscribed


def test_permissions_still_intersect_with_feature_gates(policy, app_with_rl_data, monkeypatch):
    client, _, _ = app_with_rl_data
    app = FastAPI()
    app.include_router(api.router)
    app.middleware("http")(session.auth_middleware)
    authenticated = TestClient(app)
    monkeypatch.setattr(session, "resolve_session_user_id", lambda cookie: None)
    assert authenticated.get("/api/dashboard").status_code == 401
    monkeypatch.setattr(session, "resolve_session_user_id", lambda cookie: session.GUEST_USER_ID)
    monkeypatch.setattr(session, "is_operator", lambda user_id: False)
    authenticated.cookies.set(session.SESSION_COOKIE_NAME, "sample-session-cookie")
    assert authenticated.get("/api/dashboard?range=all").json()["hourly"]
    assert client.get("/api/projects?range=all").json()["projects"]
    assert authenticated.get("/api/projects?range=all").status_code == 403
    assert authenticated.get("/api/web-metrics?range=all").status_code == 403
    policy("usage_timeseries")
    assert authenticated.get("/api/dashboard?range=all").status_code == 200
    assert authenticated.get("/api/projects?range=all").status_code == 403
    policy("page_performance")
    assert authenticated.get("/api/web-metrics?range=all").status_code == 404


def test_bootstrap_escapes_effective_object(policy):
    assert features_script(flags.CONFIG) == ""
    config = Config(flags.CONFIG.collection, {"</script><!--\u2028\u2029": False}, False)
    script = features_script(config)
    assert script.startswith("window.FEATURES = ")
    assert "</script>" not in script and "<!--" not in script
    assert "\u2028" not in script and "\u2029" not in script
    assert "false" in script


@pytest.mark.parametrize(("consumer", "fields"), [
    ("usage_timeseries", ("input_tokens", "output_tokens", "cost_usd")),
    ("model_comparison", ("input_tokens", "output_tokens", "cost_usd")),
    ("token_breakdown", ("input_tokens", "output_tokens", "provider", "long_context")),
    ("summary", ("input_tokens", "output_tokens", "cost_usd", "requests", "session_count")),
    ("burn_rate", ("input_tokens", "output_tokens", "cache_read_tokens")),
    ("cache_ttl", ("cache_5m_tokens", "cache_1h_tokens")),
    ("thinking_output", ("thinking_tokens",)),
    ("line_churn", ("lines_added", "lines_deleted")),
])
def test_hourly_leaves_survive_each_sole_consumer(consumer, fields, policy):
    on = build()
    assert all(key in on["hourly"][0] for key in fields)
    policy(*(key for key in HOURLY_CONSUMERS if key != consumer))
    off = build()
    assert all(key in off["hourly"][0] for key in fields)
    assert off["hourly"][0]["hour"] and off["hourly"][0]["model"]
    for key in ("thinking_tokens", "lines_added", "lines_deleted", "requests", "session_count"):
        if key not in fields:
            assert key not in off["hourly"][0]
    policy(*HOURLY_CONSUMERS)
    assert "hourly" not in build()


def test_queries_skip_exclusive_disabled_work(policy, app_with_rl_data, monkeypatch):
    client, _, _ = app_with_rl_data
    executed = []
    original = api_dashboard.Phases.execute

    def execute(self, name, conn, sql, args=None):
        executed.append(name)
        return original(self, name, conn, sql, args)

    monkeypatch.setattr(api_dashboard.Phases, "execute", execute)
    on = client.get("/api/dashboard?range=all").json()
    assert on["rate_limit_hits"] and on["response_sizes"] and on["sessions"]
    assert {"hourly", "churn", "total_sessions", "file_counts", "response_sizes",
            "rate_limit_hits", "cost_by_project", "tokens_by_project", "sessions"} <= set(executed)
    executed.clear()
    policy(*HOURLY_CONSUMERS, "response_sizes", "sessions", "rate_limits")
    off = client.get("/api/dashboard?range=all").json()
    assert off["cost_by_project"] and off["tokens_by_project"]
    assert set(executed) == {"cost_by_project", "tokens_by_project"}
