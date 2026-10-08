"""Tests moved from test_parse_codex.py to keep test modules under 700 lines."""
from __future__ import annotations

import json

import pytest

from backend import parse, parse_codex, pricing
from backend.parse_codex import _change_churn, _diff_churn

from tests.test_parse_codex import (
    FIX,
    KIMI_FIX,
    RECORD_KEYS,
    TOOL_USE_KEYS,
    _codex_model,
    _parse,
    _parse_file_norefusal,
    _uncached_headroom,
    _with_cache_write,
)


def test_a_cache_write_is_billed_at_its_own_rate_not_at_fresh_input_rates():
    written = _uncached_headroom("rollout_fork_prefix.jsonl") // 2
    blob = _with_cache_write("rollout_fork_prefix.jsonl", written)
    rec = _parse_file_norefusal("codex/cache_write.jsonl", blob)["records"][-1]
    plain = _parse("rollout_fork_prefix.jsonl")["records"][-1]
    # Same prompt, but half its uncached part written to cache at the row's
    # create_1h rate instead of billed as fresh input. The SIGNED delta
    # names the rate it took, whatever the row's ratios: strictly more at
    # list price (create_1h is 1.25x fresh there), but the perturbed-data
    # leg scales fields independently, so the sign itself is not the pin.
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


@pytest.mark.parametrize("label", [
    "gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
])
def test_every_label_the_codex_parser_stores_prices_exactly(label):
    """Every id the corpus names is a pricing.json key, so the stored model
    prices exactly — no estimate flag. An id with NO row is the issue #471
    fallback shape: stored verbatim, priced estimated, never renamed."""
    resolution = pricing.resolve(label)
    assert resolution.kind == "exact"
    assert resolution.estimated is False


def test_every_codex_label_lives_in_the_browser_mirrored_table():
    """The dashboard calls window.rateForModel for backend Codex rows, so
    keeping Codex rates outside the mirrored table misprices that view.
    Since issue #851 the first-party labels live in the tracked table and
    the mirrored table is the merged view — the bare vendor forms the
    browser's vendorBare carries beside the models table — and the keys
    are written in the normalised (dashed) form the codex parser stores.
    """
    assert {
        "kimi-k3", "kimi-k2-7-code", "kimi-k2-6",
        "gpt-6-astra", "gpt-5-6-sol", "gpt-5-6-terra", "gpt-5-6-luna",
    } <= set(pricing.VENDOR_BARE)


def test_codex_records_are_not_billed_at_kimi_rates():
    out = _parse("rollout_model_switch.jsonl")
    for rec in out["records"]:
        assert pricing.resolve(rec["model"]).kind == "exact"


def test_the_long_context_meter_doubles_input_and_multiplies_output_by_1_5(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """Unexercised by the fixtures — the largest request they carry is
    230,000 input tokens against a 272,000 threshold, matching a corpus
    whose largest measured request is 243,093 — so it is asserted on the
    rate function directly rather than on a fixture."""
    model = "gpt-5.6-sol"
    rates = {"fresh": 2.0, "create_5m": 3.0, "create_1h": 4.0,
             "read": 5.0, "output": 6.0}
    monkeypatch.setattr(pricing, "MODEL_RATES", {model: rates})
    monkeypatch.setattr(pricing, "DATED_RATES", {})
    base = pricing.compute_cost(
        model, fresh=1, output=0,
        eph5=0, eph1h=0, unsplit_create=0, read=0)
    long_in = pricing.compute_cost(
        model, fresh=1, output=0,
        eph5=0, eph1h=0, unsplit_create=0, read=0, long_context=True)
    assert long_in == pytest.approx(base * 2.0, rel=1e-9)
    base_out = pricing.compute_cost(
        model, fresh=0, output=1,
        eph5=0, eph1h=0, unsplit_create=0, read=0)
    long_out = pricing.compute_cost(
        model, fresh=0, output=1,
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
            read=rec["cache_read_tokens"], ts=rec["ts"],
        )
        assert rec["cost_usd"] == pytest.approx(round(expected, 6), rel=1e-9)


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


def test_codex_tool_use_before_first_declaration_uses_the_first_declared_model():
    """The first declared model backfills a tool call in replayed
    history (issue #653)."""
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


def test_parse_file_returns_the_same_shape_it_does_for_a_kimi_wire():
    out = _parse("rollout_model_switch.jsonl")
    assert set(out) == {"records", "ctx_turns", "turn_count",
                        "prompt_count", "prompt_ts", "models", "rate_limit_hits",
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
    out = _parse_file_norefusal("codex/truncated.jsonl", truncated)
    assert len(out["tool_uses"]) == 7


def test_an_empty_file_parses_to_an_empty_result():
    # A blob no rung identifies takes claudit's catch-all, the claude path
    # (parse_lanes.sniff_format) -- so an empty file parses through it and
    # reports the claude parse's own default agent_type.
    out = parse.parse_file("codex/empty.jsonl", b"")
    assert out == {"records": [], "ctx_turns": [], "turn_count": 0,
                   "prompt_count": 0, "prompt_ts": [], "models": [],
                   "rate_limit_hits": [], "tool_uses": [], "agent_type": parse.DEFAULT_AGENT_TYPE, "agent_type_in_band": False}


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


def test_every_model_the_corpus_names_stays_itself():
    """Each id here is one a rollout has actually carried: the parser stores
    it verbatim (spelling normalisation only, issue #471) — never renamed,
    whatever the pricing table lists."""
    for raw in ("gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"):
        label = _codex_model(raw)
        assert label == raw, f"{raw} was relabelled to {label}"
