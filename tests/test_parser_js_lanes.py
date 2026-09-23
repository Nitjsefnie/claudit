"""The browser parses every transcript format the backend does.

Task 2 taught backend.parse.parse_file to sniff Codex and Kimi wires; the
Inspector still parsed only Claude transcripts in the browser. This drives
the REAL src/parser-lanes.js + src/parser.js pair through node — the same
way test_parser_js_mirror.py drives parser.js — and asserts the browser's
per-record token totals equal backend.parse.parse_file's, so the
Inspector's numbers are the stored numbers.

Sniff parity is asserted the same way: the browser's format sniff must
agree with parse.sniff_format on every lane fixture AND on the awkward
blobs test_parse_lanes.py pins (a Claude line with a string "message",
a kimi-code llm.error line whose "message" is a string, and the
unidentified catch-all).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

import pytest

from backend import parse, pricing

ROOT = Path(__file__).resolve().parents[1]
LANES_JS = ROOT / "src" / "parser-lanes.js"
PARSER_JS = ROOT / "src" / "parser.js"
CHARTS_JSX = ROOT / "src" / "dashboard-charts-extra.jsx"
FIX_PARSER = ROOT / "fixtures" / "parser"
FIX_CODEX = ROOT / "fixtures" / "codex"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available"
)

# Every Task 2 lane fixture: the three the sniff tests use plus the
# ported fixtures the backend Codex/Kimi parsers are tested against.
LANE_FIXTURES = [
    FIX_PARSER / "codex_min.jsonl",
    *sorted(FIX_CODEX.glob("*.jsonl")),
    *sorted(FIX_PARSER.glob("kimi_*.jsonl")),
]

# The awkward blobs from tests/test_parse_lanes.py, verbatim.
TRICKY_BLOBS = [
    b'{"sessionId":"x","type":"system","message":"hi"}\n',
    (b'{"type":"llm.error","time":1784213155000,'
     b'"kind":"quota_exhausted","message":"out of quota"}\n'),
    b"",
    b'{"unidentified": true}\n',
]

# Cumulative counters for the model-map-order blob: the second snapshot
# must advance every field or the differencing drops it as a duplicate.
_MODEL_MAP_USAGE = {
    "input_tokens": 20_000, "cached_input_tokens": 19_500,
    "cache_write_input_tokens": 0, "output_tokens": 500,
    "reasoning_output_tokens": 300, "total_tokens": 20_500,
}
_MODEL_MAP_USAGE2 = {
    "input_tokens": 21_000, "cached_input_tokens": 20_500,
    "cache_write_input_tokens": 0, "output_tokens": 700,
    "reasoning_output_tokens": 400, "total_tokens": 21_700,
}


@lru_cache(maxsize=1)
def _browser_lane_output() -> dict:
    """Parse every lane fixture in the browser, once, through node."""
    fixtures = {p.name: p.read_text(encoding="utf-8") for p in LANE_FIXTURES}
    script = f"""
      global.window = {{}};
      require({str(LANES_JS)!r});
      require({str(PARSER_JS)!r});
      const fixtures = {json.dumps(fixtures)};
      const out = {{}};
      for (const [name, text] of Object.entries(fixtures)) {{
        const {{ events, meta }} = window.parseTranscript(text);
        out[name] = {{
          records: meta
            .filter(m => m.type === 'assistant_usage')
            .map(m => ({{
              line: m.line,
              model: m.model,
              long_context: !!m.long_context,
              thinking_tokens: m.thinking_tokens || 0,
              fresh: m.usage.input_tokens || 0,
              create: m.usage.cache_creation_input_tokens || 0,
              read: m.usage.cache_read_input_tokens || 0,
              output: m.usage.output_tokens || 0,
              // One record through the real cost path — no formula
              // duplicated in this test.
              cost: window.computeSessionStats([], [m]).cost,
            }})),
          totals: (() => {{
            const s = window.computeSessionStats(events, meta);
            return {{ fresh: s.fresh, create: s.create, read: s.read,
                     output: s.output, cost: s.cost }};
          }})(),
        }};
      }}
      console.log(JSON.stringify(out));
    """
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=120,
        check=False,  # Return code checked by hand on the next line.
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _browser_records(name: str) -> list[dict]:
    return _browser_lane_output()[name]["records"]


def _browser_totals(name: str) -> dict:
    return _browser_lane_output()[name]["totals"]


def _backend_records(name: str) -> list[dict]:
    blob = (next(p for p in LANE_FIXTURES if p.name == name)).read_bytes()
    return parse.parse_file(f"sessions/p/s/{name}", blob)["records"]


# --------------------------------------------------------------------------
# Per-record parity: the browser's records are the backend's records
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", [p.name for p in LANE_FIXTURES])
def test_browser_parse_matches_backend_per_record(name):
    backend = _backend_records(name)
    browser = _browser_records(name)
    assert len(browser) == len(backend), "record count"
    for got, want in zip(browser, backend):
        label = f"{name} line {want['line_num']}"
        assert got["line"] == want["line_num"], label
        assert got["model"] == want["model"], f"{label}: model"
        assert got["fresh"] == want["fresh_tokens"], f"{label}: fresh"
        assert got["create"] == want["cache_creation_tokens"], f"{label}: create"
        assert got["read"] == want["cache_read_tokens"], f"{label}: read"
        assert got["output"] == want["output_tokens"], f"{label}: output"
        assert got["thinking_tokens"] == want["thinking_tokens"], (
            f"{label}: thinking_tokens")
        # Exact: the browser mirrors pricing.compute_cost's operation
        # order (per-term division) and Python's round(x, 6), so each
        # record's derived cost IS the stored cost_usd double.
        assert got["cost"] == want["cost_usd"], f"{label}: cost"
        # The long_context flag itself is asserted by the dedicated tests
        # below; here the buckets already pin the parse.


@pytest.mark.parametrize("name", [p.name for p in LANE_FIXTURES])
def test_browser_session_totals_match_backend_sum(name):
    backend = _backend_records(name)
    browser = _browser_totals(name)
    assert browser["fresh"] == sum(r["fresh_tokens"] for r in backend)
    assert browser["create"] == sum(r["cache_creation_tokens"] for r in backend)
    assert browser["read"] == sum(r["cache_read_tokens"] for r in backend)
    assert browser["output"] == sum(r["output_tokens"] for r in backend)
    # Exact: the browser mirrors pricing.compute_cost's operation order
    # (per-term division) and Python's round(x, 6), so every record's
    # derived cost IS the stored double (asserted per record above). The
    # total then depends only on summation: the browser accumulates
    # naively, CPython's sum() is Neumaier-compensated and may land one
    # ulp away, so sum here the way the browser does — left to right.
    total = 0.0
    for r in backend:
        total += r["cost_usd"]
    assert browser["cost"] == total


def test_a_settings_only_model_switch_labels_the_following_request():
    """A switch carried by thread_settings_applied alone, with the next
    request before any turn_context re-declares: both parsers must label
    the request gpt-5.6-terra. rollout_model_switch.jsonl has no
    token_count in that window, so the parity sweep missed it."""
    blob = (FIX_CODEX / "rollout_settings_switch.jsonl").read_bytes()
    backend = parse.parse_file("codex/settings_switch.jsonl", blob)["records"]
    assert len(backend) == 1
    assert backend[0]["model"] == "gpt-5.6-terra"
    assert backend[0]["line_num"] == 3

    browser = _browser_records("rollout_settings_switch.jsonl")
    assert len(browser) == 1
    assert browser[0]["model"] == "gpt-5.6-terra"
    assert browser[0]["line"] == 3


def test_model_map_order_keeps_the_generations_apart_in_both_parsers():
    """gpt-6-sol contains the needle sol, so the substring map's order is
    load-bearing (D7): a bare sol model must fall to gpt-5.6-sol while
    gpt-6-sol stays itself — in the browser exactly as in the backend."""
    lines = [
        {"timestamp": "2026-06-14T12:00:01.000Z", "type": "turn_context",
         "payload": {"model": "gpt-6-sol"}},
        {"timestamp": "2026-06-14T12:00:02.000Z", "type": "event_msg",
         "payload": {"type": "token_count", "info": {
             "total_token_usage": _MODEL_MAP_USAGE,
             "last_token_usage": _MODEL_MAP_USAGE}}},
        {"timestamp": "2026-06-14T12:00:03.000Z", "type": "turn_context",
         "payload": {"model": "sol"}},
        {"timestamp": "2026-06-14T12:00:04.000Z", "type": "event_msg",
         "payload": {"type": "token_count", "info": {
             "total_token_usage": _MODEL_MAP_USAGE2,
             "last_token_usage": _MODEL_MAP_USAGE2}}},
    ]
    blob = b"".join(json.dumps(line).encode() + b"\n" for line in lines)
    backend = parse.parse_file("codex/model_map_order.jsonl", blob)["records"]
    assert [r["model"] for r in backend] == ["gpt-6-sol", "gpt-5.6-sol"]

    script = f"""
      global.window = {{}};
      require({str(LANES_JS)!r});
      require({str(PARSER_JS)!r});
      const {{ events, meta }} = window.parseTranscript(
        {json.dumps(blob.decode())});
      console.log(JSON.stringify(
        meta.filter(m => m.type === 'assistant_usage').map(m => m.model)));
    """
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == ["gpt-6-sol", "gpt-5.6-sol"]


# --------------------------------------------------------------------------
# The long-context meter (T1-b)
# --------------------------------------------------------------------------


def _long_context_blob(plan_type: str | None) -> bytes:
    """One Codex request at 300k prompt tokens — over the 272k threshold.

    Built inline: fixtures/parser files must stay under 1 KB, and no
    committed fixture carries a request this large.
    """
    usage = {
        "input_tokens": 300_000, "cached_input_tokens": 290_000,
        "cache_write_input_tokens": 0, "output_tokens": 2_000,
        "reasoning_output_tokens": 1_000, "total_tokens": 302_000,
    }
    lines = [
        {"timestamp": "2026-06-14T12:00:01.000Z", "type": "session_meta",
         "payload": {"session_id": "00000000-0000-4000-8000-0000000000f1"}},
        {"timestamp": "2026-06-14T12:00:02.000Z", "type": "turn_context",
         "payload": {"model": "gpt-5.6-sol"}},
        {"timestamp": "2026-06-14T12:00:03.000Z", "type": "event_msg",
         "payload": {"type": "token_count",
                     "info": {"total_token_usage": usage,
                              "last_token_usage": usage},
                     # plan_type rides the payload's rate_limits (backend
                     # reads payload.rate_limits.plan_type).
                     **({"rate_limits": {"plan_type": plan_type}}
                        if plan_type else {})}},
    ]
    return b"".join(json.dumps(line).encode() + b"\n" for line in lines)


def _node_long_context(plan_type: str | None) -> dict:
    script = f"""
      global.window = {{}};
      require({str(LANES_JS)!r});
      require({str(PARSER_JS)!r});
      const text = {json.dumps(_long_context_blob(plan_type).decode())};
      const {{ events, meta }} = window.parseTranscript(text);
      const s = window.computeSessionStats(events, meta);
      const rec = meta.find(m => m.type === 'assistant_usage');
      console.log(JSON.stringify({{
        total_in: rec.usage.input_tokens + rec.usage.cache_creation_input_tokens
                + rec.usage.cache_read_input_tokens,
        long_context: !!rec.long_context,
        cost: s.cost,
      }}));
    """
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.parametrize("plan_type", [None, "pro"])
def test_browser_costs_a_long_context_request_exactly_like_the_backend(plan_type):
    """A >272k-token Codex request bills the whole record on the long-context
    meter — unless the rollout is a ChatGPT-plan subscription, which has no
    such tier. The browser must mirror both branches of the rule."""
    blob = _long_context_blob(plan_type)
    backend = parse.parse_file("codex/long_context.jsonl", blob)["records"]
    assert len(backend) == 1
    rec = backend[0]
    assert (rec["fresh_tokens"] + rec["cache_creation_tokens"]
            + rec["cache_read_tokens"]) > pricing.LONG_CONTEXT_THRESHOLD

    got = _node_long_context(plan_type)
    assert got["long_context"] is (plan_type is None)
    assert got["total_in"] == (rec["fresh_tokens"] + rec["cache_creation_tokens"]
                               + rec["cache_read_tokens"])
    assert got["cost"] == pytest.approx(rec["cost_usd"], rel=1e-5)


def test_browser_long_context_threshold_equals_backend():
    script = f"""
      global.window = {{}};
      require({str(LANES_JS)!r});
      console.log(JSON.stringify(window.LONG_CONTEXT_THRESHOLD));
    """
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == pricing.LONG_CONTEXT_THRESHOLD


# --------------------------------------------------------------------------
# Sniff parity with backend.parse_lanes.sniff_format
# --------------------------------------------------------------------------


def _node_sniff(blobs: list[bytes]) -> list[str]:
    script = f"""
      global.window = {{}};
      require({str(LANES_JS)!r});
      const blobs = {json.dumps([b.decode('utf-8', 'surrogateescape') for b in blobs])};
      console.log(JSON.stringify(blobs.map(b => window.sniffTranscriptFormat(b))));
    """
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.parametrize(
    "label",
    [p.name for p in LANE_FIXTURES]
    + [f"tricky[{i}]" for i in range(len(TRICKY_BLOBS))],
)
def test_browser_sniff_agrees_with_backend(label):
    blobs = [p.read_bytes() for p in LANE_FIXTURES] + list(TRICKY_BLOBS)
    labels = [p.name for p in LANE_FIXTURES] + [
        f"tricky[{i}]" for i in range(len(TRICKY_BLOBS))]
    idx = labels.index(label)
    assert _node_sniff([blobs[idx]])[0] == parse.sniff_format(blobs[idx])


# --------------------------------------------------------------------------
# The Inspector render contract on lane output
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", [p.name for p in LANE_FIXTURES])
def test_lane_output_carries_the_fields_the_inspector_renders(name):
    """The Inspector (detail-pane.jsx, SessionView, SessionHeader,
    ContextGrowthView, txToDashData) reads a fixed set of fields off the
    Claude parse's shapes. A lane transcript goes through the same
    components, so the lane parse must populate those same fields —
    never a differently-named shape the renderer would crash on."""
    fixtures = {p.name: p.read_text(encoding="utf-8") for p in [next(
        p for p in LANE_FIXTURES if p.name == name)]}
    script = f"""
      global.window = {{}};
      require({str(LANES_JS)!r});
      require({str(PARSER_JS)!r});
      const fixtures = {json.dumps(fixtures)};
      const out = {{}};
      for (const [name, text] of Object.entries(fixtures)) {{
        const {{ events, meta }} = window.parseTranscript(text);
        out[name] = {{
          event_types: [...new Set(events.map(e => e.type))],
          tool_calls_shaped: events
            .filter(e => e.type === 'tool_call')
            .every(e => typeof e.tool_name === 'string'
                     && e.tool_input !== null && typeof e.tool_input === 'object'
                     && typeof e.line === 'number'),
          tool_results_shaped: events
            .filter(e => e.type === 'tool_result')
            .every(e => typeof e.tool_use_id === 'string'
                     && typeof e.detail === 'string'
                     && typeof e.is_error === 'boolean'
                     && typeof e.line === 'number'),
          usages_shaped: meta
            .filter(m => m.type === 'assistant_usage')
            .every(m => typeof m.line === 'number' && m.ts != null
                     && typeof m.model === 'string'
                     && typeof m.usage === 'object' && m.usage !== null
                     && Number.isFinite(m.usage.input_tokens)
                     && Number.isFinite(m.usage.output_tokens)
                     && Number.isFinite(m.usage.cache_creation_input_tokens)
                     && Number.isFinite(m.usage.cache_read_input_tokens)),
          stats: Object.keys(window.computeSessionStats(events, meta)).sort(),
        }};
      }}
      console.log(JSON.stringify(out));
    """
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout)[name]

    assert set(got["event_types"]) <= {
        "user_message", "assistant_text", "thinking",
        "tool_call", "tool_result",
    }, "event types the Inspector's EventDetail switch knows"
    assert got["tool_calls_shaped"], "tool_call fields"
    assert got["tool_results_shaped"], "tool_result fields"
    assert got["usages_shaped"], "assistant_usage fields"
    # SessionHeader reads every one of these off stats.
    assert {"turns", "userMsgs", "toolCalls", "errorResults",
            "parallelBatches", "firstTs", "lastTs", "output",
            "hitRate", "cost"} <= set(got["stats"])


# --------------------------------------------------------------------------
# capForModel lane caps
# --------------------------------------------------------------------------


def _capformodel_source() -> str:
    """The capForModel function + its MODEL_CAPS table, extracted verbatim
    from the JSX source (node cannot parse the file's JSX elsewhere)."""
    src = CHARTS_JSX.read_text(encoding="utf-8")
    match = re.search(
        r"const MODEL_CAPS = \{.*?\n\};\nfunction capForModel\(m\) \{.*?\n\}",
        src, re.S)
    assert match, "capForModel/MODEL_CAPS not found in dashboard-charts-extra.jsx"
    return match.group(0)


def test_capformodel_lane_caps():
    script = f"""
      const src = {json.dumps(_capformodel_source())};
      eval(src);
      console.log(JSON.stringify({{
        sol: capForModel('gpt-6-sol'),
        sol56: capForModel('gpt-5-6-sol'),
        luna6: capForModel('gpt-6-luna'),
        kimi: capForModel('kimi-k3'),
        opus5: capForModel('claude-opus-5'),
        opus48: capForModel('claude-opus-4-8'),
        fable: capForModel('fable-5-1'),
        haiku: capForModel('haiku-4-5'),
      }}));
    """
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout)
    assert got["sol"] == 272_000
    assert got["sol56"] == 272_000
    assert got["luna6"] == 272_000
    assert got["kimi"] == 256_000
    assert got["opus5"] == 1_000_000
    assert got["opus48"] == 1_000_000
    assert got["fable"] == 1_000_000
    assert got["haiku"] == 200_000


# --------------------------------------------------------------------------
# The Token Breakdown's long-context re-derivation (I1)
# --------------------------------------------------------------------------

APP_JSX = ROOT / "src" / "app.jsx"


def _token_breakdown_source() -> str:
    """computeTokenBreakdown verbatim from the JSX source (node cannot
    parse the file's JSX elsewhere)."""
    src = APP_JSX.read_text(encoding="utf-8")
    start = src.index("function computeTokenBreakdown")
    end = src.index("\n// Paired token/cost breakdown bars", start)
    return src[start:end]


def test_browser_token_breakdown_applies_the_long_context_meter():
    """The Token Breakdown re-derives per-component cost from summed
    tokens, so a long-context row must be priced at the same 2x input /
    1.5x output pricing.compute_cost stored — or its bars drift from the
    stored cost_total they decompose."""
    lc_fresh, lc_out = 300_000, 2_000
    flat_fresh, flat_out = 50_000, 500
    # The events' ts is June 2026 — inside sol's pre-Aug21 dated window —
    # so the expected figures are computed at the SAME rates the browser's
    # rateForModel resolves, isolating the meter as the only difference.
    ts = datetime(2026, 6, 14, 12, 0, tzinfo=UTC)
    expected_lc = pricing.compute_cost(
        "gpt-5-6-sol", fresh=lc_fresh, output=lc_out, eph5=0, eph1h=0,
        unsplit_create=0, read=0, long_context=True, ts=ts,
    )
    expected_flat = pricing.compute_cost(
        "gpt-5-6-sol", fresh=flat_fresh, output=flat_out, eph5=0, eph1h=0,
        unsplit_create=0, read=0, ts=ts,
    )
    script = f"""
      global.window = {{}};
      require({str(LANES_JS)!r});
      require({str(PARSER_JS)!r});
      window.dashboardCol = {{}};
      eval({json.dumps(_token_breakdown_source())});
      const events = [
        {{ ts: Date.parse('2026-06-14T12:00:00Z'), model: 'gpt-5-6-sol',
           input_tokens: {lc_fresh}, output_tokens: {lc_out},
           cache_create: 0, cache_read: 0,
           ephemeral_5m: 0, ephemeral_1h: 0, long_context: true }},
        {{ ts: Date.parse('2026-06-14T12:00:00Z'), model: 'gpt-5-6-sol',
           input_tokens: {flat_fresh}, output_tokens: {flat_out},
           cache_create: 0, cache_read: 0,
           ephemeral_5m: 0, ephemeral_1h: 0, long_context: false }},
      ];
      const bd = computeTokenBreakdown(events);
      console.log(JSON.stringify({{
        costTotal: bd.costTotal,
        input: bd.rows.find(r => r.label === 'Input'),
        output: bd.rows.find(r => r.label === 'Output'),
      }}));
    """
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout)
    assert got["costTotal"] == pytest.approx(expected_lc + expected_flat)
    assert got["input"]["cost"] == pytest.approx(
        (lc_fresh * pricing.LONG_CONTEXT_INPUT_MULT + flat_fresh)
        * pricing.rate_for("gpt-5-6-sol", ts)["fresh"] / 1_000_000
    )
    assert got["output"]["cost"] == pytest.approx(
        (lc_out * pricing.LONG_CONTEXT_OUTPUT_MULT + flat_out)
        * pricing.rate_for("gpt-5-6-sol", ts)["output"] / 1_000_000
    )


def test_browser_long_context_multipliers_equal_backend():
    """The browser's meter multipliers are window constants from
    parser-lanes.js and must equal pricing's."""
    script = f"""
      global.window = {{}};
      require({str(LANES_JS)!r});
      console.log(JSON.stringify({{
        in: window.LONG_CONTEXT_INPUT_MULT,
        out: window.LONG_CONTEXT_OUTPUT_MULT,
      }}));
    """
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout)
    assert got["in"] == pricing.LONG_CONTEXT_INPUT_MULT
    assert got["out"] == pricing.LONG_CONTEXT_OUTPUT_MULT


def test_browser_inspector_turn_cost_applies_the_long_context_meter():
    """txToDashData re-derives each Inspector turn's cost from the parsed
    usage; a long-context record must price at the meter there too, or
    the Inspector's per-turn cost drifts from the stored figure. The
    Inspector path prices at LIST by construction (rateFor without a ts
    — the conservative default SV-DATED-RATES gives an unstamped
    record), so the expected figure is the metered LIST price."""
    expected = pricing.compute_cost(
        "gpt-5.6-sol", fresh=10_000, output=2_000, eph5=0, eph1h=0,
        unsplit_create=0, read=290_000, long_context=True,
    )
    script = f"""
      global.window = {{ shortModelName: m => m }};
      require({str(LANES_JS)!r});
      require({str(PARSER_JS)!r});
      const text = {json.dumps(_long_context_blob(None).decode())};
      const tx = window.parseTranscript(text);
      const src = require('fs').readFileSync({str(APP_JSX)!r}, 'utf8');
      const start = src.indexOf('function txToDashData');
      const end = src.indexOf('\\nfunction App(', start);
      eval(src.slice(start, end));
      const dash = txToDashData(tx);
      console.log(JSON.stringify({{
        turns: dash.events.length,
        cost: dash.events.reduce((s, e) => s + e.cost_usd, 0),
      }}));
    """
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout)
    assert got["turns"] == 1
    assert got["cost"] == pytest.approx(expected)
