"""Kimi request generation windows through the public parser."""
from __future__ import annotations

import json
from pathlib import Path

from backend import parse
from backend.flag_registry import COLLECTION_SPECS
from backend.flags import CollectionPolicy

FIX = Path(__file__).parents[1] / "fixtures" / "parser"
KEY = "sessions/p/s/wire.jsonl"


def test_kimi_generation_collection_off_and_unstamped_anchor():
    off = CollectionPolicy(frozenset({"generation_speed"}), COLLECTION_SPECS)
    for name in ("kimi_code_generation.jsonl", "kimi_legacy_generation.jsonl"):
        blob = (FIX / name).read_bytes()
        records = parse.parse_file(KEY, blob, collection=off)["records"]
        assert len(records) == 2
        assert records[0]["reply_latency_s"] is not None
        assert all(r["generation_s"] is r["output_tokens_per_s"] is None for r in records)
        rows = [json.loads(line) for line in blob.splitlines()]
        rows[1].pop("time" if name.startswith("kimi_code") else "timestamp")
        record = parse.parse_file(KEY, _wire(rows))["records"][0]
        assert record["generation_s"] is record["output_tokens_per_s"] is None


def _wire(rows: list[dict]) -> bytes:
    return b"\n".join(json.dumps(row).encode() for row in rows)


def test_kimi_code_generation_cycles():
    blob = b"""{"time": 1783000001000, "type": "turn.prompt", "input": []}
{"time": 1783000003000, "type": "context.append_loop_event", "event": {"type": "content.part", "part": {"type": "text", "text": "a"}}}
{"time": 1783000004000, "type": "turn.steer", "input": [{"type": "text", "text": "next"}]}
{"time": 1783000005000, "type": "context.append_message", "message": {"role": "user", "content": []}}
{"time": 1783000007000, "type": "context.append_loop_event", "event": {"type": "content.part", "part": {"type": "text", "text": "b"}}}
{"time": 1783000008000, "type": "usage.record", "usage": {"inputOther": 10, "output": 12}}
{"time": 1783000010000, "type": "context.append_message", "message": {"role": "user", "content": [{"type": "text", "text": "next"}]}}
{"time": 1783000013000, "type": "context.append_loop_event", "event": {"type": "content.part", "part": {"type": "think", "think": "hm"}}}
{"time": 1783000015000, "type": "usage.record", "usage": {"inputOther": 10, "output": 12}}
{"time": 1783000016000, "type": "usage.record", "usage": {"inputOther": 10, "output": 12}}
{"time": 1783000020000, "type": "context.append_loop_event", "event": {"type": "tool.result", "toolCallId": "c", "result": {"output": "ok"}}}
{"time": 1783000023000, "type": "context.append_message", "message": {"role": "assistant", "content": []}}
{"time": 1783000024000, "type": "usage.record", "usage": {"inputOther": 10, "output": 12}}
{"time": 1783000030000, "type": "context.append_message", "message": {"role": "tool", "toolCallId": "c"}}
{"time": 1783000034000, "type": "context.append_loop_event", "event": {"type": "tool.call", "toolCallId": "c", "name": "Read", "args": {}}}
{"time": 1783000035000, "type": "usage.record", "usage": {"inputOther": 10, "output": 12}}
{"time": 1783000041000, "type": "turn.prompt", "input": []}
{"time": 1783000042000, "type": "llm.error", "kind": "quota_exhausted", "message": "request failed"}
{"time": 1783000043000, "type": "context.append_loop_event", "event": {"type": "content.part", "part": {"type": "text", "text": "hi"}}}
{"time": 1783000045000, "type": "usage.record", "usage": {"inputOther": 10, "output": 12}}
{"time": 1783000051000, "type": "turn.prompt", "input": []}
{"time": 1783000053000, "type": "context.append_loop_event", "event": {"type": "content.part", "part": {"type": "text", "text": "hi"}}}
{"time": 1783000054000, "type": "llm.error", "kind": "quota_exhausted", "message": "request failed"}
{"time": 1783000055000, "type": "usage.record", "usage": {"inputOther": 10, "output": 12}}
{"time": 1783000061000, "type": "turn.prompt", "input": []}
{"time": 1783000062000, "type": "llm.error", "kind": "rate_limit", "message": "request failed"}
{"time": 1783000063000, "type": "context.append_loop_event", "event": {"type": "content.part", "part": {"type": "text", "text": "hi"}}}
{"time": 1783000065000, "type": "usage.record", "usage": {"inputOther": 10, "output": 12}}
{"time": 1783000071000, "type": "turn.prompt", "input": []}
{"time": 1783000073000, "type": "context.append_loop_event", "event": {"type": "content.part", "part": {"type": "text", "text": "hi"}}}
{"time": 1783000074000, "type": "llm.error", "kind": "rate_limit", "message": "request failed"}
{"time": 1783000075000, "type": "usage.record", "usage": {"inputOther": 10, "output": 12}}
{"time": 1783000081000, "type": "turn.prompt", "input": []}
{"time": 1783000082000, "type": "llm.error", "kind": "other", "message": "request failed"}
{"time": 1783000083000, "type": "context.append_loop_event", "event": {"type": "content.part", "part": {"type": "text", "text": "hi"}}}
{"time": 1783000085000, "type": "usage.record", "usage": {"inputOther": 10, "output": 12}}
{"time": 1783000091000, "type": "turn.prompt", "input": []}
{"time": 1783000093000, "type": "context.append_loop_event", "event": {"type": "content.part", "part": {"type": "text", "text": "hi"}}}
{"time": 1783000094000, "type": "llm.error", "kind": "other", "message": "request failed"}
{"time": 1783000095000, "type": "usage.record", "usage": {"inputOther": 10, "output": 12}}
{"time": 1783000101000, "type": "turn.prompt", "input": []}
{"time": 1783000102000, "type": "context.append_loop_event", "event": {"type": "content.part", "part": {"type": "text", "text": "ok"}}}
{"time": 1783000103000, "type": "usage.record", "usage": {"inputOther": 10, "output": 12}}"""
    out = parse.parse_file(KEY, blob)
    assert [(r["generation_s"], r["output_tokens_per_s"]) for r in out["records"]] == (
        [(6, 2), (3, 4), (None, None), (3, 4), (4, 3)]
        + [(None, None)] * 6 + [(1, 12)])
    assert [hit["line"] for hit in out["rate_limit_hits"]] == [18, 23]


def test_legacy_kimi_generation_content_and_empty_usage():
    rows = [json.loads(line) for line in (FIX / "kimi_legacy_generation.jsonl").read_bytes().splitlines()]
    rows = [row for row in rows if row.get("message", {}).get("type") not in ("ToolCall", "ToolCallPart")]
    rows.insert(3, {"timestamp": 1783000004, "message": {
        "type": "StatusUpdate", "payload": {"token_usage": {}}}})
    records = parse.parse_file(KEY, _wire(rows))["records"]
    assert [(r["generation_s"], r["output_tokens_per_s"])
            for r in records] == [(2, 4), (2, 4.5)]
