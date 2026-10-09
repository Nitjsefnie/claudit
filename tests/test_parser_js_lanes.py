"""Run browser lane parsing through Node and compare records/costs with
the backend, including per-model long-context meters.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

import pytest

from backend import parse, pricing

ROOT = Path(__file__).resolve().parents[1]
LANES_JS = ROOT / "src" / "parser-lanes.js"
CODEX_JS = ROOT / "src" / "parser-codex.js"
LOADER_JS = ROOT / "src" / "pricing-loader.js"
RATES_JS = ROOT / "src" / "rates.js"
PARSER_JS = ROOT / "src" / "parser.js"
FIX_PARSER = ROOT / "fixtures" / "parser"
FIX_CODEX = ROOT / "fixtures" / "codex"


# Lane fixtures used by backend parser tests.
LANE_FIXTURES = [
    FIX_PARSER / "codex_min.jsonl",
    FIX_PARSER / "codex_user_xml.jsonl",
    # The unattributed rollout has a separate browser null-model pin.
    *[p for p in sorted(FIX_CODEX.glob("*.jsonl"))
      if p.name != "rollout_fork_prefix.jsonl"],
    *sorted(FIX_PARSER.glob("kimi_*.jsonl")),
]

TRICKY_BLOBS = [
    b'{"sessionId":"x","type":"system","message":"hi"}\n',
    (b'{"type":"llm.error","time":1784213155000,'
     b'"kind":"quota_exhausted","message":"out of quota"}\n'),
    b"",
    b'{"unidentified": true}\n',
]


@lru_cache(maxsize=1)
def _browser_lane_output() -> dict:
    """Parse every lane fixture in the browser, once, through node."""
    fixtures = {p.name: p.read_text(encoding="utf-8") for p in LANE_FIXTURES}
    script = f"""
      global.window = {{}};
      require({str(LANES_JS)!r});
      require({str(CODEX_JS)!r});
      require({str(LOADER_JS)!r}); require({str(RATES_JS)!r}); require({str(PARSER_JS)!r});
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
    # STDIN avoids Windows' CreateProcess 32k command-line limit.
    proc = subprocess.run(
        ["node"], input=script, capture_output=True, text=True, timeout=120,
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


def _long_context_blob(plan_type: str | None) -> bytes:
    """One inline 300k Codex request; large fixtures stay out of parser/."""
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
                     # Backend reads plan_type from payload.rate_limits.
                     **({"rate_limits": {"plan_type": plan_type}}
                        if plan_type else {})}},
    ]
    return b"".join(json.dumps(line).encode() + b"\n" for line in lines)


def _node_long_context(plan_type: str | None) -> dict:
    script = f"""
      global.window = {{}};
      require({str(LANES_JS)!r});
      require({str(CODEX_JS)!r});
      require({str(LOADER_JS)!r}); require({str(RATES_JS)!r}); require({str(PARSER_JS)!r});
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


def _node_long_context_shape(blob: str, meters: dict) -> list:
    """Codex long_context flags under injected per-model meters."""
    script = f"""
      global.window = {{}};
      require({str(LANES_JS)!r});
      require({str(CODEX_JS)!r});
      require({str(LOADER_JS)!r}); require({str(PARSER_JS)!r});
      window.longContextMeters = {json.dumps(meters)};
      const {{ meta }} = window.parseTranscript({json.dumps(blob)});
      console.log(JSON.stringify(
        meta.filter(m => m.type === 'assistant_usage')
            .map(m => m.long_context)));
    """
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _node_sniff(blobs: list[bytes]) -> list[str]:
    script = f"""
      global.window = {{}};
      require({str(LANES_JS)!r});
      require({str(CODEX_JS)!r});
      const blobs = {json.dumps([b.decode('utf-8', 'surrogateescape') for b in blobs])};
      console.log(JSON.stringify(blobs.map(b => window.sniffTranscriptFormat(b))));
    """
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


APP_JSX = ROOT / "src" / "app.jsx"
CTX_INPUT_JS = ROOT / "src" / "ctx-input.js"
TOKEN_BREAKDOWN_JS = ROOT / "src" / "token-breakdown.js"


def _token_breakdown_source() -> str:
    """Read the standalone Token Breakdown fold (Node cannot parse JSX)."""
    return (ROOT / "src" / "token-breakdown.js").read_text(encoding="utf-8")


_NAIVE_LANE_BLOB = (
    b'{"type": "metadata", "protocol_version": "1.10"}\n'
    b'{"timestamp": "2026-09-10T00:30:00", "message": {"type": '
    b'"StatusUpdate", "payload": {"context_tokens": 10, "token_usage": '
    b'{"input_other": 10, "output": 2, "input_cache_read": 0, '
    b'"input_cache_creation": 0}, "message_id": "scripted-1"}}}\n'
)


class TestNodeDrivenLaneParsers:
    # Node-backed tests skip together when Node is unavailable.
    pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not available")

    @pytest.mark.parametrize("name", [p.name for p in LANE_FIXTURES])
    def test_browser_parse_matches_backend_per_record(self, name):
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
            # Match the backend's term order and per-record rounding.
            assert got["cost"] == want["cost_usd"], f"{label}: cost"

    @pytest.mark.parametrize("name", [p.name for p in LANE_FIXTURES])
    def test_browser_session_totals_match_backend_sum(self, name):
        backend = _backend_records(name)
        browser = _browser_totals(name)
        assert browser["fresh"] == sum(r["fresh_tokens"] for r in backend)
        assert browser["create"] == sum(r["cache_creation_tokens"] for r in backend)
        assert browser["read"] == sum(r["cache_read_tokens"] for r in backend)
        assert browser["output"] == sum(r["output_tokens"] for r in backend)
        # Sum left to right like the browser; CPython sum may differ by an ulp.
        total = 0.0
        for r in backend:
            total += r["cost_usd"]
        assert browser["cost"] == total

    def test_a_settings_only_model_switch_labels_the_following_request(self):
        """A settings-only switch labels the next request in both parsers."""
        blob = (FIX_CODEX / "rollout_settings_switch.jsonl").read_bytes()
        backend = parse.parse_file("codex/settings_switch.jsonl", blob)["records"]
        assert len(backend) == 1
        assert backend[0]["model"] == "gpt-5.6-terra"
        assert backend[0]["line_num"] == 3

        browser = _browser_records("rollout_settings_switch.jsonl")
        assert len(browser) == 1
        assert browser[0]["model"] == "gpt-5.6-terra"
        assert browser[0]["line"] == 3

    def test_a_browser_parse_never_labels_a_model_unknown(self):
        """An unattributed browser record keeps null, never 'unknown'."""
        text = (FIX_CODEX / "rollout_fork_prefix.jsonl").read_text(encoding="utf-8")
        script = f"""
          global.window = {{}};
          require({str(LANES_JS)!r});
          require({str(CODEX_JS)!r});
          const {{ meta }} = window.parseTranscriptLanes(
            {json.dumps(text)}, {{}});
          console.log(JSON.stringify(
            meta.filter(m => m.type === 'assistant_usage').map(m => m.model)));
        """
        proc = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True,
            timeout=60, check=False,
        )
        assert proc.returncode == 0, proc.stderr
        models = json.loads(proc.stdout)
        assert models, "fixture produced no records"
        assert all(m != "unknown" for m in models)

    def test_model_ids_survive_verbatim_in_both_parsers(self):
        """Model switches keep their normalized id in both parsers."""
        def _turn(second: int, model: str) -> dict:
            return {"timestamp": f"2026-06-14T12:00:{second:02d}.000Z",
                    "type": "turn_context", "payload": {"model": model}}

        def _usage(n_in: int, n_cached: int, n_out: int, n_reason: int) -> dict:
            return {"input_tokens": n_in, "cached_input_tokens": n_cached,
                    "cache_write_input_tokens": 0, "output_tokens": n_out,
                    "reasoning_output_tokens": n_reason,
                    "total_tokens": n_in + 500}

        def _snapshot(second: int, n_in: int) -> dict:
            u = _usage(n_in, n_in - 500, 400 + n_in // 1000 * 100,
                       300 + n_in // 1000 * 50)
            return {"timestamp": f"2026-06-14T12:00:{second:02d}.000Z",
                    "type": "event_msg",
                    "payload": {"type": "token_count",
                                "info": {"total_token_usage": u,
                                         "last_token_usage": u,
                                         "model_context_window": 258400}}}

        models = ["gpt-6-sol", "gpt-6.1-sol", "sol", "gpt5.6-sol",
                  "gpt\u0665.6-sol"]
        lines: list[dict] = [_turn(1, models[0])]
        for i in range(len(models)):
            lines.append(_snapshot(2 * i + 2, 1_000 * (i + 1)))
            if i + 1 < len(models):
                lines.append(_turn(2 * i + 3, models[i + 1]))
        blob = b"".join(json.dumps(line).encode() + b"\n" for line in lines)
        out = parse.parse_file("codex/model_ids_verbatim.jsonl", blob)
        backend = out["records"]
        got = [r["model"] for r in backend]
        assert got == ["gpt-6-sol", "gpt-6.1-sol", "sol", "gpt-5.6-sol",
                       "gpt\u0665.6-sol"]

        script = f"""
          global.window = {{}};
          require({str(LANES_JS)!r});
          require({str(CODEX_JS)!r});
          require({str(LOADER_JS)!r}); require({str(RATES_JS)!r}); require({str(PARSER_JS)!r});
          const {{ events, meta }} = window.parseTranscript(
            {json.dumps(blob.decode())});
          console.log(JSON.stringify(
            meta.filter(m => m.type === 'assistant_usage').map(m => m.model)));
        """
        proc = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True,
            encoding="utf-8", timeout=60, check=False,
        )
        assert proc.returncode == 0, proc.stderr
        got_browser = json.loads(proc.stdout)
        assert got_browser == ["gpt-6-sol", "gpt-6.1-sol", "sol", "gpt-5.6-sol",
                               "gpt\u0665.6-sol"]

    @pytest.mark.parametrize("plan_type", [None, "pro"])
    def test_browser_costs_a_long_context_request_exactly_like_the_backend(self, plan_type):
        """The meter applies to Codex requests regardless of plan type."""
        blob = _long_context_blob(plan_type)
        backend = parse.parse_file("codex/long_context.jsonl", blob)["records"]
        assert len(backend) == 1
        rec = backend[0]
        assert (rec["fresh_tokens"] + rec["cache_creation_tokens"]
                + rec["cache_read_tokens"]) > pricing.LONG_CONTEXT_THRESHOLD

        got = _node_long_context(plan_type)
        assert got["long_context"] is True
        assert got["total_in"] == (rec["fresh_tokens"] + rec["cache_creation_tokens"]
                                   + rec["cache_read_tokens"])
        assert got["cost"] == pytest.approx(rec["cost_usd"], rel=1e-5)

    def test_browser_long_context_threshold_equals_backend(self):
        script = f"""
          global.window = {{}};
          require({str(LANES_JS)!r});
          require({str(CODEX_JS)!r});
          console.log(JSON.stringify(window.LONG_CONTEXT_THRESHOLD));
        """
        proc = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, timeout=60,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout) == pricing.LONG_CONTEXT_THRESHOLD

    def test_browser_threshold_and_flag_decide_per_model(self):
        """The threshold and Claude flag follow the model's meter."""
        script = f"""
          global.window = {{}};
          require({str(LANES_JS)!r});
          require({str(CODEX_JS)!r});
          require({str(LOADER_JS)!r}); require({str(RATES_JS)!r}); require({str(PARSER_JS)!r});
          window.longContextModels = ['acme-test-1'];
          window.longContextMeters = {{'acme-test-1': {{threshold: 200000}}}};
          const usage = {{input_tokens: 250000, cache_creation_input_tokens: 0,
                         cache_read_input_tokens: 10000}};
          console.log(JSON.stringify({{
            override: window.longContextThresholdFor('acme-test-1'),
            fallback: window.longContextThresholdFor('no-such-model'),
            above: window.longContextFlagFor('acme-test-1', usage),
            below: window.longContextFlagFor('acme-test-1',
              {{...usage, input_tokens: 150000}}),
            nonmember: window.longContextFlagFor('no-such-model', usage),
            fallbackFactors: window.longContextFactorsFor('acme-test-1'),
          }}));
        """
        proc = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, timeout=60,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout) == {
            "override": 200_000,
            "fallback": pricing.LONG_CONTEXT_THRESHOLD,
            "above": True,
            "below": False,
            "nonmember": None,
            "fallbackFactors": [
                pricing.LONG_CONTEXT_INPUT_MULT,
                pricing.LONG_CONTEXT_OUTPUT_MULT,
            ],
        }

    def test_browser_codex_lane_decides_on_the_model_threshold(self):
        """The Codex lane consults its injected per-model threshold."""
        usage = {"input_tokens": 250_000, "cached_input_tokens": 0,
                 "cache_write_input_tokens": 0, "output_tokens": 1_000,
                 "reasoning_output_tokens": 0, "total_tokens": 251_000}
        info = {"total_token_usage": usage, "last_token_usage": usage,
                "model_context_window": 400000}
        blob = "".join(json.dumps(line) + "\n" for line in [
            {"timestamp": "2026-07-01T00:00:00.000Z", "type": "turn_context",
             "payload": {"model": "gpt-5.6-sol"}},
            {"timestamp": "2026-07-01T00:00:01.000Z", "type": "event_msg",
             "payload": {"type": "token_count", "info": info}}])
        got = _node_long_context_shape(
            blob, {"gpt-5-6-sol": {"threshold": 200000}})
        assert got == [True]

    def test_per_model_meter_factors_match_backend_and_inspector(self, monkeypatch):
        """Synthetic per-model factors match backend and Inspector costs."""
        model_key = "gpt-5-6-sol"
        rates = {
            "fresh": 7.0, "create_5m": 8.75, "create_1h": 14.0,
            "read": 0.7, "output": 35.0,
        }
        meter = {"threshold": 100_000, "input_mult": 5.0,
                 "output_mult": 5.0}
        monkeypatch.setattr(pricing, "MODEL_RATES",
                            {**pricing.MODEL_RATES, model_key: rates})
        monkeypatch.setattr(
            pricing, "DATED_RATES",
            {key: value for key, value in pricing.DATED_RATES.items()
             if key != model_key})
        monkeypatch.setattr(
            pricing, "LONG_CONTEXT_MODELS",
            frozenset({*pricing.LONG_CONTEXT_MODELS, model_key}))
        monkeypatch.setattr(pricing, "LONG_CONTEXT_METERS",
                            {**pricing.LONG_CONTEXT_METERS, model_key: meter})
        backend = parse.parse_file(
            "codex/per_model_meter.jsonl", _long_context_blob(None))["records"][0]
        assert backend["long_context"] is True

        script = f"""
          global.window = {{shortModelName: m => m, dashboardCol: {{inputTokens: '#1',
            outputTokens: '#2', cacheCreateTokens: '#3', cacheReadTokens: '#4'}}}};
          require({str(LANES_JS)!r});
          require({str(CODEX_JS)!r});
          require({str(LOADER_JS)!r});
          window.modelRates[{json.dumps(model_key)}] = {{
            fresh: 7.0, c5: 8.75, c1h: 14.0, read: 0.7, out: 35.0 }};
          delete window.datedRates[{json.dumps(model_key)}];
          delete window.modelFees[{json.dumps(model_key)}];
          window.longContextModels = [{json.dumps(model_key)}];
          window.longContextMeters = {{{json.dumps(model_key)}: {json.dumps(meter)}}};
          require({str(RATES_JS)!r});
          require({str(PARSER_JS)!r});
          require({str(TOKEN_BREAKDOWN_JS)!r});
          require({str(CTX_INPUT_JS)!r});
          const text = {json.dumps(_long_context_blob(None).decode())};
          const tx = window.parseTranscript(text);
          const {{ events, meta }} = tx;
          const record = meta.find(m => m.type === 'assistant_usage');
          const usage = record.usage;
          const inspector = window.computeTokenBreakdown([{{
            model_id: record.model, model: record.model, provider: null,
            ts: Date.parse(record.ts), input_tokens: usage.input_tokens,
            output_tokens: usage.output_tokens,
            cache_create: usage.cache_creation_input_tokens,
            cache_read: usage.cache_read_input_tokens,
            ephemeral_5m: 0, ephemeral_1h: 0,
            long_context: record.long_context,
          }}]);
          const source = require('fs').readFileSync({str(APP_JSX)!r}, 'utf8');
          const start = source.indexOf('function txToDashData');
          const end = source.indexOf('\\nfunction App(', start);
          eval(source.slice(start, end));
          const dashboard = txToDashData(tx);
          console.log(JSON.stringify({{
            factors: window.longContextFactorsFor(record.model),
            session: window.computeSessionStats(events, meta).cost,
            inspector: inspector.costTotal,
            appInspector: dashboard.events.reduce((sum, event) => sum + event.cost_usd, 0),
          }}));
        """
        proc = subprocess.run(["node", "-e", script], capture_output=True,
                              text=True, timeout=60, check=False)
        assert proc.returncode == 0, proc.stderr
        got = json.loads(proc.stdout)
        assert got["factors"] == [5.0, 5.0]
        assert got["session"] == pytest.approx(backend["cost_usd"], abs=1e-9)
        assert got["inspector"] == pytest.approx(backend["cost_usd"], abs=1e-9)
        assert got["appInspector"] == pytest.approx(backend["cost_usd"], abs=1e-9)

    @pytest.mark.parametrize(
        "label",
        [p.name for p in LANE_FIXTURES]
        + [f"tricky[{i}]" for i in range(len(TRICKY_BLOBS))],
    )
    def test_browser_sniff_agrees_with_backend(self, label):
        blobs = [p.read_bytes() for p in LANE_FIXTURES] + list(TRICKY_BLOBS)
        labels = [p.name for p in LANE_FIXTURES] + [
            f"tricky[{i}]" for i in range(len(TRICKY_BLOBS))]
        idx = labels.index(label)
        assert _node_sniff([blobs[idx]])[0] == parse.sniff_format(blobs[idx])

    @pytest.mark.parametrize("name", [p.name for p in LANE_FIXTURES])
    def test_lane_output_carries_the_fields_the_inspector_renders(self, name):
        """Pin the event shapes consumed by Inspector components."""
        fixtures = {p.name: p.read_text(encoding="utf-8") for p in [next(
            p for p in LANE_FIXTURES if p.name == name)]}
        script = f"""
          global.window = {{}};
          require({str(LANES_JS)!r});
          require({str(CODEX_JS)!r});
          require({str(LOADER_JS)!r}); require({str(RATES_JS)!r}); require({str(PARSER_JS)!r});
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
        assert {"turns", "userMsgs", "toolCalls", "errorResults",
                "parallelBatches", "firstTs", "lastTs", "output",
                "hitRate", "cost"} <= set(got["stats"])

    def test_browser_token_breakdown_applies_the_long_context_meter(self):
        """Token Breakdown applies the meter to long-context rows."""
        lc_fresh, lc_out = 300_000, 2_000
        flat_fresh, flat_out = 50_000, 500
        # Use the browser's same dated window to isolate the meter.
        ts = datetime(2026, 6, 14, 12, 0, tzinfo=UTC)
        expected_lc = pricing.compute_cost(
            "gpt-5-6-sol", fresh=lc_fresh, output=lc_out, eph5=0, eph1h=0,  # sv-test-data: allow (derived: expected priced from the same loaded tables as the JS side)
            unsplit_create=0, read=0, long_context=True, ts=ts,
        )
        expected_flat = pricing.compute_cost(
            "gpt-5-6-sol", fresh=flat_fresh, output=flat_out, eph5=0, eph1h=0,  # sv-test-data: allow (derived: expected priced from the same loaded tables as the JS side)
            unsplit_create=0, read=0, ts=ts,
        )
        script = f"""
          global.window = {{}};
          require({str(LANES_JS)!r});
          require({str(CODEX_JS)!r});
          require({str(LOADER_JS)!r}); require({str(RATES_JS)!r}); require({str(PARSER_JS)!r});
          window.dashboardCol = {{}};
          eval({json.dumps(_token_breakdown_source())});
          const events = [
            {{ ts: Date.parse('2026-06-14T12:00:00Z'), model: 'gpt-5-6-sol',
               model_id: 'gpt-5-6-sol',
               input_tokens: {lc_fresh}, output_tokens: {lc_out},
               cache_create: 0, cache_read: 0,
               ephemeral_5m: 0, ephemeral_1h: 0, long_context: true }},
            {{ ts: Date.parse('2026-06-14T12:00:00Z'), model: 'gpt-5-6-sol',
               model_id: 'gpt-5-6-sol',
               input_tokens: {flat_fresh}, output_tokens: {flat_out},
               cache_create: 0, cache_read: 0,
               ephemeral_5m: 0, ephemeral_1h: 0, long_context: false }},
          ];
          const bd = window.computeTokenBreakdown(events);
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
            * pricing.rate_for("gpt-5-6-sol", ts)["fresh"] / 1_000_000  # sv-test-data: allow (derived: expected priced from the same loaded tables as the JS side)
        )
        assert got["output"]["cost"] == pytest.approx(
            (lc_out * pricing.LONG_CONTEXT_OUTPUT_MULT + flat_out)
            * pricing.rate_for("gpt-5-6-sol", ts)["output"] / 1_000_000  # sv-test-data: allow (derived: expected priced from the same loaded tables as the JS side)
        )

    def test_browser_long_context_multipliers_equal_backend(self):
        """Global browser fallback factors mirror pricing."""
        script = f"""
          global.window = {{}};
          require({str(LANES_JS)!r});
          require({str(CODEX_JS)!r});
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

    def test_browser_inspector_turn_cost_applies_the_long_context_meter(self):
        """Inspector turn cost matches the record's metered dated price."""
        expected = pricing.compute_cost(
            "gpt-5.6-sol", fresh=10_000, output=2_000, eph5=0, eph1h=0,  # sv-test-data: allow (derived: expected priced from the same loaded tables as the JS side)
            unsplit_create=0, read=290_000, long_context=True,
            ts=datetime(2026, 6, 14, 12, 0, 3, tzinfo=UTC),
        )
        script = f"""
          global.window = {{ shortModelName: m => m }};
          require({str(LANES_JS)!r});
          require({str(CODEX_JS)!r});
          require({str(LOADER_JS)!r}); require({str(RATES_JS)!r}); require({str(PARSER_JS)!r});
          require({str(CTX_INPUT_JS)!r});
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

    def test_lane_parser_reads_an_offset_less_timestamp_as_utc(self):
        """A legacy Kimi timestamp without an offset resolves as UTC."""
        out = parse.parse_file("sessions/p/s/naive.jsonl", _NAIVE_LANE_BLOB)
        assert len(out["records"]) == 1
        assert out["records"][0]["ts"] == datetime(
            2026, 9, 10, 0, 30, tzinfo=UTC)

        script = f"""
          global.window = {{}};
          require({str(LANES_JS)!r});
          require({str(CODEX_JS)!r});
          require({str(LOADER_JS)!r}); require({str(RATES_JS)!r}); require({str(PARSER_JS)!r});
          const {{ events, meta }} = window.parseTranscriptLanes(
            {json.dumps(_NAIVE_LANE_BLOB.decode())});
          const usage = meta.find((m) => m.type === 'assistant_usage');
          console.log(JSON.stringify({{ usageTs: usage && usage.ts }}));
        """
        env = {**os.environ, "TZ": "Europe/Berlin"}
        proc = subprocess.run(
            ["node", "-e", script], capture_output=True, text=True, timeout=60,
            env=env, check=False,  # Return code checked by hand on the next line.
        )
        assert proc.returncode == 0, proc.stderr
        got = json.loads(proc.stdout)
        assert got["usageTs"] == "2026-09-10T00:30:00.000Z"
