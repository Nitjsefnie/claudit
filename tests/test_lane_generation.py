"""Lane generation windows and Codex wire timing boundaries."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend import flags, parse
from backend.lane_generation import LaneGeneration
from backend.parse_common import _ParseState


FIX = Path(__file__).resolve().parents[1] / "fixtures" / "codex"


@pytest.mark.parametrize("ordering", ["new", "old"])
def test_codex_generation_collection_off(ordering):
    blob = (FIX / f"rollout_generation_{ordering}_ordering.jsonl").read_bytes()
    enabled = parse.parse_file("codex/synthetic.jsonl", blob)
    policy = flags.resolve_config({"CLAUDIT_COLLECT_GENERATION_SPEED": "0"}).collection
    disabled = parse.parse_file("codex/synthetic.jsonl", blob, collection=policy)
    assert any(r["generation_s"] is not None for r in enabled["records"])
    assert any(r["reply_latency_s"] is not None for r in disabled["records"])
    for record in disabled["records"]:
        assert record["generation_s"] is None
        assert record["output_tokens_per_s"] is None
    for result in (enabled, disabled):
        for record in result["records"]:
            del record["generation_s"], record["output_tokens_per_s"]
    assert disabled == enabled


def test_codex_generation_search_only_duplicate_preserves_window():
    rows = [json.loads(line) for line in
            (FIX / "rollout_generation_new_ordering.jsonl").read_bytes().splitlines()]
    output_index = next(i for i, row in enumerate(rows)
                        if row["payload"].get("type") == "custom_tool_call_output")
    rows.insert(output_index + 1, {
        "timestamp": "2026-06-14T12:00:08Z", "type": "response_item",
        "payload": {"type": "web_search_call"},
    })
    blob = b"\n".join(json.dumps(row).encode() for row in rows)
    enabled = parse.parse_file("codex/synthetic.jsonl", blob)
    records = enabled["records"]
    assert [r["web_search_requests"] for r in records] == [None, 1, None, None]
    assert [r["output_tokens"] for r in records] == [12, 0, 24, 40]
    assert [r["generation_s"] for r in records] == [None, None, 6, 8]
    assert [r["output_tokens_per_s"] for r in records] == [None, None, 4, 5]
    policy = flags.resolve_config({"CLAUDIT_COLLECT_GENERATION_SPEED": "0"}).collection
    disabled = parse.parse_file("codex/synthetic.jsonl", blob, collection=policy)
    for result in (enabled, disabled):
        for record in result["records"]:
            del record["generation_s"], record["output_tokens_per_s"]
    assert enabled == disabled


def _codex_generation_blob(response_type, payload, end):
    rows = [
        {"timestamp": "2026-06-14T12:00:00Z", "type": "turn_context",
         "payload": {"model": "gpt-5.6-sol"}},
        {"timestamp": "2026-06-14T12:00:00Z", "type": "response_item",
         "payload": {"type": "message", "role": "user"}},
        {"timestamp": end, "type": response_type, "payload": payload},
        {"timestamp": "2026-06-14T13:00:02Z", "type": "event_msg",
         "payload": {"type": "token_count", "info": {
             "total_token_usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
             "last_token_usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}}}},
    ]
    return b"\n".join(json.dumps(row).encode() for row in rows)


@pytest.mark.parametrize("rtype,payload", [
    ("response_item", {"type": "message", "role": "assistant"}),
    ("response_item", {"type": "agent_message"}),
    ("response_item", {"type": "reasoning"}),
    ("response_item", {"type": "custom_tool_call"}),
    ("response_item", {"type": "function_call"}),
    ("event_msg", {"type": "agent_message"}),
    ("event_msg", {"type": "agent_reasoning"}),
    ("event_msg", {"type": "item_completed", "item": {"type": "AgentMessage"}}),
    ("event_msg", {"type": "item_completed", "item": {"type": "Reasoning"}}),
])
def test_codex_generation_response_shapes(rtype, payload):
    blob = _codex_generation_blob(rtype, payload, "2026-06-14T12:00:05Z")
    record, = parse.parse_file("codex/synthetic.jsonl", blob)["records"]
    assert (record["generation_s"], record["output_tokens_per_s"]) == (5, 4)


@pytest.mark.parametrize("end,seconds,speed", [
    ("2026-06-14T13:00:01Z", None, None),
    ("2026-06-14T12:00:00Z", None, None),
    ("2026-06-14T11:59:59Z", None, None),
    (None, None, None),
    ("2026-06-14T13:00:00Z", 3600, 20 / 3600),
])
def test_codex_generation_duration_limits(end, seconds, speed):
    valid_blob = _codex_generation_blob(
        "response_item", {"type": "reasoning"}, "2026-06-14T12:00:05Z")
    valid, = parse.parse_file("codex/synthetic.jsonl", valid_blob)["records"]
    assert (valid["generation_s"], valid["output_tokens_per_s"]) == (5, 4)
    blob = _codex_generation_blob("response_item", {"type": "reasoning"}, end)
    record, = parse.parse_file("codex/synthetic.jsonl", blob)["records"]
    assert (record["generation_s"], record["output_tokens_per_s"]) == (seconds, speed)


def test_lane_generation_failure_and_recovery():
    clock = LaneGeneration()
    clock.anchor_line(datetime(2026, 6, 14, 12, 0, 0, tzinfo=timezone.utc))
    clock.response_line(datetime(2026, 6, 14, 12, 0, 2, tzinfo=timezone.utc))
    clock.fail()
    clock.response_line(datetime(2026, 6, 14, 12, 0, 4, tzinfo=timezone.utc))
    assert clock.bind() is None
    clock.anchor_line(datetime(2026, 6, 14, 12, 0, 5, tzinfo=timezone.utc))
    clock.response_line(datetime(2026, 6, 14, 12, 0, 9, tzinfo=timezone.utc))
    assert clock.bind() == 4
    assert clock.bind() is None


def test_lane_generation_empty_inputs_preserve_closed_window():
    clock = LaneGeneration()
    clock.anchor_line(datetime(2026, 6, 14, 12, 0, 0, tzinfo=timezone.utc))
    clock.response_line(datetime(2026, 6, 14, 12, 0, 2, tzinfo=timezone.utc))
    clock.anchor_line(datetime(2026, 6, 14, 12, 0, 3, tzinfo=timezone.utc))
    clock.anchor_line(None)
    assert clock.bind() == 2
    clock.response_line(datetime(2026, 6, 14, 12, 0, 5, tzinfo=timezone.utc))
    assert clock.bind() is None


def test_lane_generation_clock_not_constructed_when_disabled():
    policy = flags.resolve_config({"CLAUDIT_COLLECT_GENERATION_SPEED": "0"}).collection
    assert _ParseState("codex/synthetic.jsonl", collection=policy).lane_generation is None
