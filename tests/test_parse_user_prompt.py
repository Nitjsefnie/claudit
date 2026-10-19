"""User-record prompt counting, one prompt per record.

The unit is ONE user record: it counts exactly one prompt when any
top-level content block is human — a text block passing the prompt
gate, or an image block. Images inside a ``tool_result`` are result
payload, never a prompt. The count, the per-prompt timestamp, the
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


@pytest.mark.parametrize("name,count,timestamps,record_count,latencies", [
    ("prompt_image_only.jsonl", 2,
     ["2026-06-23T10:00:00+00:00", "2026-06-23T10:00:20+00:00"], 2, [10.0, 10.0]),
    ("prompt_image_with_injected_text.jsonl", 1,
     ["2026-06-23T11:00:00+00:00"], None, [10.0]),
    ("prompt_multi_text_block.jsonl", 1, None, None, []),
], ids=["image-only", "image-with-injected-text", "multiple-text-blocks"])
def test_human_blocks_count_once_per_user_record(
        name, count, timestamps, record_count, latencies):
    out = parse.parse_file(f"k/sess-prompt/{name}", _read(name))
    assert out["prompt_count"] == count
    assert len(out["prompt_ts"]) == count
    if timestamps is not None:
        assert out["prompt_ts"] == timestamps
    if record_count is not None:
        assert len(out["records"]) == record_count
    for index, latency in enumerate(latencies):
        assert out["records"][index]["reply_latency_s"] == pytest.approx(latency)


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
