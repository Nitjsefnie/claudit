"""Kimi request generation windows through the public parser."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend import parse
from backend.flag_registry import COLLECTION_SPECS
from backend.flags import CollectionPolicy

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "parser"
KEY = "sessions/p/s/wire.jsonl"


@pytest.mark.parametrize("name", ["kimi_code_generation.jsonl", "kimi_legacy_generation.jsonl"])
def test_kimi_generation_collection_off_and_unstamped_anchor(name):
    blob = (FIX / name).read_bytes()
    off = CollectionPolicy(frozenset({"generation_speed"}), COLLECTION_SPECS)
    records = parse.parse_file(KEY, blob, collection=off)["records"]
    assert len(records) == 2
    assert all(r["generation_s"] is r["output_tokens_per_s"] is None for r in records)
    rows = [json.loads(line) for line in blob.splitlines()]
    rows[1].pop("time" if name.startswith("kimi_code") else "timestamp")
    record = parse.parse_file(KEY, _wire(rows))["records"][0]
    assert record["generation_s"] is record["output_tokens_per_s"] is None


def _wire(rows: list[dict]) -> bytes:
    return b"\n".join(json.dumps(row).encode() for row in rows)


def _kc(sec: int, typ: str, **fields) -> dict:
    return {"time": 1_783_000_000_000 + sec * 1000, "type": typ, **fields}


def _kc_event(sec: int, typ: str, **fields) -> dict:
    return _kc(sec, "context.append_loop_event", event={"type": typ, **fields})


def _kc_usage(sec: int) -> dict:
    return _kc(sec, "usage.record", usage={"inputOther": 10, "output": 12})


@pytest.mark.parametrize("response", [
    _kc_event(5, "content.part", part={"type": "text", "text": "hi"}),
    _kc_event(5, "content.part", part={"type": "think", "think": "hm"}),
    _kc_event(5, "tool.call", toolCallId="c", name="Read", args={}),
    _kc(5, "context.append_message", message={"role": "assistant", "content": []}),
])
@pytest.mark.parametrize("anchor", [
    _kc(1, "turn.prompt", input=[]),
    _kc_event(1, "tool.result", toolCallId="c", result={"output": "ok"}),
    _kc(1, "context.append_message", message={"role": "tool", "toolCallId": "c"}),
])
def test_kimi_code_generation_anchor_and_response_shapes(anchor, response):
    rows = [_kc(0, "metadata", created_at=1_783_000_000_000), anchor,
            _kc_event(2, "step.begin", turnId="t"), response,
            _kc_event(7, "step.end"), _kc_usage(8), _kc_usage(9)]
    records = parse.parse_file(KEY, _wire(rows))["records"]
    assert [(r["generation_s"], r["output_tokens_per_s"])
            for r in records] == [(4.0, 3.0), (None, None)]


def test_kimi_code_generation_steer_anchors_only_at_delivery():
    steer = [{"type": "text", "text": "next"}]
    rows = [_kc(1, "turn.prompt", input=[]),
            _kc_event(3, "content.part", part={"type": "text", "text": "a"}),
            _kc(4, "turn.steer", input=steer),
            _kc(5, "context.append_message", message={"role": "user", "content": []}),
            _kc_event(7, "content.part", part={"type": "text", "text": "b"}),
            _kc_usage(8),
            _kc(10, "context.append_message", message={"role": "user", "content": steer}),
            _kc_event(13, "content.part", part={"type": "think", "think": "hm"}),
            _kc_usage(15)]
    records = parse.parse_file(KEY, _wire(rows))["records"]
    assert [(r["generation_s"], r["output_tokens_per_s"])
            for r in records] == [(6.0, 2.0), (3.0, 4.0)]


@pytest.mark.parametrize("kind", ["quota_exhausted", "rate_limit", "other"])
@pytest.mark.parametrize("after_response", [False, True])
def test_kimi_code_generation_llm_error_invalidates_any_kind(kind, after_response):
    response = _kc_event(3, "content.part", part={"type": "text", "text": "hi"})
    failure = _kc(4, "llm.error", kind=kind, message="request failed")
    rows = [_kc(1, "turn.prompt", input=[]),
            *([response, failure] if after_response else [failure, response]),
            _kc_usage(5)]
    out = parse.parse_file(KEY, _wire(rows))
    assert len(out["records"]) == 1
    assert out["records"][0]["generation_s"] is None
    assert out["records"][0]["output_tokens_per_s"] is None
    assert len(out["rate_limit_hits"]) == int(kind == "quota_exhausted")


def _legacy(sec: int, typ: str, **payload) -> dict:
    return {"timestamp": 1_783_000_000 + sec,
            "message": {"type": typ, "payload": payload}}


@pytest.mark.parametrize("response", [
    _legacy(5, "ContentPart", type="text", text="hi"),
    _legacy(5, "ContentPart", type="think", think="hm"),
    _legacy(5, "ToolCall", id="c", function={"name": "Read", "arguments": "{}"}),
    _legacy(5, "ToolCallPart", arguments_part="{}"),
])
@pytest.mark.parametrize("anchor", [
    _legacy(1, "TurnBegin", user_input="go"),
    _legacy(1, "ToolResult", tool_call_id="c", return_value={"output": "ok"}),
])
def test_legacy_kimi_generation_anchor_and_response_shapes(anchor, response):
    rows = [{"type": "metadata", "protocol_version": "1.10"}, anchor,
            response, _legacy(6, "StatusUpdate", token_usage={}),
            _legacy(8, "StatusUpdate", token_usage={"input_other": 10, "output": 12}),
            _legacy(9, "StatusUpdate", token_usage={"input_other": 20, "output": 12})]
    records = parse.parse_file(KEY, _wire(rows))["records"]
    assert [(r["generation_s"], r["output_tokens_per_s"])
            for r in records] == [(4.0, 3.0), (None, None)]
