"""SV-PARSER-SPEC: src/parser.js resolves rates like backend/pricing.py.

Both read their rates from src/pricing.json (SV-RATE-DATA), but each carries
its own resolution logic. If that logic drifts, the Inspector and the
dashboard disagree on cost for the same file. This asserts parity by driving
the real parser.js through node — no npm, no build step, matching the repo's
no-toolchain rule. That both sides derive the same tables from the file is
pinned in test_pricing_data.py.
"""
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend import parse, pricing

ROOT = Path(__file__).resolve().parents[1]
LOADER_JS = ROOT / "src" / "pricing-loader.js"
PARSER_JS = ROOT / "src" / "parser.js"
RECORD_DEDUP_JS = ROOT / "src" / "record-dedup.js"
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
    ("claude-sonnet-5-5", None),
    ("claude-sonnet-5-5[1m]", None),
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
      require({str(LOADER_JS)!r});
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
      require({str(LOADER_JS)!r});
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
      require({str(LOADER_JS)!r});
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
      require({str(LOADER_JS)!r});
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
# Cross-file dedup: the attributed copy wins the uuid (issue #529)
# --------------------------------------------------------------------------

def _claude_line(uuid, model, text, output_tokens, tool=None, request_id="req-1"):
    content = []
    if tool is not None:
        content.append({"type": "tool_use", "id": "tu-1", "name": tool,
                        "input": {}})
    content.append({"type": "text", "text": text})
    message = {"role": "assistant", "content": content,
               "usage": {"input_tokens": 100, "output_tokens": output_tokens}}
    if model is not None:
        message["model"] = model
    return json.dumps({"type": "assistant", "timestamp": "2026-05-07T10:00:00Z",
                       "uuid": uuid, "requestId": request_id, "sessionId": "sessS",
                       "message": message}, separators=(",", ":")) + "\n"


def _node_dedup_survivor(*files):
    """The winner view of several files parsed in order through ONE shared
    seenUuids map (the cross-file dedup mode), followed by the caller's
    post-load drop through recordDedup.dropMasked -- the drop the old
    contract left to prose no production caller implemented (issue #563),
    now module code the test drives the way a caller would.

    Events are pinned beside meta: computeSessionStats counts tool calls
    from `events`, so a superseded copy's tool_call entries must leave the
    output together with its assistant_usage meta (issue #562) -- within a
    parse call by the end-of-parse retraction, across calls by
    dropMasked."""
    script = f"""
      global.window = {{}};
      require({str(RECORD_DEDUP_JS)!r});
      require({str(LOADER_JS)!r});
      require({str(PARSER_JS)!r});
      const seen = new Map();
      const allEvents = [];
      const allMeta = [];
      for (const text of {json.dumps(list(files))}) {{
        const {{ events, meta }} = window.parseTranscript(text, {{ seenUuids: seen }});
        allEvents.push(...events);
        allMeta.push(...meta.filter(m => m.type === 'assistant_usage'));
      }}
      window.recordDedup.dropMasked(allEvents, seen);
      window.recordDedup.dropMasked(allMeta, seen);
      const stats = window.computeSessionStats(allEvents, allMeta);
      console.log(JSON.stringify({{
        toolCalls: stats.toolCalls,
        toolNames: allEvents
          .filter(e => e.type === 'tool_call' || e.type === 'agent_spawn')
          .map(e => e.tool_name),
        texts: allEvents.filter(e => e.type === 'assistant_text')
          .map(e => e.detail),
        usage: allMeta.map(m => ({{ model: m.model,
                                   output: m.usage.output_tokens || 0 }})),
      }}));
    """
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# The winner view every order must agree on: one tool call, the winner's;
# one text, the winner's; one usage entry, the winner's.
_WINNER_VIEW = {
    "toolCalls": 1, "toolNames": ["Write"], "texts": ["attributed copy"],
    "usage": [{"model": "claude-sonnet-4-5", "output": 300}],
}


def test_dedup_prefers_the_attributed_copy_whatever_the_order():
    """The browser's cross-file dedup mirrors recompute_canonical's winner
    rule (SV-CANONICAL-FLAG): an unattributed copy must not mask a later
    attributed one, and a seen attributed copy beats a later unattributed
    one -- the winner is decided by attribution, not by arrival order, and
    the superseded copy's events never survive (issue #562)."""
    unknown = _claude_line("u-1", None, "copy with no model", 200, tool="Read")
    known = _claude_line("u-1", "claude-sonnet-4-5", "attributed copy", 300,
                         tool="Write")
    assert _node_dedup_survivor(unknown, known) == _WINNER_VIEW
    assert _node_dedup_survivor(known, unknown) == _WINNER_VIEW


def test_dedup_replace_retracts_events_within_one_call():
    """issue #562: a superseded copy's events are retracted in the SAME
    parse call that replaces it -- both copies sit in one file (distinct
    requestIds, so the two usage lines stay separate entries the
    retraction can drop one by one), and computeSessionStats must count
    one tool call, not two."""
    text = (_claude_line("u-1", None, "loser copy", 200, tool="Read",
                         request_id="req-1")
            + _claude_line("u-1", "claude-sonnet-4-5", "winner copy", 300,
                           tool="Write", request_id="req-2"))
    got = _node_dedup_survivor(text)
    assert got == {"toolCalls": 1, "toolNames": ["Write"],
                   "texts": ["winner copy"],
                   "usage": [{"model": "claude-sonnet-4-5", "output": 300}]}


def test_dedup_keeps_the_first_of_two_attributed_copies():
    """Attribution only breaks unknown-vs-known ties; two attributed
    copies of one uuid still keep the first (the file_key rule's
    first-seen analog), and nothing a drop would judge is lost."""
    first = _claude_line("u-1", "claude-sonnet-4-5", "first", 200)
    second = _claude_line("u-1", "claude-sonnet-4-5", "second", 300)
    assert _node_dedup_survivor(first, second) == {
        "toolCalls": 0, "toolNames": [], "texts": ["first"],
        "usage": [{"model": "claude-sonnet-4-5", "output": 200}]}


def test_dedup_of_two_unattributed_copies_keeps_the_first():
    """Two unattributed copies keep the first and the uuid never lands on
    `true`, so the drop pass keeps both sides of the verdict -- one copy,
    no invented winner."""
    first = _claude_line("u-1", None, "first unknown", 200)
    second = _claude_line("u-1", None, "second unknown", 300)
    assert _node_dedup_survivor(first, second) == {
        "toolCalls": 0, "toolNames": [], "texts": ["first unknown"],
        "usage": [{"model": None, "output": 200}]}


def test_dedup_pinned_on_the_lane_unknown_fallback_member():
    """The lane fallback spells its member 'unknown' outright and must
    classify as unattributed the same way as a model-less Claude copy's
    null (issue #688) -- the JS half of the shared vocabulary, each
    member pinned. This case rides the lane-fallback member; <synthetic>
    rides the vocabulary tests below (issue #563)."""
    lane_unknown = _claude_line("u-1", "unknown", "lane fallback copy", 200)
    known = _claude_line("u-1", "claude-sonnet-4-5", "attributed copy", 300,
                         tool="Write")
    assert _node_dedup_survivor(lane_unknown, known) == _WINNER_VIEW


def test_dedup_vocabulary_treats_synthetic_as_unattributed():
    """The <synthetic> member of the shared vocabulary (issue #563): a
    harness-fabricated stub names no model, so a real-model copy beats it
    in both orders -- exactly like the unknown fallbacks -- and its events
    leave with it."""
    synthetic = _claude_line("u-1", "<synthetic>", "synthetic copy", 200)
    known = _claude_line("u-1", "claude-sonnet-4-5", "attributed copy", 300,
                         tool="Write")
    assert _node_dedup_survivor(synthetic, known) == _WINNER_VIEW
    assert _node_dedup_survivor(known, synthetic) == _WINNER_VIEW


def test_dedup_vocabulary_synthetic_vs_unknown_keeps_the_first():
    """Both unattributed: <synthetic> is a MEMBER of the unattributed
    class, not a special case -- no attribution upgrade either way, the
    first copy in file order wins, and the drop pass keeps it (the
    verdict ends on false). The synthetic copy emits no assistant_usage
    meta (the parse gate skips synthetic stubs), so its survival shows in
    its events alone."""
    synthetic = _claude_line("u-1", "<synthetic>", "synthetic copy", 200)
    unknown = _claude_line("u-1", None, "copy with no model", 300)
    first = {"toolCalls": 0, "toolNames": [], "texts": ["synthetic copy"],
             "usage": []}
    second = {"toolCalls": 0, "toolNames": [], "texts": ["copy with no model"],
              "usage": [{"model": None, "output": 300}]}
    assert _node_dedup_survivor(synthetic, unknown) == first
    assert _node_dedup_survivor(unknown, synthetic) == second


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
      require({str(LOADER_JS)!r});
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


# --------------------------------------------------------------------------
# Prompt gate (issue #213, SV-PARSER-SPEC): XML-wrapped harness injections
# are not prompts, in the backend AND the browser.
# --------------------------------------------------------------------------

# fixture name -> (lines that ARE prompts, lines that are injections)
PROMPT_GATE_FIXTURES = {
    "prompt_xml_injection.jsonl": ([1, 8], [3, 5, 6, 9]),
    "prompt_pasted_content_keeps.jsonl": ([1, 2], []),
    "prompt_unknown_xml_tag.jsonl": ([3], [1, 2]),
    "prompt_xml_midtext.jsonl": ([1, 3, 5], []),
}


def test_parser_js_prompt_gate_matches_backend():
    """The browser drops the same user texts the backend denies: on each
    fixture the user_message lines are exactly the backend's prompt lines
    (so no injection line emits a user_message event and the real prompts
    still emit), and computeSessionStats' userMsgs equals the backend
    prompt_count. Also pins the interrupt-marker exclusion the backend
    already had (R3) on interrupt_list_content.jsonl, whose marker rides
    a list-content text block — the browser never pushed it as a prompt
    before this gate existed, so parity needed it too. One node run over
    every fixture, batched for speed."""
    for prompt_lines, injection_lines in PROMPT_GATE_FIXTURES.values():
        assert not set(prompt_lines) & set(injection_lines)
    names = [*PROMPT_GATE_FIXTURES, "interrupt_list_content.jsonl"]
    texts = {name: (ROOT / "fixtures" / "parser" / name).read_text(
        encoding="utf-8") for name in names}
    script = f"""
      global.window = {{}};
      require({str(LOADER_JS)!r});
      require({str(PARSER_JS)!r});
      const fixtures = {json.dumps(texts)};
      const out = {{}};
      for (const [name, text] of Object.entries(fixtures)) {{
        const {{ events, meta }} = window.parseTranscript(text);
        out[name] = {{
          userMsgs: window.computeSessionStats(events, meta).userMsgs,
          lines: events.filter(e => e.type === 'user_message')
                       .map(e => e.line),
        }};
      }}
      console.log(JSON.stringify(out));
    """
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60,
        # Return code checked by hand on the next line.
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout)
    for name, (prompt_lines, _injection_lines) in PROMPT_GATE_FIXTURES.items():
        backend = parse.parse_file(
            f"k/s/{name}",
            (ROOT / "fixtures" / "parser" / name).read_bytes(),
        )["prompt_count"]
        assert got[name]["userMsgs"] == backend, name
        assert got[name]["lines"] == prompt_lines, name
    interrupt = parse.parse_file(
        "k/s/interrupt_list_content.jsonl",
        (ROOT / "fixtures" / "parser" / "interrupt_list_content.jsonl").read_bytes(),
    )
    assert got["interrupt_list_content.jsonl"]["userMsgs"] == (
        interrupt["prompt_count"])
    assert got["interrupt_list_content.jsonl"]["lines"] == [1]


# --------------------------------------------------------------------------
# Offset-less timestamps are UTC (issue #376, SV-PARSER-SPEC): the
# backend stamps a naive ISO timestamp UTC at parse (parse_common._to_dt);
# the browser must read the same text the same way, or the Inspector
# prices and displays a different instant than the database stores.
# --------------------------------------------------------------------------

_NAIVE_TS_LINES = [
    {"type": "user", "timestamp": "2026-09-10T00:29:00", "uuid": "u",
     "message": {"role": "user", "content": "hi"}},
    {"type": "assistant", "timestamp": "2026-09-10 00:30:00", "uuid": "a",
     "requestId": "req-376",
     "message": {"id": "msg_376", "role": "assistant",
                 "model": "claude-opus-4-7",
                 "content": [{"type": "text", "text": "naive"}],
                 "stop_reason": "end_turn",
                 "usage": {"input_tokens": 10, "output_tokens": 1,
                           "cache_creation_input_tokens": 0,
                           "cache_read_input_tokens": 0}}},
]

# Synthetic rates (SV-TEST-DATA): a window ending at the cutover prices
# the naive text's LOCAL reading (22:30Z the day before, in Europe/
# Berlin) differently from its UTC reading (00:30Z), so a cost equality
# below cannot pass by accident.
_CHEAP_JS_376 = {"fresh": 0.5, "c5": 0.625, "c1h": 1.0, "read": 0.05,
                 "out": 2.5}
_OPUS_JS_376 = {"fresh": 9.0, "c5": 11.25, "c1h": 18.0, "read": 0.9,
                "out": 45.0}


def test_parser_js_reads_an_offset_less_timestamp_as_utc():
    """parseTranscript stamps a naive ISO timestamp UTC at capture (the
    T-form and, since Python's fromisoformat reads it naive too, the
    space-separated form), and the stamped record prices exactly like
    the explicit-Z spelling — under a non-UTC node zone, where an
    unstamped string would read 22:30Z."""
    script = f"""
      global.window = {{}};
      require({str(LOADER_JS)!r});
      require({str(PARSER_JS)!r});
      window.datedRates['claude-opus-4-7'] = [
        {{ endExclusive: Date.parse('2026-09-10T00:00:00Z'),
           rates: {json.dumps(_CHEAP_JS_376)} }}];
      window.modelRates['claude-opus-4-7'] = {json.dumps(_OPUS_JS_376)};
      const text = {json.dumps("\n".join(json.dumps(line) for line in _NAIVE_TS_LINES))};
      const {{ events, meta }} = window.parseTranscript(text);
      const usage = meta.find((m) => m.type === 'assistant_usage');
      // The explicit-Z spelling of the fixture's raw wall clock, built
      // here as a literal — never from the stamped string, so the
      // equality below compares two independent readings of one
      // instant (appending to an already-stamped ts would be the
      // identity and prove nothing).
      const zTwins = meta.map((m) => ({{ ...m, ts: '2026-09-10T00:30:00Z' }}));
      console.log(JSON.stringify({{
        firstEventTs: events.find((e) => e.ts).ts,
        usageTs: usage.ts,
        cost: window.computeSessionStats([], [usage]).cost,
        zCost: window.computeSessionStats([], zTwins).cost,
      }}));
    """
    env = {**os.environ, "TZ": "Europe/Berlin"}
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60,
        env=env, check=False,  # Return code checked by hand on the next line.
    )
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout)
    assert got["usageTs"] == "2026-09-10 00:30:00Z", (
        "the capture must stamp the naive string UTC, spelling preserved")
    assert got["firstEventTs"] == "2026-09-10T00:29:00Z", (
        "every event rides the stamped ts, display included")
    assert got["cost"] == got["zCost"], (
        "the naive text must price as its UTC reading, not the viewer's "
        "local one")
