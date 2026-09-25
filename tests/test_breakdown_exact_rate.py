"""The Token Breakdown prices every row at the rate its cost_usd was stored at.

The dashboard's events carry a short display name ('opus-4-8'), which
resolveModelRate matches only as a family tier — the family's NEWEST
rate — so pricing by it put claude-opus-4-8 at Opus 5.5 rates and the
per-type cost bars stopped summing to the stored total beside them
(issue #70, SV-DATED-RATES). These drive the real adapter and breakdown
out of src/app.jsx through node (which cannot parse the file's JSX, so
the plain functions are sliced out of it) against backend.pricing and,
end to end, against /api/dashboard over an ingested fixture.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import api, db, ingest, pricing
from tests import scratch_db

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available"
)


def _slice(path: str, start: str, end: str) -> str:
    src = (ROOT / path).read_text(encoding="utf-8")
    i = src.index(start)
    return src[i:src.index(end, i)]


def _harness() -> str:
    """parser.js plus the plain (JSX-free) functions the breakdown path
    runs, evaluated in one node global scope."""
    parts = [
        _slice("src/dashboard-charts-extra.jsx", "function shortModelName",
               "\nfunction ContextGrowthPanel"),
        _slice("src/app.jsx", "function modelProviderLabel",
               "\nfunction App()"),
        _slice("src/app.jsx", "function backendAggregateRange",
               "\n// Which of the four token panels"),
        _slice("src/app.jsx", "function computeTokenBreakdown",
               "\n// Paired token/cost breakdown bars"),
    ]
    return f"""
      global.window = {{}};
      require({str(ROOT / 'src' / 'parser-lanes.js')!r});
      require({str(ROOT / 'src' / 'parser.js')!r});
      window.dashboardCol = {{}};
      eval({json.dumps(chr(10).join(parts))});
      window.shortModelName = shortModelName;
    """


def _node(body: str) -> dict:
    proc = subprocess.run(
        ["node", "-e", _harness() + body], cwd=ROOT, capture_output=True,
        text=True, timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _breakdown_of_dashboard(body: dict) -> dict:
    return _node(f"""
      const shaped = backendDashToShape({json.dumps(body)});
      const bd = computeTokenBreakdown(shaped.events);
      console.log(JSON.stringify({{
        costTotal: bd.costTotal,
        models: shaped.events.map(e => e.model),
      }}));
    """)


_TS = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-sonnet-4-5"])
def test_breakdown_prices_an_older_claude_model_at_its_own_rate(model):
    """Issue #70's cases, now pinning that each model is priced by its own
    EXACT row rather than the default row: a producer dropping model_id
    must fail here, which the old opus-4-8 case could never show — its
    exact row equals the default row, while opus-5-5's differs from it and
    sonnet-4-5's differs from both the default and the tier its short name
    resolves to."""
    live = _node(f"""
      const r = window.resolveModelRate({json.dumps(model)});
      const d = window.resolveModelRate(undefined);
      console.log(JSON.stringify({{kind: r.kind, exact: r.rates, dflt: d.rates}}));
    """)
    assert live["kind"] == "exact", (
        f"fixture live only while {model} resolves to an exact row, "
        f"got kind {live['kind']}")
    assert live["exact"] != live["dflt"], (
        f"fixture live only while {model}'s exact row differs from the "
        "default row")
    fresh, out, eph5, eph1h, read = 12_000, 3_000, 4_000, 20_000, 500_000
    stored = pricing.compute_cost(
        model, fresh=fresh, output=out, eph5=eph5, eph1h=eph1h,
        unsplit_create=0, read=read, ts=_TS)
    got = _breakdown_of_dashboard({"bucket_s": 3600, "sessions": [], "hourly": [{
        "hour": _TS.isoformat(), "model": model, "provider": None,
        "input_tokens": fresh, "output_tokens": out,
        "cache_5m_tokens": eph5, "cache_1h_tokens": eph1h,
        "cache_read_tokens": read, "cost_usd": stored,
    }]})
    assert got["costTotal"] == pytest.approx(stored, rel=1e-12)
    # The display name is unchanged: labels still read 'opus-4-8'.
    assert got["models"] == [model.removeprefix("claude-")]


def test_breakdown_prices_an_event_without_model_id_by_its_display_name():
    """The missing-model_id rule: an event without its raw id is priced by
    the name it still carries — a Claude short name resolves to its family
    tier, an estimate; an event without either lands on the default row."""
    got = _node("""
      const mk = (model) => ({ model, model_id: undefined, provider: null,
        ts: Date.parse('2026-09-20T12:00:00Z'), long_context: false,
        input_tokens: 1000, output_tokens: 700, cache_create: 15000,
        cache_read: 90000, ephemeral_5m: 1000, ephemeral_1h: 4000 });
      const events = [mk('opus-4-8'), mk(undefined)];
      const cost = (rates, e) => (e.input_tokens * rates.fresh
        + e.output_tokens * rates.out + e.ephemeral_5m * rates.c5
        + e.ephemeral_1h * rates.c1h
        + Math.max(0, e.cache_create - e.ephemeral_5m - e.ephemeral_1h)
          * rates.c1h
        + e.cache_read * rates.read) / 1e6;
      const tier = window.resolveModelRate('opus-4-8').rates;
      const dflt = window.resolveModelRate(undefined).rates;
      const bd = computeTokenBreakdown(events);
      console.log(JSON.stringify({
        costTotal: bd.costTotal,
        expected: cost(tier, events[0]) + cost(dflt, events[1]),
        allDefault: cost(dflt, events[0]) + cost(dflt, events[1]),
        tier: tier, dflt: dflt,
      }));
    """)
    # Liveness: the fixture means something only while the tier the
    # display name resolves to differs from the default row.
    assert got["tier"] != got["dflt"], (
        "fixture live only while opus-4-8's family tier differs from the "
        "default row")
    assert got["costTotal"] == pytest.approx(got["expected"], rel=1e-12)
    # Not the all-default value: pricing both events there is exactly the
    # pre-fix behaviour this rule replaces.
    assert got["costTotal"] != pytest.approx(got["allDefault"], rel=1e-9)


def test_transcript_breakdown_prices_at_the_rate_its_events_were_costed():
    """The Inspector-loaded path (txToDashData) costs each turn by the
    raw model id; its breakdown must price the same id."""
    got = _node("""
      const usage = (n) => ({ input_tokens: 1000 * n, output_tokens: 700 * n,
        cache_creation_input_tokens: 5000 * n, cache_read_input_tokens: 90000 * n,
        cache_creation: { ephemeral_5m_input_tokens: 1000 * n,
                          ephemeral_1h_input_tokens: 4000 * n } });
      const meta = [
        { type: 'assistant_usage', sessionId: 's', line: 1,
          ts: '2026-09-20T12:00:00Z', model: 'claude-opus-4-8', usage: usage(1) },
        { type: 'assistant_usage', sessionId: 's', line: 2,
          ts: '2026-09-20T12:01:00Z', model: 'claude-sonnet-4-5', usage: usage(2) },
      ];
      const dash = txToDashData({ meta, events: [] });
      const bd = computeTokenBreakdown(dash.events);
      console.log(JSON.stringify({
        costTotal: bd.costTotal,
        eventCost: dash.events.reduce((s, e) => s + e.cost_usd, 0),
        models: dash.events.map(e => e.model),
      }));
    """)
    assert got["eventCost"] > 0
    assert got["costTotal"] == pytest.approx(got["eventCost"], rel=1e-12)
    assert got["models"] == ["opus-4-8", "sonnet-4-5"]


def test_transcript_turns_price_at_the_rate_in_force_at_their_own_time():
    """Issue #55: txToDashData priced every turn with no timestamp, so a
    turn inside gpt-5-6-sol's GA window (it ends at pricing.AUG21_CUT,
    where the cut list price begins) costed at list instead. Each turn's
    cost_usd must equal what pricing.compute_cost stores for that turn's
    own timestamp, so the turn costs sum to the stored total."""
    model = "gpt-5-6-sol"
    cut = pricing.AUG21_CUT
    ts_in, ts_out = cut - timedelta(days=1), cut + timedelta(days=30)
    got = _node(f"""
      const usage = {{ input_tokens: 1000, output_tokens: 700,
        cache_creation_input_tokens: 5000, cache_read_input_tokens: 90000,
        cache_creation: {{ ephemeral_5m_input_tokens: 1000,
                          ephemeral_1h_input_tokens: 4000 }} }};
      const meta = [
        {{ type: 'assistant_usage', sessionId: 's', line: 1,
          ts: {json.dumps(ts_in.isoformat())}, model: {json.dumps(model)}, usage }},
        {{ type: 'assistant_usage', sessionId: 's', line: 2,
          ts: {json.dumps(ts_out.isoformat())}, model: {json.dumps(model)}, usage }},
      ];
      const dash = txToDashData({{ meta, events: [] }});
      console.log(JSON.stringify(dash.events.map(e => e.cost_usd)));
    """)
    assert len(got) == 2
    dated = pricing.compute_cost(
        model, fresh=1000, output=700, eph5=1000, eph1h=4000,
        unsplit_create=0, read=90000, ts=ts_in)
    listed = pricing.compute_cost(
        model, fresh=1000, output=700, eph5=1000, eph1h=4000,
        unsplit_create=0, read=90000, ts=ts_out)
    # The fixture means something only while the window reprices it.
    assert dated != listed
    assert got[0] == pytest.approx(dated, rel=1e-12)
    assert got[1] == pytest.approx(listed, rel=1e-12)


# (provider or None, model, fresh, 5m write, 1h write, cache read, output):
# two older Claude models whose short names resolve to a newer tier, a
# current one, an OpenRouter record priced by its serving host, and a
# dated permaslug priced only through that host (no provider row, no rate).
_RECORDS = [
    (None, "claude-opus-4-8", 1200, 3000, 15000, 400_000, 2500),
    (None, "claude-sonnet-4-5", 800, 0, 9000, 150_000, 1800),
    (None, "claude-opus-5-5", 500, 1000, 5000, 90_000, 900),
    ("DeepInfra", "z-ai/glm-5.3-flash", 7000, 0, 0, 3000, 1100),
    ("DeepInfra", "deepseek/deepseek-v4-flash-20260731", 7000, 0, 0, 3000,
     1100),
]


def _transcript(start: datetime) -> str:
    out = [{"type": "user", "timestamp": start.isoformat(), "uuid": "u0",
            "message": {"role": "user", "content": "hi"}}]
    for i, (provider, model, fresh, e5, e1h, read, output) in enumerate(
            _RECORDS, 1):
        msg = {"id": f"msg-{i}", "type": "message", "role": "assistant",
               "model": model, "content": [{"type": "text", "text": "ok"}],
               "stop_reason": "end_turn",
               "usage": {"input_tokens": fresh,
                         "cache_creation_input_tokens": e5 + e1h,
                         "cache_creation": {
                             "ephemeral_5m_input_tokens": e5,
                             "ephemeral_1h_input_tokens": e1h},
                         "cache_read_input_tokens": read,
                         "output_tokens": output}}
        if provider:
            msg["provider"] = provider
        out.append({"type": "assistant", "uuid": f"a{i}",
                    "requestId": f"req-{i}", "message": msg,
                    "timestamp": (start + timedelta(seconds=i)).isoformat()})
    return "\n".join(json.dumps(o) for o in out) + "\n"


@pytest.fixture(scope="module", name="dashboard_body")
def _dashboard_body_fixture():
    mp = pytest.MonkeyPatch()
    test_db = scratch_db.create_database("breakdown_rate")
    tmp = tempfile.mkdtemp(prefix="sv-breakdown-")
    start = (datetime.now(UTC) - timedelta(hours=2)).replace(
        minute=0, second=0, microsecond=0)
    sess = Path(tmp) / "r2" / "claude" / "projBD" / "sess-bd"
    sess.mkdir(parents=True)
    (sess / "sess-bd.jsonl").write_text(_transcript(start), encoding="utf-8")
    try:
        mp.setenv("DATABASE_URL_VIZ", f"postgresql:///{test_db}")
        mp.setenv("R2_ENDPOINT", f"file://{tmp}/r2/")
        db.reset_viz_pool()
        result = ingest.run_ingest(trigger="manual")
        assert result["error"] is None, result["error"]
        a = FastAPI()
        a.include_router(api.router)
        yield TestClient(a).get("/api/dashboard?range=3650d&fresh=1").json()
    finally:
        db.reset_viz_pool()
        shutil.rmtree(tmp)
        scratch_db.drop_database(test_db)
        mp.undo()


def test_breakdown_total_equals_the_stored_cost_for_mixed_models(
        dashboard_body):
    """Parity end to end: records ingested and priced by the backend,
    served by /api/dashboard, shaped and priced by the browser. The
    breakdown's bars must sum to the stored cost_usd of every row."""
    hourly = dashboard_body["hourly"]
    assert {(h["model"], h["provider"]) for h in hourly} == {
        (m, p) for p, m, *_ in _RECORDS}
    stored = sum(h["cost_usd"] for h in hourly)
    assert stored > 0
    # Liveness: the dated permaslug must travel the provider path — exact
    # row, folded key, nonzero rate — or the parity assertion below would
    # pass with both sides on the default row.
    hour = next(h["hour"] for h in hourly
                if h["model"] == "deepseek/deepseek-v4-flash-20260731")
    live = _node(
        "console.log(JSON.stringify(window.resolveModelRate("
        "'deepseek/deepseek-v4-flash-20260731', "
        f"Date.parse({json.dumps(hour)}), 'DeepInfra')))")
    assert live["kind"] == "exact", (
        "dated-record fixture live only while the permaslug folds to an "
        f"exact provider row, got kind {live['kind']}")
    assert live["key"] == "deepseek/deepseek-v4-flash-0731"
    assert live["rates"]["fresh"], (
        "fixture live only while the folded row's rates are nonzero")
    got = _breakdown_of_dashboard(dashboard_body)
    # The stored per-record cost is rounded to 6 places; the breakdown
    # prices the summed tokens unrounded.
    assert got["costTotal"] == pytest.approx(stored, abs=1e-5)


def test_synthetic_preview_events_carry_the_id_the_breakdown_prices():
    """The no-backend preview feeds the same breakdown, which prices
    model_id alone: each event's id must name its displayed model, and
    every Claude model must resolve to its own row rather than a tier."""
    got = _node(f"""
      require({str(ROOT / 'src' / 'synthetic-data.js')!r});
      const events = window.generateSyntheticData().events;
      const pairs = {{}};
      for (const e of events) pairs[e.model] = [e.model_id,
        e.model_id && window.resolveModelRate(e.model_id).kind];
      console.log(JSON.stringify(pairs));
    """)
    assert got, "the preview generated no events"
    for model, (model_id, kind) in got.items():
        assert model_id is not None, model
        assert _node(f"console.log(JSON.stringify(shortModelName("
                     f"{json.dumps(model_id)})))") == model
        if model != "<synthetic>":
            assert kind == "exact", (model, model_id)
