"""Claude reply timing: injected text anchors, failed attempts invalidate."""
from __future__ import annotations

from pathlib import Path

import pytest

from backend import parse

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "parser"


def test_injected_message_anchors_latency():
    out = parse.parse_file(
        "k/sess/sess.jsonl", (FIX / "injected_message_anchors_latency.jsonl").read_bytes())
    assert out["records"][0]["reply_latency_s"] == pytest.approx(10.0)
    assert out["prompt_count"] == 1
    assert out["prompt_ts"] == ["2026-06-23T10:00:00+00:00"]
    assert out["turn_count"] == 1
    assert out["ctx_turns"] == [{
        "idx": 1, "ts": "2026-06-23T11:00:10+00:00", "line": 3,
        "input": 100, "output": 10, "delta": 100,
    }]


def test_injected_list_message_anchors_latency():
    out = parse.parse_file(
        "k/sess/sess.jsonl", (FIX / "injected_list_message_anchors_latency.jsonl").read_bytes())
    assert out["records"][0]["reply_latency_s"] == pytest.approx(10.0)
    assert out["prompt_count"] == 1
    assert out["prompt_ts"] == ["2026-06-23T10:00:00+00:00"]
    assert out["turn_count"] == 1
    assert out["ctx_turns"] == [{
        "idx": 1, "ts": "2026-06-23T11:00:10+00:00", "line": 3,
        "input": 100, "output": 10, "delta": 100,
    }]


def test_api_error_nulls_reply_latency():
    out = parse.parse_file(
        "k/sess/sess.jsonl", (FIX / "api_error_nulls_reply_latency.jsonl").read_bytes())
    first, second = out["records"]
    assert first["reply_latency_s"] is None
    assert second["reply_latency_s"] == pytest.approx(10.0)
    assert "api_error" in first["turn_flags"]
    assert "api_error" not in second["turn_flags"]


def test_other_system_preserves_reply_latency():
    out = parse.parse_file(
        "k/sess/sess.jsonl", (FIX / "other_system_preserves_reply_latency.jsonl").read_bytes())
    assert out["records"][0]["reply_latency_s"] == pytest.approx(10.0)
