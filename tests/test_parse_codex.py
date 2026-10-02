"""parse.py — per-file extraction from Codex rollout JSONL.

Every fixture under fixtures/codex is SYNTHETIC: generated, never
captured. What each one carries, and the invariants an edit to one has to
preserve, are in fixtures/codex/README.md.

Generated data is only worth what it still catches, so the fixtures
reproduce the format's grammar exactly — record types, payload
discriminators, field names — and keep the numeric relationships that
make the naive token sum wrong: a cumulative counter opened at an
inherited 40M, duplicate snapshots that repeat it verbatim, a ~98%
cache-read rate, and model declarations that sit between the requests
they govern. Each of the four sections below fails if the parser stops
honouring one of those, which is the property the corpus exists for.

The four assertions the format demands, each with its own section below:

  1. a forked thread's inherited cumulative total does not inflate the count
  2. duplicate token_count events do not double-count
  3. cached tokens are SUBTRACTED from input, never added
  4. model attribution follows the preceding turn_context

Expected numbers are read off the fixtures' own raw fields and quoted in
the test that uses them, so a failure says which field moved.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import json
import pytest

from backend import parse, pricing
from backend.parse_codex import _codex_declared_models, _codex_model


FIX = Path(__file__).resolve().parents[1] / "fixtures" / "codex"
KIMI_FIX = Path(__file__).resolve().parents[1] / "fixtures" / "parser"


def _parse(name):
    return parse.parse_file(f"codex/{name}", (FIX / name).read_bytes())


def _billed_input(rec):
    """The three input buckets a record splits its prompt into."""
    return (rec["fresh_tokens"] + rec["cache_creation_tokens"]
            + rec["cache_read_tokens"])


# --------------------------------------------------------------------------
# Format detection
# --------------------------------------------------------------------------


def test_a_rollout_file_is_parsed_as_codex_not_as_a_kimi_wire():
    """Codex records carry a "timestamp" like legacy kimi-cli ones, so a
    detector that checks the kimi rungs first silently returns an empty
    parse instead of failing."""
    out = _parse("rollout_model_switch.jsonl")
    assert out["records"], "no records: the file fell through to a kimi parser"
    assert {r["model"] for r in out["records"]} <= {
        "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"
    }


def test_detection_still_routes_kimi_code_wires_to_the_kimi_parser():
    """The Codex rung is additive: it must not capture the other formats."""
    out = parse.parse_file(
        "sessions/projA/sess-A/wire.jsonl",
        (KIMI_FIX / "kimi_code.jsonl").read_bytes(),
    )
    assert out["records"]
    assert all(r["model"].startswith("kimi-") for r in out["records"])


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
#
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


def test_the_forks_billed_total_is_the_sum_of_deltas_not_of_finals():
    """Summing this file's final cumulative counter — the obvious wrong
    answer — charges 42.5M tokens for 2.46M of real work, a 17x error."""
    out = _parse("rollout_fork_prefix.jsonl")
    billed = sum(_billed_input(r) + r["output_tokens"] for r in out["records"])
    assert billed == FORK_SUM_INPUT + FORK_SUM_OUTPUT
    assert billed < FORK_FINAL_CUMULATIVE / 15


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
#
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


def test_adding_the_cache_instead_of_subtracting_would_inflate_fresh_input():
    """At this corpus's ~98% cache rate the wrong sign is not a rounding
    error: it reports ~97x the fresh input actually billed."""
    out = _parse("rollout_fork_prefix.jsonl")
    fresh = sum(r["fresh_tokens"] for r in out["records"])
    wrong = sum(_billed_input(r) + r["cache_read_tokens"] for r in out["records"])
    assert wrong > fresh * 20


# --------------------------------------------------------------------------
# Trap 4 — model attribution comes from the preceding turn_context
# --------------------------------------------------------------------------
#
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


def test_records_before_the_first_declaration_take_the_files_sole_model():
    """A fork replays history before the new thread declares a model, so
    the leading requests have no turn_context in front of them. Where the
    file declares exactly one model there is only one answer.

    NOTE: this fixture declares gpt-5.6-sol, which is also the
    unattributed fallback, so the LABEL alone cannot tell the two paths
    apart — the declared-model set is asserted separately below, and that
    is what distinguishes them.
    """
    blob = (FIX / "rollout_sole_model_prefix.jsonl").read_bytes()
    assert _codex_declared_models(blob) == {"gpt-5.6-sol"}
    out = _parse("rollout_sole_model_prefix.jsonl")
    assert [r["line_num"] for r in out["records"]] == [8, 10, 11, 12, 18]
    # The only declaration in this window is at line 20, after all five.
    assert all(r["model"] == "gpt-5.6-sol" for r in out["records"])


def test_a_file_declaring_no_model_bills_unknown_rather_than_inventing_one():
    """The fork fixture declares none. An unattributed record still has to
    be billed — as `unknown` at the default (estimated) rates: the parser
    invents no attribution the transcript does not carry (issue #471)."""
    blob = (FIX / "rollout_fork_prefix.jsonl").read_bytes()
    assert _codex_declared_models(blob) == set()
    out = _parse("rollout_fork_prefix.jsonl")
    assert all(r["model"] == "unknown" for r in out["records"])
    assert pricing.resolve("unknown").estimated is True


def test_a_file_declaring_several_models_reports_all_of_them():
    blob = (FIX / "rollout_model_switch.jsonl").read_bytes()
    assert _codex_declared_models(blob) == {"gpt-5.6-sol", "gpt-5.6-terra"}


def test_a_multi_model_fork_keeps_its_replayed_prefix_unknown():
    """The issue-529 shape: a fork that replays its parent's history and
    THEN declares two models of its own. No declaration fronts the
    replayed prefix and two candidates follow it, so no sole-model answer
    exists — the prefix stays `unknown` and the records it shares with the
    parent keep the file-local uuids that let the dedup pick the parent's
    attributed copy (SV-CANONICAL-FLAG)."""
    blob = (FIX / "rollout_fork_model_switch.jsonl").read_bytes()
    assert _codex_declared_models(blob) == {"gpt-5.6-sol", "gpt-5.6-terra"}
    out = _parse("rollout_fork_model_switch.jsonl")
    assert [(r["line_num"], r["model"], r["uuid"]) for r in out["records"]] == [
        (4, "unknown", "00000000-0000-4000-8000-000000000001:100500"),
        (5, "unknown", "00000000-0000-4000-8000-000000000001:110920"),
        (10, "gpt-5.6-sol", "00000000-0000-4000-8000-000000000001:215540"),
        (13, "gpt-5.6-terra", "00000000-0000-4000-8000-000000000001:316040"),
    ]


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
    ("gpt-7-unreleased", "gpt-7-unreleased"),  # unknown stays itself, estimated
    (None, "unknown"),
])
def test_model_ids_stay_as_recorded_after_spelling_normalisation(raw, expected):
    assert _codex_model(raw) == expected


def test_a_sol_6_1_rollout_keeps_its_recorded_model_and_prices_it():
    """Issue #471: a gpt-6.1-sol rollout kept reading as gpt-5.6-sol (the
    first listed id whose needle the id contains) and billing at twice its
    price. The id survives verbatim and resolves EXACTLY at its own
    pricing.json row."""
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
# Trap 5 — the same request is journalled in several files
# --------------------------------------------------------------------------
#
# A resumed or forked rollout replays its parent's history into its own
# file. Per-file parsing is right either way, but ingest sums ACROSS files,
# and 28,889 per-file records over the local corpus represent only 12,945
# distinct requests. The dedup mechanism already exists — records.uuid plus
# is_canonical, resolved by ingest.recompute_canonical — and what it needs
# from the parser is a uuid identical across every file replaying a request.


# The fork fixture's session_meta declares TWO ids: its own (`id`) and the
# thread's (`session_id`), which is its PARENT's. That is not a discrepancy
# to paper over; it is the whole mechanism. The thread id is shared by every
# file of a fork family, so it is the half of the identity that survives a
# replay — and the file's own id, which a real rollout also carries in its
# filename, is the half that must NOT reach the uuid.
FORK_THREAD_ID = "00000000-0000-4000-8000-000000000001"
FORK_FILE_ID = "00000000-0000-4000-8000-000000000002"


def test_a_records_uuid_identifies_the_request_not_the_line_it_sits_on():
    """<file_key>:<line> can never dedup: the same request lands on a
    different line in every file that replays it."""
    out = _parse("rollout_fork_prefix.jsonl")
    for rec in out["records"]:
        assert rec["uuid"].startswith(FORK_THREAD_ID + ":")
        assert rec["file_key"] not in rec["uuid"]
    # Distinct requests keep distinct identities within the file.
    assert len({r["uuid"] for r in out["records"]}) == len(out["records"])


def test_the_uuid_is_the_threads_id_and_the_counter_position():
    """Both halves are load-bearing: the counter position alone would
    collide across unrelated threads, the thread id alone across requests.

    The thread id is the one session_meta declares, NOT the file's own id
    (which a real rollout repeats in its filename) — a fork reuses its
    parent's, and a uuid keyed on the file would make every replayed
    request look distinct again.
    """
    out = _parse("rollout_fork_prefix.jsonl")
    first = out["records"][0]
    # Line 5's total_token_usage.total_tokens, verbatim.
    assert first["uuid"] == f"{FORK_THREAD_ID}:40100500"
    assert FORK_FILE_ID not in first["uuid"]


def test_a_forks_replayed_records_collide_with_their_originals_by_uuid():
    """Two sibling forks share a session id and a baseline, so the
    requests they both replay must produce EQUAL uuids — that equality is
    what recompute_canonical collapses."""
    fork = _parse("rollout_fork_prefix.jsonl")
    # Re-parsing the same bytes under a different file_key stands in for the
    # sibling file that replays them: same thread, same counter positions,
    # so the identities must match even though the file_key does not.
    replay = parse.parse_file(
        "codex/some_other_rollout.jsonl",
        (FIX / "rollout_fork_prefix.jsonl").read_bytes(),
    )
    assert [r["uuid"] for r in fork["records"]] == [
        r["uuid"] for r in replay["records"]]
    assert {r["file_key"] for r in fork["records"]} != {
        r["file_key"] for r in replay["records"]}


def test_a_fragment_with_no_session_meta_falls_back_to_per_file_identity():
    """A mid-file window has no thread identity to share. Per-file is the
    honest answer — it simply cannot dedup against anything."""
    out = _parse("rollout_model_switch.jsonl")
    assert all(r["uuid"].startswith("codex/rollout_model_switch.jsonl:")
               for r in out["records"])


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


def test_reasoning_tokens_are_a_subset_of_output_never_an_addend():
    """Adding them to output would double-bill every thinking token."""
    out = _parse("rollout_fork_prefix.jsonl")
    for rec in out["records"]:
        assert rec["thinking_tokens"] <= rec["output_tokens"]


def test_reasoning_tokens_do_not_change_what_a_record_costs():
    out = _parse("rollout_fork_prefix.jsonl")
    for rec in out["records"]:
        expected = pricing.compute_cost(
            rec["model"],
            fresh=rec["fresh_tokens"], output=rec["output_tokens"],
            eph5=0, eph1h=0, unsplit_create=rec["cache_creation_tokens"],
            read=rec["cache_read_tokens"],
            ts=rec["ts"],
        )
        assert rec["cost_usd"] == pytest.approx(round(expected, 6), rel=1e-9)


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
    out = parse.parse_file("codex/cache_write.jsonl", blob)
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
# The item_completed rewrap (Codex >= 2026-08-18)
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
    "output_tokens", "thinking_tokens", "long_context", "cost_usd",
    "text_chars", "reply_latency_s", "ctx_input", "stop_reason", "effort",
    "cli_version", "turn_flags", "turn_tool_results", "eph5_tokens",
    "eph1h_tokens",
}
TOOL_USE_KEYS = {
    "file_key", "line_num", "idx", "ts", "tool_name", "model", "tool_use_id",
    "is_error", "error_kind", "error_text", "lines_added", "lines_deleted",
    "agent_type", "agent_model", "dispatch_prompt_chars",
    "dispatch_brief_ref", "dispatch_name", "result_chars", "read_kind",
    "read_targets",
    "write_targets", "is_reread",
}


# --------------------------------------------------------------------------
# Shell churn inside the JS program
# --------------------------------------------------------------------------
