"""Kimi request generation windows through the public parser."""
from __future__ import annotations

import json
from pathlib import Path

from backend import parse
from backend.flag_registry import COLLECTION_SPECS
from backend.flags import CollectionPolicy

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "parser"
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


def _kc(sec: int, typ: str, **fields) -> dict:
    return {"time": 1_783_000_000_000 + sec * 1000, "type": typ, **fields}


def _kc_event(sec: int, typ: str, **fields) -> dict:
    return _kc(sec, "context.append_loop_event", event={"type": typ, **fields})


def _kc_usage(sec: int) -> dict:
    return _kc(sec, "usage.record", usage={"inputOther": 10, "output": 12})


def test_kimi_code_generation_cycles():
    steer = [{"type": "text", "text": "next"}]
    rows = [_kc(1, "turn.prompt", input=[]),
            _kc_event(3, "content.part", part={"type": "text", "text": "a"}),
            _kc(4, "turn.steer", input=steer),
            _kc(5, "context.append_message", message={"role": "user", "content": []}),
            _kc_event(7, "content.part", part={"type": "text", "text": "b"}), _kc_usage(8),
            _kc(10, "context.append_message", message={"role": "user", "content": steer}),
            _kc_event(13, "content.part", part={"type": "think", "think": "hm"}),
            _kc_usage(15), _kc_usage(16),
            _kc_event(20, "tool.result", toolCallId="c", result={"output": "ok"}),
            _kc(23, "context.append_message", message={"role": "assistant", "content": []}), _kc_usage(24),
            _kc(30, "context.append_message", message={"role": "tool", "toolCallId": "c"}),
            _kc_event(34, "tool.call", toolCallId="c", name="Read", args={}), _kc_usage(35)]
    start = 40
    for kind in ("quota_exhausted", "rate_limit", "other"):
        for after_response in (False, True):
            response = _kc_event(start + 3, "content.part", part={"type": "text", "text": "hi"})
            failure = _kc(start + (4 if after_response else 2), "llm.error", kind=kind, message="request failed")
            rows.extend([_kc(start + 1, "turn.prompt", input=[]),
                         *([response, failure] if after_response else [failure, response]), _kc_usage(start + 5)])
            start += 10
    rows.extend([_kc(start + 1, "turn.prompt", input=[]),
                 _kc_event(start + 2, "content.part", part={"type": "text", "text": "ok"}), _kc_usage(start + 3)])
    out = parse.parse_file(KEY, _wire(rows))
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
