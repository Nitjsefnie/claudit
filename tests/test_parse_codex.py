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
from datetime import timedelta
from pathlib import Path

import json
import pytest

from backend import parse, parse_codex, pricing
from backend.parse_codex import (_change_churn, _codex_declared_models,
                                 _codex_model, _diff_churn)


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


def test_a_file_declaring_no_model_falls_back_rather_than_dropping_records():
    """The fork fixture declares none. An unattributed record still has to
    be billed, and it bills at the flagship rate: a visible overcount beats
    a silent undercount."""
    blob = (FIX / "rollout_fork_prefix.jsonl").read_bytes()
    assert _codex_declared_models(blob) == set()
    out = _parse("rollout_fork_prefix.jsonl")
    assert all(r["model"] == "gpt-6-astra" for r in out["records"])


def test_a_file_declaring_several_models_reports_all_of_them():
    blob = (FIX / "rollout_model_switch.jsonl").read_bytes()
    assert _codex_declared_models(blob) == {"gpt-5.6-sol", "gpt-5.6-terra"}


@pytest.mark.parametrize("raw,expected", [
    ("gpt-5.6-sol", "gpt-5.6-sol"),
    ("gpt-5.6-terra", "gpt-5.6-terra"),
    ("gpt5.6-sol", "gpt-5.6-sol"),        # real: one turn_context spells it so
    ("GPT-5.6-Terra", "gpt-5.6-terra"),
    ("gpt-5.6-luna-preview", "gpt-5.6-luna"),
    # Astra is GPT-6 and priced separately; before it had a row here it
    # matched no needle and was relabelled to the flagship, which is why the
    # dashboard reported zero Astra usage while the corpus was half Astra.
    ("gpt-6-astra", "gpt-6-astra"),
    ("GPT-6-Astra", "gpt-6-astra"),
    ("gpt-7-unreleased", "gpt-6-astra"),  # unknown -> flagship, not the default
    (None, "gpt-6-astra"),
])
def test_model_ids_canonicalise_to_a_priced_label(raw, expected):
    assert _codex_model(raw) == expected


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


def test_cache_write_tokens_are_billed_on_their_own_meter():
    """0 across the local corpus, but real and billable in the format —
    Codex charges a cache write at 1.25x uncached input. Folding it into
    fresh underbills it; folding it into cached overbills it 10x. Asserted
    on the rate table, since no local record exercises the bucket."""
    assert all(r["cache_creation_tokens"] == 0
               for r in _parse("rollout_fork_prefix.jsonl")["records"])
    # claudit's table keys are its normalised (dashed) form of the label.
    rates = pricing.MODEL_RATES["gpt-5-6-sol"]
    assert rates["create_1h"] == pytest.approx(rates["fresh"] * 1.25)
    assert rates["create_1h"] > rates["fresh"] > rates["read"]
    write_only = pricing.compute_cost(
        "gpt-5.6-sol", fresh=0, output=0,
        eph5=0, eph1h=0, unsplit_create=1_000_000, read=0)
    assert write_only == pytest.approx(rates["create_1h"], rel=1e-9)
    # The ratio holds in the retired window too, at 1.25 x the old 5.00.
    old = pricing.rate_for("gpt-5.6-sol", pricing.AUG21_CUT - timedelta(1))
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


def test_a_cache_write_is_billed_at_its_own_rate_not_at_fresh_input_rates():
    written = _uncached_headroom("rollout_fork_prefix.jsonl") // 2
    blob = _with_cache_write("rollout_fork_prefix.jsonl", written)
    rec = parse.parse_file("codex/cache_write.jsonl", blob)["records"][-1]
    plain = _parse("rollout_fork_prefix.jsonl")["records"][-1]
    # Same prompt, but half its uncached part written to cache at 1.25x
    # instead of billed as fresh input at 1x — so it costs strictly MORE.
    assert rec["cost_usd"] > plain["cost_usd"]
    # The fixture declares no model, so it bills at the flagship rate.
    rates = pricing.rate_for(_codex_model(None), rec["ts"])
    expected_delta = written * (rates["create_1h"] - rates["fresh"]) / 1_000_000
    assert rec["cost_usd"] - plain["cost_usd"] == pytest.approx(
        expected_delta, abs=1e-6)


def test_the_billed_buckets_still_partition_the_prompt_exactly_once():
    """fresh + create + read == the whole prompt, with cache_write counted
    once in create and not again in fresh."""
    out = _parse("rollout_fork_prefix.jsonl")
    for rec in out["records"]:
        assert rec["ctx_input"] == (
            rec["fresh_tokens"] + rec["cache_creation_tokens"]
            + rec["cache_read_tokens"])


# --------------------------------------------------------------------------
# Pricing
# --------------------------------------------------------------------------


@pytest.mark.parametrize("label", [
    "gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
])
def test_every_label_the_codex_parser_emits_is_priced_exactly(label):
    """An unmatched label resolves to the cheapest Codex fallback, which
    would silently understate Sol or Terra while marking the value estimated.
    """
    resolution = pricing.resolve(label)
    assert resolution.kind == "exact"
    assert resolution.estimated is False


def test_every_codex_label_lives_in_the_browser_mirrored_table():
    """The dashboard calls window.rateForModel for backend Codex rows, so
    keeping Codex rates outside the mirrored table misprices that view.
    claudit's table is a SUPERSET -- it also prices the Claude and GLM
    lanes -- and its keys are written in its normalised (dashed) form.
    """
    assert {
        "kimi-k3", "kimi-k2-7-code", "kimi-k2-6",
        "gpt-6-astra", "gpt-5-6-sol", "gpt-5-6-terra", "gpt-5-6-luna",
    } <= set(pricing.MODEL_RATES)


def test_codex_records_are_not_billed_at_kimi_rates():
    out = _parse("rollout_model_switch.jsonl")
    for rec in out["records"]:
        assert pricing.resolve(rec["model"]).kind == "exact"


def test_the_long_context_meter_doubles_input_and_multiplies_output_by_1_5():
    """Unexercised by the fixtures — the largest request they carry is
    230,000 input tokens against a 272,000 threshold, matching a corpus
    whose largest measured request is 243,093 — so it is asserted on the
    rate function directly rather than on a fixture."""
    base = pricing.compute_cost(
        "gpt-5.6-sol", fresh=1_000_000, output=0,
        eph5=0, eph1h=0, unsplit_create=0, read=0)
    long_in = pricing.compute_cost(
        "gpt-5.6-sol", fresh=1_000_000, output=0,
        eph5=0, eph1h=0, unsplit_create=0, read=0, long_context=True)
    assert long_in == pytest.approx(base * 2.0, rel=1e-9)
    base_out = pricing.compute_cost(
        "gpt-5.6-sol", fresh=0, output=1_000_000,
        eph5=0, eph1h=0, unsplit_create=0, read=0)
    long_out = pricing.compute_cost(
        "gpt-5.6-sol", fresh=0, output=1_000_000,
        eph5=0, eph1h=0, unsplit_create=0, read=0, long_context=True)
    assert long_out == pytest.approx(base_out * 1.5, rel=1e-9)


def test_the_long_context_meter_is_off_by_default_for_kimi_records():
    """Kimi has no such tier; the flag must not leak into that path."""
    out = parse.parse_file(
        "sessions/projA/sess-A/wire.jsonl",
        (KIMI_FIX / "kimi_code.jsonl").read_bytes(),
    )
    for rec in out["records"]:
        expected = pricing.compute_cost(
            rec["model"],
            fresh=rec["fresh_tokens"], output=rec["output_tokens"],
            eph5=0, eph1h=0, unsplit_create=rec["cache_creation_tokens"],
            read=rec["cache_read_tokens"],
        )
        assert rec["cost_usd"] == pytest.approx(round(expected, 6), rel=1e-9)


# --------------------------------------------------------------------------
# Tool calls and line churn
# --------------------------------------------------------------------------


def test_codex_tool_uses_preserve_event_time_models_across_a_switch():
    """Each tool call keeps the model that was active at its event."""
    lines = [
        {
            "timestamp": "2026-06-14T12:00:01.000Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.6-sol"},
        },
        {
            "timestamp": "2026-06-14T12:00:02.000Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "call_id": "sol-call",
                "input": "tools.exec_command({})",
            },
        },
        {
            "timestamp": "2026-06-14T12:00:03.000Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "sol-call",
                "output": "ok",
            },
        },
        {
            "timestamp": "2026-06-14T12:00:04.000Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.6-terra"},
        },
        {
            "timestamp": "2026-06-14T12:00:05.000Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "call_id": "terra-call",
                "input": "tools.exec_command({})",
            },
        },
        {
            "timestamp": "2026-06-14T12:00:06.000Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "terra-call",
                "output": "ok",
            },
        },
    ]
    blob = b"".join(json.dumps(line).encode() + b"\n" for line in lines)

    # parse_codex.parse, not parse.parse_file: the format module keeps a
    # tool row's model; claudit's adapter drops it (the tool_uses table
    # has no model column -- the record carries it).
    out = parse_codex.parse("codex/tool_model_switch.jsonl", blob)

    assert [tool_use.get("model") for tool_use in out["tool_uses"]] == [
        "gpt-5.6-sol", "gpt-5.6-terra",
    ]


def test_codex_tool_use_before_first_declaration_uses_the_sole_model():
    """A sole declaration backfills a tool call in replayed history."""
    lines = [
        {
            "timestamp": "2026-06-14T12:00:01.000Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "call_id": "prefix-call",
                "input": "tools.exec_command({})",
            },
        },
        {
            "timestamp": "2026-06-14T12:00:02.000Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "prefix-call",
                "output": "ok",
            },
        },
        {
            "timestamp": "2026-06-14T12:00:03.000Z",
            "type": "turn_context",
            "payload": {"model": "gpt-5.6-terra"},
        },
    ]
    blob = b"".join(json.dumps(line).encode() + b"\n" for line in lines)

    out = parse_codex.parse("codex/tool_sole_model.jsonl", blob)

    assert [tool_use.get("model") for tool_use in out["tool_uses"]] == [
        "gpt-5.6-terra",
    ]


def test_exec_calls_are_named_by_the_api_they_invoke():
    """`exec` is the only custom tool; naming rows after it would collapse
    every shell command, patch and plan update into one bucket."""
    out = _parse("rollout_patch_linked.jsonl")
    names = [t["tool_name"] for t in out["tool_uses"]]
    assert names == [
        "exec_command", "exec_command", "exec_command",
        "apply_patch", "exec_command", "apply_patch", "exec_command",
    ]


def test_a_completed_tool_call_is_settled_as_not_errored():
    out = _parse("rollout_patch_linked.jsonl")
    assert all(t["is_error"] is False for t in out["tool_uses"])


def test_patch_churn_lands_on_the_call_that_applied_it():
    """patch_apply_end shares no call_id with the tool calls, so the churn
    is carried onto the most recent apply_patch call of the same turn."""
    out = _parse("rollout_patch_linked.jsonl")
    patches = [t for t in out["tool_uses"] if t["tool_name"] == "apply_patch"]
    assert len(patches) == 2
    for tool_use in patches:
        assert (tool_use["lines_added"], tool_use["lines_deleted"]) == (1, 1)
    for tool_use in out["tool_uses"]:
        if tool_use["tool_name"] == "exec_command":
            assert (tool_use["lines_added"], tool_use["lines_deleted"]) == (0, 0)


def test_a_subagents_patch_is_recorded_even_with_no_tool_call_to_attach_to():
    """The parent rollout journals the patch but not the subagent's tool
    call. Dropping it loses the only record of that edit."""
    out = _parse("rollout_patch_subagent.jsonl")
    assert len(out["tool_uses"]) == 1
    tool_use = out["tool_uses"][0]
    assert tool_use["tool_name"] == "apply_patch"
    assert tool_use["is_error"] is False
    assert (tool_use["lines_added"], tool_use["lines_deleted"]) == (11, 2)


@pytest.mark.parametrize("diff,expected", [
    ("@@ -1,2 +1,3 @@\n ctx\n-old\n+new\n+extra\n", (2, 1)),
    ("--- a/x\n+++ b/x\n+one\n", (1, 0)),   # file headers are not churn
    ("", (0, 0)),
    (None, (0, 0)),
])
def test_diff_churn_counts_changed_lines_not_headers(diff, expected):
    assert _diff_churn(diff) == expected


# --------------------------------------------------------------------------
# The item_completed rewrap (Codex >= 2026-08-18)
# --------------------------------------------------------------------------


def test_a_filechange_item_is_counted_as_churn():
    """From 2026-08-18 Codex stopped emitting event_msg/patch_apply_end and
    began wrapping the same content in event_msg/item_completed with an
    item.type of FileChange. A parser that only knows the old discriminator
    books every patch at zero lines, silently: the rows still appear, the
    churn is just gone."""
    out = _parse("rollout_item_completed.jsonl")
    patches = [t for t in out["tool_uses"] if t["tool_name"] == "apply_patch"]
    assert [(t["lines_added"], t["lines_deleted"]) for t in patches] == [
        (3, 3),   # update +1/-1, add of 2 lines, delete of 2 lines
        (0, 0),   # status=failed changed nothing
        (4, 0),   # a 4-line file added
    ]


def test_an_added_or_deleted_file_carries_content_not_a_diff():
    """`add` and `delete` changes have no unified_diff at all — the whole
    file body is under `content`. Counting only unified_diff drops them, and
    on the reference corpus adds and deletes are 44% of all changes."""
    out = _parse("rollout_item_completed.jsonl")
    linked = [t for t in out["tool_uses"] if t["tool_name"] == "apply_patch"][0]
    # 1 diff line + 2 added-file lines; 1 diff line + 2 deleted-file lines.
    assert (linked["lines_added"], linked["lines_deleted"]) == (3, 3)


def test_a_failed_filechange_contributes_no_churn_and_is_an_error():
    out = _parse("rollout_item_completed.jsonl")
    failed = [t for t in out["tool_uses"] if t["is_error"]]
    assert len(failed) == 1
    assert (failed[0]["lines_added"], failed[0]["lines_deleted"]) == (0, 0)


def test_filechange_churn_lands_on_the_apply_patch_call_of_the_same_turn():
    """item.id is "exec-<uuid>" and shares no namespace with call_id (0 of
    2,623 FileChange ids matched a call_id on the reference corpus), so the
    turn carries the attribution exactly as patch_apply_end's did."""
    out = _parse("rollout_item_completed.jsonl")
    assert len(out["tool_uses"]) == 3
    linked = out["tool_uses"][0]
    assert linked["tool_name"] == "apply_patch"
    assert linked["is_error"] is False
    assert (linked["lines_added"], linked["lines_deleted"]) == (3, 3)


def test_an_agentmessage_item_is_counted_as_assistant_text():
    """event_msg/agent_message became item_completed/AgentMessage in the same
    release, so Response Sizes reads zero for every session after it."""
    out = _parse("rollout_item_completed.jsonl")
    assert sum(rec["text_chars"] for rec in out["records"]) == 138


@pytest.mark.parametrize("change,expected", [
    ({"type": "update", "unified_diff": "@@ -1 +1,2 @@\n-a\n+b\n+c\n"},
     (2, 1)),
    ({"type": "add", "content": "one\ntwo\nthree\n"}, (3, 0)),
    ({"type": "delete", "content": "one\ntwo\n"}, (0, 2)),
    ({"type": "add", "content": ""}, (0, 0)),
    ({"type": "update", "unified_diff": None, "move_path": "/b"}, (0, 0)),
    ({}, (0, 0)),
])
def test_change_churn_reads_whichever_field_the_change_type_carries(
        change, expected):
    assert _change_churn(change) == expected


# --------------------------------------------------------------------------
# Turns, context growth, rate limits
# --------------------------------------------------------------------------


def test_turns_are_bounded_by_task_started_and_task_complete():
    out = _parse("rollout_model_switch.jsonl")
    assert out["turn_count"] == len(out["ctx_turns"]) > 0
    for turn in out["ctx_turns"]:
        assert turn["input"] > 0
        assert turn["ts"]


def test_ctx_turns_deltas_chain_from_one_turn_to_the_next():
    out = _parse("rollout_model_switch.jsonl")
    prev = 0
    for turn in out["ctx_turns"]:
        assert turn["delta"] == turn["input"] - prev
        prev = turn["input"]


def test_reply_latency_is_measured_once_per_turn_and_is_never_negative():
    out = _parse("rollout_model_switch.jsonl")
    latencies = [r["reply_latency_s"] for r in out["records"]
                 if r["reply_latency_s"] is not None]
    assert latencies, "a turn with a billing record should time its reply"
    assert all(value >= 0 for value in latencies)
    assert len(latencies) <= out["turn_count"] + 1


def test_assistant_text_is_counted_once_per_turn():
    """event_msg/agent_message and response_item/message role=assistant
    carry the same text; counting both doubles text_chars."""
    out = _parse("rollout_model_switch.jsonl")
    assert any(r["text_chars"] > 0 for r in out["records"])
    longest = 0
    for raw in (FIX / "rollout_model_switch.jsonl").read_bytes().splitlines():
        payload = json.loads(raw).get("payload") or {}
        if payload.get("type") == "agent_message":
            longest += len(payload.get("message") or "")
    assert max(r["text_chars"] for r in out["records"]) <= longest


def test_no_rate_limit_hit_is_invented_from_ordinary_utilisation():
    """rate_limits rides every token_count. A hit is only the explicit
    rate_limit_reached_type / spend_control_reached fields — never a
    used_percent reading."""
    for name in ("rollout_fork_prefix.jsonl", "rollout_model_switch.jsonl",
                 "rollout_patch_linked.jsonl"):
        assert _parse(name)["rate_limit_hits"] == []


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
    "dispatch_brief_ref", "result_chars", "read_kind", "read_targets",
    "write_targets", "is_reread",
}


def test_parse_file_returns_the_same_shape_it_does_for_a_kimi_wire():
    out = _parse("rollout_model_switch.jsonl")
    assert set(out) == {"records", "ctx_turns", "turn_count",
                        "prompt_count", "models", "rate_limit_hits",
                        "tool_uses", "agent_type", "agent_type_in_band"}
    for rec in out["records"]:
        assert set(rec) == RECORD_KEYS
        assert rec["file_key"] == "codex/rollout_model_switch.jsonl"
        # This fixture is a mid-file window, so it declares no session_meta
        # and falls back to the per-file identity — see _codex_record_uuid.
        assert rec["uuid"] == (
            f"codex/rollout_model_switch.jsonl:{rec['line_num']}")
    for turn in out["ctx_turns"]:
        assert set(turn) == {"idx", "ts", "line", "input", "output", "delta"}


def test_tool_uses_carry_the_columns_the_tool_tables_expect():
    out = _parse("rollout_patch_linked.jsonl")
    for idx, tool_use in enumerate(out["tool_uses"]):
        assert set(tool_use) == TOOL_USE_KEYS
        assert tool_use["idx"] == idx
        assert tool_use["file_key"] == "codex/rollout_patch_linked.jsonl"


def test_a_truncated_final_line_does_not_abort_the_parse():
    """A rollout is appended to while the session runs, so the last line of
    a file read mid-write can be half an object."""
    blob = (FIX / "rollout_patch_linked.jsonl").read_bytes()
    truncated = blob + b'{"timestamp":"2026-06-14T12:00:30.000Z","type":"even'
    out = parse.parse_file("codex/truncated.jsonl", truncated)
    assert len(out["tool_uses"]) == 7


def test_an_empty_file_parses_to_an_empty_result():
    # A blob no rung identifies takes claudit's catch-all, the claude path
    # (parse_lanes.sniff_format) -- so an empty file parses through it and
    # reports the claude parse's own default agent_type.
    out = parse.parse_file("codex/empty.jsonl", b"")
    assert out == {"records": [], "ctx_turns": [], "turn_count": 0,
                   "prompt_count": 0, "models": [], "rate_limit_hits": [],
                   "tool_uses": [], "agent_type": parse.DEFAULT_AGENT_TYPE, "agent_type_in_band": False}


# --------------------------------------------------------------------------
# Shell churn inside the JS program
# --------------------------------------------------------------------------

def test_exec_command_heredoc_churn_is_counted():
    """Codex's shell text sits one layer in, as the `cmd` argument of a
    tools.exec_command call. A heredoc written to a file is a write."""
    out = _parse("rollout_shell_churn.jsonl")
    by_name = [(t["tool_name"], t["lines_added"], t["lines_deleted"])
               for t in out["tool_uses"]]
    assert by_name[0] == ("exec_command", 3, 0)


def test_monitor_argv_payload_churn_is_counted():
    """`monitor` takes command:[argv] rather than a cmd string — the
    shell payload is positional, and it counts the same."""
    out = _parse("rollout_shell_churn.jsonl")
    by_name = [(t["tool_name"], t["lines_added"], t["lines_deleted"])
               for t in out["tool_uses"]]
    assert by_name[1] == ("monitor", 1, 0)


def test_a_command_that_only_runs_tests_has_no_churn():
    out = _parse("rollout_shell_churn.jsonl")
    by_name = [(t["tool_name"], t["lines_added"], t["lines_deleted"])
               for t in out["tool_uses"]]
    assert by_name[2] == ("exec_command", 0, 0)


def test_an_applied_patch_is_not_also_counted_from_its_program_text():
    """The rollout journals the applied unified diff in patch_apply_end,
    which is better evidence than the program that requested it. The
    inline-patch rule must never fire on a `*** Begin Patch` string too,
    or every Codex edit is counted twice."""
    out = _parse("rollout_patch_linked.jsonl")
    patches = [t for t in out["tool_uses"] if t["tool_name"] == "apply_patch"]
    assert len(patches) == 2
    for tool_use in patches:
        assert (tool_use["lines_added"], tool_use["lines_deleted"]) == (1, 1)


def test_every_model_the_corpus_names_has_a_map_row():
    """The map's fallback RELABELS an unmapped id, so a missing row hides a
    model from the dashboard rather than merely mispricing it. Each id here
    is one a rollout has actually carried.
    """
    for raw in ("gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"):
        label = _codex_model(raw)
        assert label == raw, f"{raw} was relabelled to {label}"
