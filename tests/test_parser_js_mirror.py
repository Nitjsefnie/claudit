"""SV-PARSER-SPEC: src/parser.js resolves rates like backend/pricing.py.

Both read their rates from src/pricing.json (SV-RATE-DATA), but each carries
its own resolution logic. If that logic drifts, the Inspector and the
dashboard disagree on cost for the same file. This asserts parity by driving
the real parser.js through node — no npm, no build step, matching the repo's
no-toolchain rule. That both sides derive the same tables from the file is
pinned in test_pricing_data.py.
"""
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend import parse, pricing

ROOT = Path(__file__).resolve().parents[1]
PARSER_JS = ROOT / "src" / "parser.js"
UTC = timezone.utc

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available"
)

# (model id, ISO timestamp or None)
CASES = [
    ("claude-opus-4-8", None),
    ("claude-fable-5-1", None),
    ("claude-fable-5-1[1m]", None),
    ("claude-mythos-5-1", None),
    ("claude-opus-5-5", None),
    ("claude-opus-5-5[1m]", None),
    ("claude-mythos-5", None),
    ("claude-fable-9", None),
    ("claude-fable-5", None),
    ("claude-fable-5[1m]", None),
    ("claude-haiku-4-5-20251001", None),
    ("claude-opus-4-20250514", None),
    ("claude-opus-4-1-20250805", None),
    ("claude-3-7-sonnet-20250219", None),
    ("claude-opus-4-9", None),
    ("claude-opus-4-8-fast", None),
    ("claude-sonnet-6", None),
    ("anthropic/claude-opus-4.8", None),
    ("us.anthropic.claude-opus-4-8", None),
    ("CLAUDE-OPUS-4-8", None),
    ("gpt-5", None),
    ("claude-sonnet-5", "2026-07-21T10:00:00Z"),
    ("claude-sonnet-5", "2026-08-31T23:59:59Z"),
    ("claude-sonnet-5", "2026-09-01T00:00:00Z"),
    ("claude-sonnet-5", None),
    ("claude-opus-4-8", "2026-07-21T10:00:00Z"),
    ("glm-5.3-flash", None),
    ("glm-5.3-flash", "2026-09-09T15:59:59Z"),
    ("glm-5.3-flash", "2026-09-09T16:00:00Z"),
    ("GLM-5.3-Flash[1m]", None),
    # OpenRouter free/stealth shape match (raw + normalised id, in both
    # implementations) beside a paid id that must stay on DEFAULT.
    ("stealth/space-bunny-alpha", None),
    ("Stealth/Space-Bunny-Alpha", None),
    ("thinkingmachines/inkling:free", None),
    ("nvidia/nemotron-3-ultra-550b-a55b:free", None),
    ("stealth/claude-opus-4-8", None),
    ("openai/gpt-6-sol", None),
]

_KEYMAP = {"fresh": "fresh", "c5": "create_5m", "c1h": "create_1h",
           "read": "read", "out": "output"}


def _node_rates():
    script = f"""
      global.window = {{}};
      require({str(PARSER_JS)!r});
      const cases = {json.dumps(CASES)};
      const out = cases.map(([m, ts]) => {{
        const r = window.resolveModelRate(m, ts);
        return {{ model: m, ts, kind: r.kind, rates: r.rates }};
      }});
      console.log(JSON.stringify(out));
    """
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60,
        # Return code checked by hand on the next line.
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_parser_js_rate_table_matches_backend_pricing():
    for got in _node_rates():
        ts = (
            datetime.fromisoformat(got["ts"].replace("Z", "+00:00"))
            if got["ts"] else None
        )
        want = pricing.resolve(got["model"], ts)
        label = f"{got['model']} @ {got['ts']}"
        assert got["kind"] == want.kind, f"{label}: kind"
        for js_key, py_key in _KEYMAP.items():
            assert got["rates"][js_key] == pytest.approx(want.rates[py_key]), (
                f"{label}: {py_key}"
            )


def test_parser_js_exposes_the_same_rate_epochs():
    script = f"""
      global.window = {{}};
      require({str(PARSER_JS)!r});
      console.log(JSON.stringify(window.rateEpochs));
    """
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60,
        # Return code checked by hand on the next line.
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    js_epochs = [
        datetime.fromtimestamp(ms / 1000, tz=UTC) for ms in json.loads(proc.stdout)
    ]
    assert js_epochs == pricing.RATE_EPOCHS


# --------------------------------------------------------------------------
# Per-record cost rounding (M5)
# --------------------------------------------------------------------------


def _node_cost(output_tokens: int) -> float:
    """One record through the real computeSessionStats cost path. The
    model is the lane table's gpt-6-luna, listed at $0.50/M output — the
    review's own figure."""
    script = f"""
      global.window = {{}};
      require({str(PARSER_JS)!r});
      const m = {{ type: 'assistant_usage', line: 1,
                   ts: '2026-06-14T12:00:00Z', model: 'gpt-6-luna',
                   usage: {{ input_tokens: 0,
                             cache_creation_input_tokens: 0,
                             cache_read_input_tokens: 0,
                             output_tokens: {output_tokens} }} }};
      console.log(JSON.stringify(window.computeSessionStats([], [m]).cost));
    """
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_per_record_cost_rounds_exact_ties_half_even_like_python():
    """15625 output tokens at $0.50/M cost 0.0078125 exactly. Python's
    round(x, 6) — which priced the stored cost_usd — returns 0.007812
    (half-EVEN on the exact tie); Number(x.toFixed(6)) returns 0.007813
    (half-up). The cost path's comment claims its per-record cost IS the
    stored value, so it must round the way Python did."""
    assert 0.0078125 == 2 ** -7, "the case must be an exact tie"
    assert round(15625 * 0.5 / 1_000_000, 6) == 0.007812
    assert _node_cost(15_625) == 0.007812


def test_per_record_cost_rounding_agrees_with_python_on_a_tie_where_both_agree():
    """0.0234375 (46875 tokens at $0.50/M) is also an exact tie, one
    where half-up and half-even agree — the two languages must return
    the same figure all the same."""
    assert round(0.0234375, 6) == 0.023438
    assert _node_cost(46_875) == 0.023438


def test_per_record_cost_rounding_matches_python_away_from_ties():
    """A value whose expansion continues past the sixth decimal must
    round to whatever Python's round(x, 6) decides on the same double —
    the honest parity form, since the double's exact expansion is what
    both decide on."""
    tokens = 46_877
    assert _node_cost(tokens) == round(tokens * 0.5 / 1_000_000, 6)
    tokens = 1_000_001
    assert _node_cost(tokens) == round(tokens * 0.5 / 1_000_000, 6)


# --------------------------------------------------------------------------
# Claude-format merge key (SV-PARSER-SPEC)
# --------------------------------------------------------------------------


def _node_usage_records(text: str) -> list[dict]:
    """The browser's assistant_usage events for one Claude transcript."""
    script = f"""
      global.window = {{}};
      require({str(PARSER_JS)!r});
      const {{ meta }} = window.parseTranscript({json.dumps(text)});
      console.log(JSON.stringify(meta
        .filter(m => m.type === 'assistant_usage')
        .map(m => ({{ line: m.line,
                      fresh: m.usage.input_tokens || 0,
                      read: m.usage.cache_read_input_tokens || 0,
                      output: m.usage.output_tokens || 0 }}))));
    """
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.parametrize(
    "name", ["message_id_merge.jsonl", "streaming_merge.jsonl"]
)
def test_parser_js_merges_on_the_backend_key(name):
    """A line with no requestId merges on message.id in both parsers, so
    the Inspector shows the one record the database stores."""
    path = ROOT / "fixtures" / "parser" / name
    backend = [
        {"line": r["line_num"], "fresh": r["fresh_tokens"],
         "read": r["cache_read_tokens"], "output": r["output_tokens"]}
        for r in parse.parse_file(f"k/s/{name}", path.read_bytes())["records"]
    ]
    assert _node_usage_records(path.read_text(encoding="utf-8")) == backend
    assert len(backend) == 1, "both fixtures are one API message"


# --------------------------------------------------------------------------
# Per-provider rates (pricing.PROVIDER_RATES)
# --------------------------------------------------------------------------

# (model id, ISO timestamp or None, provider or None)
PROVIDER_CASES = [
    ("deepseek/deepseek-v4.1-flash", None, "Novita"),
    ("deepseek/deepseek-v4.1-flash", None, "Morph"),
    ("deepseek/deepseek-v4.1-flash", None, None),
    ("deepseek/deepseek-v4.1-flash", None, "NoSuchHost"),
    ("deepseek/deepseek-v4.1-flash", None, "novita"),
    ("DeepSeek/DeepSeek-V4.1-Flash", None, "Novita"),
    ("z-ai/glm-5.3-flash", None, "Modal"),
    ("glm-5.3-flash", "2026-09-01T00:00:00Z", None),
    ("glm-5.3-flash", "2026-09-01T00:00:00Z", "Novita"),
    ("deepseek/deepseek-v4-flash-0731", None, "Cohere"),
    ("deepseek/deepseek-v4-flash-20260731", None, "Cohere"),
    ("deepseek/deepseek-v4-flash-20260731", None, "Novita"),
    ("deepseek/deepseek-v4-flash", None, "Novita"),
    ("stealth/space-bunny-alpha", None, "Stealth"),
    ("stealth/space-bunny-alpha", None, None),
    ("claude-opus-4-8", None, "Novita"),
]


def _node_json(body: str):
    script = f"""
      global.window = {{}};
      require({str(PARSER_JS)!r});
      {body}
    """
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60,
        # Return code checked by hand on the next line.
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_parser_js_resolves_provider_rates_like_the_backend():
    got_all = _node_json(f"""
      const cases = {json.dumps(PROVIDER_CASES)};
      console.log(JSON.stringify(cases.map(([m, ts, p]) =>
        window.resolveModelRate(m, ts, p))));
    """)
    for (model, ts, provider), got in zip(PROVIDER_CASES, got_all):
        when = datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None
        want = pricing.resolve(model, when, provider)
        label = f"{model} @ {ts} via {provider}"
        assert got["kind"] == want.kind, f"{label}: kind"
        for js_key, py_key in _KEYMAP.items():
            assert got["rates"][js_key] == pytest.approx(want.rates[py_key]), (
                f"{label}: {py_key}"
            )


def test_parser_js_prices_a_provider_record_at_the_stored_cost():
    """The Inspector's cost for a transcript whose records name a host is
    the sum of what ingest stored for them."""
    path = ROOT / "fixtures" / "parser" / "openrouter_provider.jsonl"
    stored = parse.parse_file("k/s/s.jsonl", path.read_bytes())["records"]
    got = _node_json(f"""
      const {{ events, meta }} = window.parseTranscript(
        {json.dumps(path.read_text(encoding="utf-8"))});
      console.log(JSON.stringify({{
        cost: window.computeSessionStats(events, meta).cost,
        providers: meta.filter(m => m.type === 'assistant_usage')
                       .map(m => m.provider),
      }}));
    """)
    assert got["providers"] == [r["provider"] for r in stored]
    assert got["cost"] == pytest.approx(sum(r["cost_usd"] for r in stored),
                                        abs=1e-9)


def test_parser_js_merges_provider_across_streaming_chunks():
    """One requestId, several assistant lines: the record's provider is
    the FIRST non-null chunk's, and a later provider-less chunk does not
    wipe it — the same first-non-null-wins rule the requestId max-merge
    applies in parse.py, pinned against the backend on the same fixture."""
    name = "provider_merge.jsonl"
    path = ROOT / "fixtures" / "parser" / name
    stored = parse.parse_file(f"k/s/{name}", path.read_bytes())["records"]
    got = _node_json(f"""
      const {{ meta }} = window.parseTranscript(
        {json.dumps(path.read_text(encoding="utf-8"))});
      console.log(JSON.stringify(meta
        .filter(m => m.type === 'assistant_usage')
        .map(m => ({{ provider: m.provider,
                      fresh: m.usage.input_tokens || 0,
                      output: m.usage.output_tokens || 0 }}))));
    """)
    want = [(r["provider"], r["fresh_tokens"], r["output_tokens"])
            for r in stored]
    assert [(g["provider"], g["fresh"], g["output"]) for g in got] == want
    # The fixture carries both orders — provider arriving on the SECOND
    # chunk (req-1) and a provider-less chunk after it (req-2) — so neither
    # mutant (never carrying, always overwriting) can pass it by luck.
    assert want == [("Novita", 1000, 200), ("Novita", 1000, 300)]


# The parsed provider value carries parse._provider's guard: a string is
# trimmed, a whitespace-only one becomes null, anything else is null.

_UNSET = object()


def _provider_blob(raw=_UNSET) -> str:
    """One assistant line whose message.provider is `raw`; without one the
    key is absent. Alone on its requestId, so nothing merges."""
    message = {"role": "assistant", "model": "deepseek/deepseek-v4.1-flash",
               "content": [],
               "usage": {"input_tokens": 10, "output_tokens": 1}}
    if raw is not _UNSET:
        message["provider"] = raw
    return json.dumps({"type": "assistant", "requestId": "r-0",
                       "timestamp": "2026-09-21T10:00:00Z",
                       "message": message})


def _parsed_provider(raw=_UNSET) -> list:
    return _node_json(f"""
      const {{ meta }} = window.parseTranscript(
        {json.dumps(_provider_blob(raw))});
      console.log(JSON.stringify(meta
        .filter(m => m.type === 'assistant_usage')
        .map(m => m.provider)));
    """)


@pytest.mark.parametrize("raw, want", [
    pytest.param("  Chutes  ", "Chutes", id="padded"),
    pytest.param("   ", None, id="blank"),
])
def test_the_parsed_provider_strips_surrounding_whitespace(raw, want):
    assert _parsed_provider(raw) == [want]


@pytest.mark.parametrize("raw", [42, None], ids=["number", "null"])
def test_the_parsed_provider_non_string_values_are_null(raw):
    assert _parsed_provider(raw) == [None]


def test_the_parsed_provider_without_a_key_is_null():
    assert _parsed_provider() == [None]
