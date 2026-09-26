"""User-record prompt counting, one prompt per record (issue #215).

The unit is ONE user record: it counts exactly one prompt when any
top-level content block is human — a text block passing the #213 prompt
gate, or an image block. Images inside a ``tool_result`` are result
payload, never a prompt. The count, the #214 per-prompt timestamp, the
ctx-turn boundary and the reply-latency anchor all land once per
record, exactly as they do for a plain text prompt (SV-PARSER-SPEC).
The browser parser (src/parser.js) applies the same rule; its parity is
pinned in tests/test_parser_js_user_prompt.py.
"""
from pathlib import Path

import pytest

from backend import parse

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "parser"


def _read(name):
    return (FIX / name).read_bytes()


def test_prompt_image_only_record_counts_once():
    """An image-only user record is ONE prompt: it counts toward
    prompt_count, carries its own #214 timestamp, anchors the reply
    latency and bounds a ctx turn, exactly like a text prompt."""
    out = parse.parse_file(
        "k/sess-img/sess-img.jsonl", _read("prompt_image_only.jsonl")
    )
    assert out["prompt_count"] == 2
    assert out["prompt_ts"] == [
        "2026-06-23T10:00:00+00:00",
        "2026-06-23T10:00:20+00:00",
    ]
    assert len(out["records"]) == 2
    assert out["records"][0]["reply_latency_s"] == pytest.approx(10.0)
    assert out["records"][1]["reply_latency_s"] == pytest.approx(10.0)


def test_prompt_image_with_injected_text_counts():
    """A record whose text blocks all fail the #213 gate still counts
    once when it carries an image: the image is the human block."""
    out = parse.parse_file(
        "k/sess-imgx/sess-imgx.jsonl",
        _read("prompt_image_with_injected_text.jsonl"),
    )
    assert out["prompt_count"] == 1
    assert out["prompt_ts"] == ["2026-06-23T11:00:00+00:00"]
    assert out["records"][0]["reply_latency_s"] == pytest.approx(10.0)


def test_prompt_multi_text_block_counts_once():
    """One record is ONE prompt regardless of how many human text
    blocks it carries — per-record, not per-block."""
    out = parse.parse_file(
        "k/sess-mtb/sess-mtb.jsonl",
        _read("prompt_multi_text_block.jsonl"),
    )
    assert out["prompt_count"] == 1
    assert len(out["prompt_ts"]) == 1


def test_prompt_image_inside_tool_result_never_counts():
    """An image arriving inside a tool_result block is result payload:
    it fills result_chars but is no prompt and anchors no latency."""
    out = parse.parse_file(
        "k/sess-imgtr/sess-imgtr.jsonl",
        _read("prompt_image_in_tool_result.jsonl"),
    )
    assert out["prompt_count"] == 0
    assert out["prompt_ts"] == []
    assert [r["reply_latency_s"] for r in out["records"]] == [None, None]
    assert out["tool_uses"][0]["result_chars"] == (
        len("cGljdHVyZQ==") + len("the screenshot")
    )
