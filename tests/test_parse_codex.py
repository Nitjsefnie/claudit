from datetime import datetime, timedelta, timezone
from pathlib import Path

import json
import pytest

from backend import parse, pricing
from backend.codex_fork import head_scan
from backend.parse_codex import _codex_first_declared_model, _codex_model


FIX = Path(__file__).resolve().parents[1] / "fixtures" / "codex"
KIMI_FIX = Path(__file__).resolve().parents[1] / "fixtures" / "parser"


def _parse_file_norefusal(file_key: str, blob: bytes):
    """parse_file minus the model-declaration refusal: the token-math tests parse
    the no-model fork fixture, which the entry point refuses."""
    fmt = parse.sniff_format(blob)
    return parse.to_claudit(parse.LANE_PARSERS[fmt](file_key, blob), fmt)


def _parse(name):
    return _parse_file_norefusal(f"codex/{name}", (FIX / name).read_bytes())


def _billed_input(rec):
    """The three input buckets a record splits its prompt into."""
    return (rec["fresh_tokens"] + rec["cache_creation_tokens"]
            + rec["cache_read_tokens"])


# --------------------------------------------------------------------------
# Format detection
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name,expected", [
    ("rollout_web_search.jsonl", [1, None]),
    ("rollout_model_switch.jsonl", [None, None, None, None, None]),
], ids=["search-call-once-per-reply", "no-search-calls"])
def test_codex_search_call_item_is_counted_once_per_reply(name, expected):
    out = _parse(name)
    assert [record["web_search_requests"] for record in out["records"]] == expected


def test_codex_web_search_end_without_response_item_stays_null():
    usage = {
        "input_tokens": 100, "cached_input_tokens": 0,
        "cache_write_input_tokens": 0, "output_tokens": 20,
        "reasoning_output_tokens": 0, "total_tokens": 120,
    }
    lines = [
        {"timestamp": "2026-06-14T12:00:00Z", "type": "session_meta",
         "payload": {"id": "00000000-0000-4000-8000-0000000000f2",
                     "session_id": "00000000-0000-4000-8000-0000000000f2"}},
        {"timestamp": "2026-06-14T12:00:01Z", "type": "turn_context",
         "payload": {"model": "gpt-5.6-sol"}},
        {"timestamp": "2026-06-14T12:00:02Z", "type": "event_msg",
         "payload": {"type": "web_search_end", "call_id": "ws_synth_1",
                     "query": "synthetic query", "action": "search"}},
        {"timestamp": "2026-06-14T12:00:03Z", "type": "event_msg",
         "payload": {"type": "token_count", "info": {
             "total_token_usage": usage, "last_token_usage": usage}}},
    ]
    blob = b"".join(json.dumps(line).encode() + b"\n" for line in lines)

    out = parse.parse_file("codex/web_search_end_only.jsonl", blob)

    assert len(out["records"]) == 1
    assert out["records"][0]["web_search_requests"] is None


def test_a_rollout_without_a_session_meta_header_is_still_detected():
    """The patch fixtures model a window out of the middle of a rollout —
    no session_meta, no turn_context. Detection has to work off the record
    types themselves, because a resumed session's tail looks like this."""
    out = _parse("rollout_patch_linked.jsonl")
    assert out["tool_uses"]
    assert out["records"]


# --------------------------------------------------------------------------
# Trap 1 — a fork inherits its parent's cumulative counter
# --------------------------------------------------------------------------
# fixtures/codex/rollout_fork_prefix.jsonl is a forked thread: its
# session_meta carries forked_from_id and agent_path. Its first token_count
# (line 5) reports
#   total_token_usage.total_tokens = 40,100,500
#   last_token_usage.total_tokens  =      100,500
# so 40,000,000 of that total was run up by the PARENT before this file
# existed. Only the 100,500 belongs to this file's first request.


FORK_FIRST_REQUEST = {          # line 5's last_token_usage, verbatim
    "input_tokens": 100_000,
    "cached_input_tokens": 98_000,
    "output_tokens": 500,
}
FORK_INHERITED_TOTAL = 40_000_000
FORK_FINAL_CUMULATIVE = 42_458_400   # line 46's total_token_usage
FORK_SUM_INPUT = 2_450_000           # sum of the 18 real input deltas
FORK_SUM_CACHED = 2_400_000
FORK_SUM_OUTPUT = 8_400
FORK_SUM_FRESH = 50_000              # = input - cached, no cache writes here


def test_a_forks_inherited_total_never_becomes_a_billed_record():
    """The first record is the request that produced the first snapshot,
    not the snapshot. Charging the snapshot would bill this file for
    40M tokens of its parent's work."""
    out = _parse("rollout_fork_prefix.jsonl")
    first = out["records"][0]
    assert first["line_num"] == 5
    assert _billed_input(first) == FORK_FIRST_REQUEST["input_tokens"]
    assert first["output_tokens"] == FORK_FIRST_REQUEST["output_tokens"]
    assert _billed_input(first) < FORK_INHERITED_TOTAL / 100


def test_every_forked_record_is_a_plausible_single_request():
    """A leaked cumulative snapshot shows up as one record far larger than
    the model's context window, which no single request can be."""
    out = _parse("rollout_fork_prefix.jsonl")
    assert out["records"]
    for rec in out["records"]:
        assert 0 < _billed_input(rec) < 1_000_000


# --------------------------------------------------------------------------
# Trap 2 — duplicate token_count events
# --------------------------------------------------------------------------
# The fork fixture holds 20 token_count events. Line 35 (the one a
# compaction emits, with last_token_usage zeroed) and line 44 both repeat
# the previous cumulative snapshot: no request happened between them.


def test_duplicate_token_count_events_do_not_double_count():
    out = _parse("rollout_fork_prefix.jsonl")
    assert len(out["records"]) == 18, "20 token_count events, 2 of them repeats"
    booked = {r["line_num"] for r in out["records"]}
    assert 35 not in booked
    assert 44 not in booked


def test_a_repeated_snapshot_adds_no_tokens():
    """Differencing is what drops the repeats, so the totals must equal the
    totals of the non-repeated events alone."""
    out = _parse("rollout_fork_prefix.jsonl")
    assert sum(_billed_input(r) for r in out["records"]) == FORK_SUM_INPUT
    assert sum(r["output_tokens"] for r in out["records"]) == FORK_SUM_OUTPUT


# --------------------------------------------------------------------------
# Trap 3 — cached_input_tokens is a subset of input_tokens
# --------------------------------------------------------------------------


def test_cached_tokens_are_subtracted_from_fresh_not_added_to_it():
    """Line 5's request reads 98,000 of its 100,000 input tokens from
    cache. Fresh input is the 2,000 that were not cached; adding the two
    instead would report 198,000."""
    out = _parse("rollout_fork_prefix.jsonl")
    first = out["records"][0]
    assert first["cache_read_tokens"] == FORK_FIRST_REQUEST["cached_input_tokens"]
    assert first["fresh_tokens"] == (
        FORK_FIRST_REQUEST["input_tokens"]
        - FORK_FIRST_REQUEST["cached_input_tokens"]
    )
    assert first["fresh_tokens"] == 2000


def test_the_three_input_buckets_partition_the_prompt():
    """fresh + cache_creation + cache_read is the WHOLE prompt, for every
    record — the invariant that makes ctx_input the context size."""
    out = _parse("rollout_fork_prefix.jsonl")
    for rec in out["records"]:
        assert _billed_input(rec) == rec["ctx_input"]
        assert rec["fresh_tokens"] >= 0
    assert sum(r["fresh_tokens"] for r in out["records"]) == FORK_SUM_FRESH
    assert sum(r["cache_read_tokens"] for r in out["records"]) == FORK_SUM_CACHED


# --------------------------------------------------------------------------
# Trap 4 — model attribution comes from the preceding turn_context
# --------------------------------------------------------------------------
# fixtures/codex/rollout_model_switch.jsonl is a window across a model
# switch: thread_settings_applied names gpt-5.6-sol through line 28 and
# gpt-5.6-terra from line 29, with token_count records on both sides. The
# token_count payloads themselves name no model at all.
SWITCH_LINE = 29


def test_token_count_payloads_carry_no_model_of_their_own():
    """The premise of the whole attribution rule, asserted against the
    fixture rather than taken on trust."""
    seen = 0
    for raw in (FIX / "rollout_model_switch.jsonl").read_bytes().splitlines():
        payload = json.loads(raw).get("payload") or {}
        if payload.get("type") != "token_count":
            continue
        seen += 1
        assert "model" not in payload
        assert "model" not in (payload.get("info") or {})
    assert seen == 5


def test_records_take_the_model_in_force_when_the_request_happened():
    out = _parse("rollout_model_switch.jsonl")
    assert len(out["records"]) == 5
    by_line = {r["line_num"]: r["model"] for r in out["records"]}
    before = {ln: m for ln, m in by_line.items() if ln < SWITCH_LINE}
    after = {ln: m for ln, m in by_line.items() if ln > SWITCH_LINE}
    assert before and after, (
        f"fixture must straddle the switch at line {SWITCH_LINE}")
    assert set(before.values()) == {"gpt-5.6-sol"}
    assert set(after.values()) == {"gpt-5.6-terra"}


def test_a_session_that_switches_model_is_priced_at_both_rates():
    """One label per session would bill the terra requests at sol's 2.5x
    rates, or the reverse."""
    out = _parse("rollout_model_switch.jsonl")
    for rec in out["records"]:
        expected = pricing.compute_cost(
            rec["model"],
            fresh=rec["fresh_tokens"], output=rec["output_tokens"],
            eph5=0, eph1h=0, unsplit_create=rec["cache_creation_tokens"],
            read=rec["cache_read_tokens"],
            ts=rec["ts"],
        )
        assert rec["cost_usd"] == pytest.approx(round(expected, 6), rel=1e-9)
    models = {r["model"] for r in out["records"]}
    assert models == {"gpt-5.6-sol", "gpt-5.6-terra"}


@pytest.mark.parametrize("name,columns,expected", [
    ("rollout_sole_model_prefix.jsonl", ("line_num", "model"), [
        (8, "gpt-5.6-sol"), (10, "gpt-5.6-sol"), (11, "gpt-5.6-sol"),
        (12, "gpt-5.6-sol"), (18, "gpt-5.6-sol"),
    ]),
    ("rollout_model_switch.jsonl", (), None),
    ("rollout_fork_model_switch.jsonl", ("line_num", "model", "uuid"), [
        (4, "gpt-5.6-sol", "00000000-0000-4000-8000-000000000001:100500"),
        (5, "gpt-5.6-sol", "00000000-0000-4000-8000-000000000001:110920"),
        (10, "gpt-5.6-sol", "00000000-0000-4000-8000-000000000001:215540"),
        (13, "gpt-5.6-terra", "00000000-0000-4000-8000-000000000001:316040"),
    ]),
], ids=["sole-late-declaration", "first-declaration-in-file-order",
        "multi-model-fork"])
def test_first_declaration_backfills_the_prefix(name, columns, expected):
    blob = (FIX / name).read_bytes()
    assert _codex_first_declared_model(blob) == "gpt-5.6-sol"
    if expected is not None:
        out = _parse(name)
        assert [tuple(r[column] for column in columns)
                for r in out["records"]] == expected


def test_a_file_declaring_no_model_is_refused_not_stored_as_unknown():
    """The fork fixture declares none. Nothing here derives the model in
    force, and the ruling refuses a placeholder: the parser
    invents no attribution the transcript does not carry, and ingest
    fails loudly on the file instead of storing `unknown` rows."""
    blob = (FIX / "rollout_fork_prefix.jsonl").read_bytes()
    assert _codex_first_declared_model(blob) is None
    with pytest.raises(ValueError, match="rollout_fork_prefix"):
        parse.parse_file("codex/rollout_fork_prefix.jsonl", blob)


@pytest.mark.parametrize("raw,expected", [
    ("gpt-5.6-sol", "gpt-5.6-sol"),
    ("gpt-5.6-terra", "gpt-5.6-terra"),
    ("gpt5.6-sol", "gpt-5.6-sol"),        # real: one turn_context spells it so
    ("GPT-5.6-Terra", "gpt-5.6-terra"),
    ("gpt-5.6-luna-preview", "gpt-5.6-luna-preview"),
    # Sol 6.1 is the id that hid as GPT-5.6 Sol until the relabelling map
    # went: it now survives verbatim and prices at its own row.
    ("gpt-6.1-sol", "gpt-6.1-sol"),
    ("gpt-6-astra", "gpt-6-astra"),
    ("GPT-6-Astra", "gpt-6-astra"),
    ("gpt-7-unreleased", "gpt-7-unreleased"),
    (None, None),
    ("", None),
    ("  ", None),
])
def test_model_ids_stay_as_recorded_after_spelling_normalisation(raw, expected):
    assert _codex_model(raw) == expected


def test_the_parser_never_labels_a_model_unknown():
    """The ruling: `unknown` is a placeholder for a model
    that was never actually unknown, so no parser may emit it — a file
    that cannot attribute is refused instead. Pin it on every lane
    parser at their shared entry point."""
    # rollout_patch_linked.jsonl is in the sweep because it is the one
    # fixture that produces tool_uses rows — without it the tool_uses
    # half below never runs.
    for name in ("rollout_model_switch.jsonl", "rollout_fork_model_switch.jsonl",
                 "rollout_sole_model_prefix.jsonl", "rollout_patch_linked.jsonl"):
        out = _parse(name)
        for r in out["records"]:
            assert r["model"] != "unknown" and r["model"], name
        for tu in out["tool_uses"]:
            assert tu["model"] != "unknown" and tu["model"], name


def test_a_sol_6_1_rollout_keeps_its_recorded_model_and_prices_it():
    """Keep the declared gpt-6.1-sol id verbatim and resolve its own exact row.
    A shorter model id contained inside it must not capture attribution or price."""
    out = _parse("rollout_sol_6_1.jsonl")
    assert [r["model"] for r in out["records"]] == ["gpt-6.1-sol", "gpt-6.1-sol"]
    for rec in out["records"]:
        resolution = pricing.resolve(rec["model"], rec["ts"])
        assert resolution.kind == "exact" and not resolution.estimated
        assert resolution.key == "gpt-6-1-sol"
        assert rec["cost_usd"] == pytest.approx(round(pricing.compute_cost(
            rec["model"],
            fresh=rec["fresh_tokens"], output=rec["output_tokens"],
            eph5=0, eph1h=0, unsplit_create=rec["cache_creation_tokens"],
            read=rec["cache_read_tokens"], ts=rec["ts"],
        ), 6), rel=1e-9)


# --------------------------------------------------------------------------
# Token types — count what the format carries, invent nothing
# --------------------------------------------------------------------------


def test_reasoning_tokens_are_carried_not_dropped():
    """Codex breaks its output down into reasoning and the rest; claudit
    stores the count as thinking_tokens (a subset of output_tokens)."""
    out = _parse("rollout_fork_prefix.jsonl")
    assert any(r["thinking_tokens"] > 0 for r in out["records"])
    # Line 5's last_token_usage.reasoning_output_tokens, verbatim.
    assert out["records"][0]["thinking_tokens"] == 300


def test_a_kimi_wire_reports_a_truthful_zero_rather_than_an_invented_count():
    """The Kimi formats carry no reasoning breakdown. The column exists for
    them, and 0 is the true value — not a placeholder."""
    out = parse.parse_file(
        "sessions/projA/sess-A/wire.jsonl",
        (KIMI_FIX / "kimi_code.jsonl").read_bytes(),
    )
    assert out["records"]
    assert all(r["thinking_tokens"] == 0 for r in out["records"])


def test_cache_write_tokens_are_billed_on_their_own_meter(monkeypatch):
    """No corpus record has a cache write; synthetic rows pin its meter.
    Cache writes bill at 1.25x fresh and use the 1h bucket."""
    assert all(r["cache_creation_tokens"] == 0
               for r in _parse("rollout_fork_prefix.jsonl")["records"])
    model, cutover = "acme/cache-write-9", datetime(2030, 1, 1, tzinfo=timezone.utc)
    rates = dict(zip(pricing.RATE_FIELDS, (8, 10, 10, 0.8, 40)))
    old_rates = dict(zip(pricing.RATE_FIELDS, (4, 5, 5, 0.4, 20)))
    monkeypatch.setitem(pricing.MODEL_RATES, model, rates)
    monkeypatch.setitem(pricing.DATED_RATES, model, [(cutover, old_rates)])
    assert rates["create_1h"] == pytest.approx(rates["fresh"] * 1.25)
    assert rates["create_1h"] > rates["fresh"] > rates["read"]
    write_only = pricing.compute_cost(
        model, fresh=0, output=0,
        eph5=0, eph1h=0, unsplit_create=1_000_000, read=0)
    assert write_only == pytest.approx(rates["create_1h"], rel=1e-9)
    old = pricing.rate_for(model, cutover - timedelta(seconds=1))
    assert old["create_1h"] == pytest.approx(old["fresh"] * 1.25)


def _with_cache_write(name: str, written: int) -> bytes:
    """A fixture with cache_write_input_tokens set on its LAST
    token_count, and nothing else touched.

    Built here rather than baked into the fixture, because the fixtures
    mirror what the format actually emits and cache writes are 0 across
    all 30,249 token_count payloads measured on the reference corpus — a
    fixture carrying one would misrepresent the format. The field is real
    and billable all the same — declared and summed in
    codex-rs/protocol/src/protocol.rs — so the arithmetic needs a test
    that the corpus cannot supply.

    Only the cumulative counter is edited, exactly as the format would
    report it: the counter is monotonic, so a write shows up as the
    cumulative cache_write advancing while input_tokens keeps its own
    total. The subset invariant is preserved — the written tokens are taken
    OUT of the same prompt, never added on top.
    """
    lines = (FIX / name).read_bytes().splitlines()
    last_idx = None
    for i, raw in enumerate(lines):
        obj = json.loads(raw)
        if (obj.get("payload") or {}).get("type") == "token_count":
            last_idx = i
    assert last_idx is not None
    obj = json.loads(lines[last_idx])
    total = obj["payload"]["info"]["total_token_usage"]
    assert total["cache_write_input_tokens"] == 0
    total["cache_write_input_tokens"] = written
    lines[last_idx] = json.dumps(obj).encode()
    return b"\n".join(lines) + b"\n"


def _uncached_headroom(name: str) -> int:
    """Fresh input on the record the helper above edits.

    A cache write is taken OUT of the uncached part of that request, so a
    test may not write more than there is — past it the parser clamps fresh
    at zero, which is correct behaviour for impossible data but not what
    these tests are about.
    """
    return _parse(name)["records"][-1]["fresh_tokens"]


def test_cache_writes_come_out_of_fresh_input_not_on_top_of_it():
    """fresh = input - cached - written. Folding the written tokens into
    fresh underbills them (they cost 1.25x uncached input, not 1x);
    adding them on top double-counts the prompt."""
    written = _uncached_headroom("rollout_fork_prefix.jsonl") // 2
    assert written > 0
    blob = _with_cache_write("rollout_fork_prefix.jsonl", written)
    out = _parse_file_norefusal("codex/cache_write.jsonl", blob)
    rec = out["records"][-1]
    assert rec["cache_creation_tokens"] == written
    # The prompt is unchanged: the write came out of it, not on top.
    assert rec["ctx_input"] == (
        rec["fresh_tokens"] + rec["cache_creation_tokens"]
        + rec["cache_read_tokens"])
    plain = _parse("rollout_fork_prefix.jsonl")["records"][-1]
    assert rec["ctx_input"] == plain["ctx_input"]
    assert rec["fresh_tokens"] == plain["fresh_tokens"] - written


# --------------------------------------------------------------------------
# Pricing
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Tool calls and line churn
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Turns, context growth, rate limits
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Return-shape contract — unchanged from the Kimi formats
# --------------------------------------------------------------------------


RECORD_KEYS = {
    "file_key", "line_num", "uuid", "request_id", "ts", "model",
    "fresh_tokens", "cache_creation_tokens", "cache_read_tokens",
    "cache_write_tokens",
    "output_tokens", "thinking_tokens", "long_context", "cost_usd",
    "text_chars", "reply_latency_s", "ctx_input", "stop_reason", "effort",
    "generation_s", "output_tokens_per_s",
    "cli_version", "turn_flags", "turn_tool_results", "eph5_tokens",
    "eph1h_tokens", "web_search_requests", "is_replay",
}
TOOL_USE_KEYS = {
    "file_key", "line_num", "idx", "ts", "tool_name", "model", "tool_use_id",
    "is_error", "error_kind", "error_text", "lines_added", "lines_deleted",
    "agent_type", "agent_model", "dispatch_prompt_chars",
    "dispatch_brief_ref", "dispatch_name", "result_chars", "read_kind",
    "read_targets",
    "write_targets", "is_reread", "is_replay",
}


# --------------------------------------------------------------------------
# Shell churn inside the JS program
# --------------------------------------------------------------------------

def test_the_fork_head_advances_and_counts_like_the_parse_loop():
    """Two codex_fork.head_scan edges ( delta round): a line
    that merely MENTIONS the session_meta needle (here a JSON string
    value) advances - the first session_meta line decides - and the
    boundary counts lines the way the parse loop's splitlines() does, so
    a lone CR inside an earlier line cannot push a replayed row past it."""
    meta = b'{"timestamp":"t","type":"session_meta","payload":{"session_id":"s",'
    meta += b'"id":"t1","forked_from_id":"t0"}}'
    mention = (b'{"timestamp":"t","type":"event_msg","payload":{"type":'
               b'"agent_message","message":"roles: [\"session_meta\"]"}}')
    cr_line = (b'{"timestamp":"t","type":"event_msg","payload":{"type":'
               b'"token_count","info":{}}}\r{"tail"}\n')
    decl = b'{"timestamp":"t","type":"turn_context","payload":{"model":"gpt-5.6-terra"}}'
    blob = mention + b"\n" + meta + b"\n" + cr_line + decl + b"\n"
    first_model, is_fork, declared_at = head_scan(blob)
    assert is_fork is True          # the mention advanced; the meta decided
    assert first_model == "gpt-5.6-terra"
    # splitlines numbering: the CR line is lines 3-4, the declaration line 5.
    assert declared_at == 5
