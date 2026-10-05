"""Non-object JSONL records and content blocks are skipped consistently."""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from backend import parse


ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "fixtures" / "parser"
LANES_JS = ROOT / "src" / "parser-lanes.js"
LOADER_JS = ROOT / "src" / "pricing-loader.js"
PARSER_JS = ROOT / "src" / "parser.js"

LINE_FIXTURES = {
    "non_object_lines.jsonl": [3, 4, 5, 6, 7],
    "non_object_lines_kimi_code.jsonl": [5, 6, 7, 8, 9],
    "non_object_lines_kimi_legacy.jsonl": [5, 6, 7, 8, 9],
}

NESTED_FIXTURES = {
    "non_object_nested_claude.jsonl": [2],
    "non_object_nested_kimi_code.jsonl": [3, 4],
    "non_object_nested_kimi_legacy.jsonl": [3],
}

CODEX_JUNK_LINES = [3, 7, 8]
KIMI_CODE_TOOL_CALL_JUNK_LINES = [8, 9]


def _backend_prompt_lines(text: str, parsed: dict[str, Any]) -> list[int]:
    """Map stored prompt timestamps to their source record lines."""
    timestamp_lines: dict[int, int] = {}
    for line_num, line in enumerate(text.splitlines(), 1):
        if not line:
            continue
        record = json.loads(line)
        if not isinstance(record, dict):
            continue
        if record.get("type") == "user":
            timestamp = record.get("timestamp")
            milliseconds = False
        elif record.get("type") in ("turn.prompt", "turn.steer"):
            timestamp = record.get("time")
            milliseconds = True
        elif (isinstance(record.get("message"), dict)
              and record["message"].get("type") == "TurnBegin"):
            timestamp = record.get("timestamp")
            milliseconds = False
        else:
            continue
        key = _timestamp_key(timestamp, milliseconds)
        if key is not None:
            assert key not in timestamp_lines, f"duplicate prompt timestamp: {key}"
            timestamp_lines[key] = line_num

    lines: list[int] = []
    for timestamp in parsed["prompt_ts"]:
        if timestamp is None:
            continue
        key = _timestamp_key(timestamp)
        assert key in timestamp_lines, f"prompt timestamp has no source line: {timestamp}"
        lines.append(timestamp_lines[key])
    return lines


def _timestamp_key(value: object, milliseconds: bool = False) -> int | None:
    if isinstance(value, (int, float)):
        seconds = value / 1000 if milliseconds else value
    elif isinstance(value, str) and value:
        seconds = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        ).timestamp()
    else:
        return None
    return round(seconds * 1_000_000)


def _without_lines(text: str, line_numbers: list[int]) -> bytes:
    lines = text.splitlines()
    for line_num in line_numbers:
        lines[line_num - 1] = ""
    return "\n".join(lines).encode("ascii")


@pytest.mark.parametrize("name", LINE_FIXTURES)
def test_backend_skips_non_object_lines_without_renumbering(name):
    text = (FIX / name).read_text(encoding="ascii")
    file_key = f"sessions/p/s/{name}"
    actual = parse.parse_file(file_key, text.encode("ascii"))
    expected = parse.parse_file(
        file_key, _without_lines(text, LINE_FIXTURES[name])
    )

    assert expected["records"]
    assert expected["tool_uses"]
    assert expected["prompt_count"] > 0
    prompt_lines = _backend_prompt_lines(text, expected)
    assert any(line < LINE_FIXTURES[name][0] for line in prompt_lines)
    assert any(line > LINE_FIXTURES[name][-1] for line in prompt_lines)
    assert any(row["line_num"] < LINE_FIXTURES[name][0]
               for row in expected["records"])
    assert any(row["line_num"] > LINE_FIXTURES[name][-1]
               for row in expected["records"])
    assert actual == expected


def test_backend_kimi_code_skips_a_non_object_content_part():
    name = "non_object_lines_kimi_code.jsonl"
    text = (FIX / name).read_text(encoding="ascii")
    actual_blob = _without_lines(text, LINE_FIXTURES[name])
    actual = parse.parse_file(f"sessions/p/s/{name}", actual_blob)

    lines = actual_blob.decode("ascii").splitlines()
    part_line = json.loads(lines[9])
    part_line["event"]["part"] = {}
    lines[9] = json.dumps(part_line, separators=(",", ":"))
    expected = parse.parse_file(
        f"sessions/p/s/{name}", "\n".join(lines).encode("ascii")
    )

    assert expected["records"]
    assert expected["tool_uses"]
    assert actual == expected


@pytest.mark.parametrize("name", NESTED_FIXTURES)
def test_backend_nested_non_object_maps_match_empty_lines(name):
    text = (FIX / name).read_text(encoding="ascii")
    file_key = f"sessions/p/s/{name}"
    actual = parse.parse_file(file_key, text.encode("ascii"))
    expected = parse.parse_file(
        file_key, _without_lines(text, NESTED_FIXTURES[name])
    )

    assert expected["records"]
    assert expected["tool_uses"]
    assert expected["prompt_count"] == 2
    prompt_lines = _backend_prompt_lines(text, expected)
    assert prompt_lines
    assert any(line < NESTED_FIXTURES[name][0] for line in prompt_lines)
    assert any(line > NESTED_FIXTURES[name][-1] for line in prompt_lines)
    assert actual == expected


def test_backend_kimi_code_non_object_loop_event_matches_empty_line():
    name = "non_object_nested_kimi_code.jsonl"
    text = (FIX / name).read_text(encoding="ascii")
    lines = text.splitlines()
    message_line = json.loads(lines[2])
    message_line["message"] = {}
    lines[2] = json.dumps(message_line, separators=(",", ":"))
    event_text = "\n".join(lines)
    actual = parse.parse_file(
        f"sessions/p/s/{name}", event_text.encode("ascii")
    )
    expected = parse.parse_file(
        f"sessions/p/s/{name}",
        _without_lines(event_text, [4]),
    )

    assert expected["records"]
    assert expected["tool_uses"]
    assert expected["prompt_count"] == 2
    assert actual == expected


def test_backend_null_user_blocks_keep_prompts_and_latency_anchors():
    name = "null_user_content_block.jsonl"
    text = (FIX / name).read_text(encoding="ascii")
    parsed = parse.parse_file(f"k/s/{name}", text.encode("ascii"))

    assert parsed["prompt_count"] == 2
    assert _backend_prompt_lines(text, parsed) == [1, 4]
    assert [row["reply_latency_s"] for row in parsed["records"]] == [10, 10]
    assert parsed["tool_uses"]
    assert parsed["tool_uses"][0]["error_text"] == "tool failure"


def _node_parse(transcripts: dict[str, str]) -> dict[str, Any]:
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    script = f"""
      global.window = {{}};
      require({json.dumps(str(LANES_JS))});
      require({json.dumps(str(LOADER_JS))});
      require({json.dumps(str(PARSER_JS))});
      const fixtures = {json.dumps(transcripts)};
      const out = {{}};
      for (const [name, text] of Object.entries(fixtures)) {{
        try {{
          const {{ events, meta }} = window.parseTranscript(text);
          out[name] = {{
            lines: events.filter(e => e.type === 'user_message')
                         .map(e => e.line),
            userMsgs: window.computeSessionStats(events, meta).userMsgs,
            usageCount: meta.filter(m => m.type === 'assistant_usage').length,
            rateLimits: meta.filter(m => m.type === 'rate_limit')
                            .map(m => m.line),
            parseErrors: events.filter(e => e.type === 'parse_error').length,
            parsedLines: events.concat(meta).map(e => e.line),
            toolCalls: events.filter(e => e.type === 'tool_call').length,
            toolResults: events.filter(e => e.type === 'tool_result')
                             .map(e => e.detail),
          }};
        }} catch (error) {{
          out[name] = {{ error: String(error), stack: error.stack }};
        }}
      }}
      console.log(JSON.stringify(out));
    """
    proc = subprocess.run(
        ["node", "-"], input=script, capture_output=True, text=True,
        encoding="utf-8", timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_browser_skips_non_object_lines_and_matches_backend():
    texts = {
        name: (FIX / name).read_text(encoding="ascii")
        for name in LINE_FIXTURES
    }
    got = _node_parse(texts)

    for name, text in texts.items():
        assert "error" not in got[name], got[name].get("stack", got[name])
        parsed = parse.parse_file(f"k/s/{name}", text.encode("ascii"))
        prompt_lines = _backend_prompt_lines(text, parsed)
        assert prompt_lines, name
        assert parsed["records"], name
        assert got[name]["lines"] == prompt_lines, name
        assert got[name]["userMsgs"] == parsed["prompt_count"], name
        assert got[name]["usageCount"] == len(parsed["records"]), name
        assert got[name]["parseErrors"] == 0, name
        assert not (set(got[name]["parsedLines"])
                    & set(LINE_FIXTURES[name])), name


def test_browser_nested_non_object_maps_match_backend_prompts():
    texts = {
        name: (FIX / name).read_text(encoding="ascii")
        for name in NESTED_FIXTURES
    }
    got = _node_parse(texts)

    for name, text in texts.items():
        assert "error" not in got[name], got[name].get("stack", got[name])
        parsed = parse.parse_file(f"sessions/p/s/{name}", text.encode("ascii"))
        prompt_lines = _backend_prompt_lines(text, parsed)
        assert prompt_lines, name
        assert parsed["records"], name
        assert parsed["tool_uses"], name
        assert parsed["prompt_count"] == 2, name
        assert got[name]["lines"] == prompt_lines, name
        assert got[name]["userMsgs"] == parsed["prompt_count"], name
        assert got[name]["usageCount"] == len(parsed["records"]), name
        assert got[name]["parseErrors"] == 0, name
        if name == "non_object_nested_kimi_code.jsonl":
            assert got[name]["toolCalls"] == len(parsed["tool_uses"]), name
        assert not (set(got[name]["parsedLines"])
                    & set(NESTED_FIXTURES[name])), name


def test_browser_null_user_content_blocks_match_backend():
    name = "null_user_content_block.jsonl"
    text = (FIX / name).read_text(encoding="ascii")
    got = _node_parse({name: text})[name]
    parsed = parse.parse_file(f"k/s/{name}", text.encode("ascii"))

    assert parsed["prompt_count"] == 2
    assert _backend_prompt_lines(text, parsed) == [1, 4]
    assert "error" not in got, got.get("stack", got)
    assert got["lines"] == [1, 4]
    assert got["userMsgs"] == 2
    assert got["usageCount"] == len(parsed["records"])


def test_browser_skips_null_assistant_content_blocks():
    name = "null_user_content_block.jsonl"
    lines = (FIX / name).read_text(encoding="ascii").splitlines()
    first_user = json.loads(lines[0])
    first_user["message"]["content"][0] = "ignored"
    lines[0] = json.dumps(first_user, separators=(",", ":"))
    got = _node_parse({"assistant-null": "\n".join(lines)})["assistant-null"]

    assert "error" not in got, got.get("stack", got)
    assert got["lines"] == [1, 4]
    assert got["userMsgs"] == 2
    assert got["usageCount"] == 2


def test_browser_tool_result_detail_skips_non_object_inner_values():
    name = "null_user_content_block.jsonl"
    lines = (FIX / name).read_text(encoding="ascii").splitlines()
    for line_num in (1, 2, 3, 4, 5):
        record = json.loads(lines[line_num - 1])
        content = record["message"].get("content")
        if isinstance(content, list):
            content[0] = {}
        lines[line_num - 1] = json.dumps(record, separators=(",", ":"))
    text = "\n".join(lines)
    got = _node_parse({"tool-result-null": text})["tool-result-null"]
    backend = parse.parse_file("k/s/null_tool_result.jsonl", text.encode("ascii"))

    assert "error" not in got, got.get("stack", got)
    assert backend["tool_uses"]
    assert backend["tool_uses"][0]["error_text"] == "tool failure"
    assert got["toolResults"] == [backend["tool_uses"][0]["error_text"]]


def test_browser_kimi_tool_result_detail_skips_non_object_inner_values():
    name = "non_object_tool_result_kimi_code.jsonl"
    text = (FIX / name).read_text(encoding="ascii")
    got = _node_parse({name: text})[name]
    backend = parse.parse_file(f"sessions/p/s/{name}", text.encode("ascii"))

    assert "error" not in got, got.get("stack", got)
    assert backend["tool_uses"]
    assert backend["tool_uses"][0]["error_text"] == "tool failure"
    assert _backend_prompt_lines(text, backend) == [2]
    assert got["lines"] == [2]
    assert got["userMsgs"] == backend["prompt_count"]
    assert got["usageCount"] == len(backend["records"])
    assert got["toolResults"] == [backend["tool_uses"][0]["error_text"]]


def test_browser_kimi_tool_result_content_matches_backend_filtering():
    name = "non_object_kimi_context_tool_result.jsonl"
    text = (FIX / name).read_text(encoding="ascii")
    got = _node_parse({name: text})[name]
    backend = parse.parse_file(f"sessions/p/s/{name}", text.encode("ascii"))

    assert "error" not in got, got.get("stack", got)
    assert backend["tool_uses"]
    assert backend["tool_uses"][0]["error_text"] == "fallback"
    assert got["toolResults"] == ["fallback", "fallback"]


def _parse_codex_file(file_key: str, blob: bytes) -> dict:
    """parse_file minus the issue-653 refusal: this module's codex
    fixture carries no model declaration, which the entry point refuses;
    these tests pin shape guards, not attribution."""
    return parse.to_claudit(
        parse.LANE_PARSERS["codex"](file_key, blob), "codex")


def test_backend_codex_nested_shape_guards_match_empty_lines():
    name = "non_object_codex_item_content.jsonl"
    text = (FIX / name).read_text(encoding="ascii")
    actual = _parse_codex_file(f"sessions/p/s/{name}", text.encode("ascii"))
    expected = _parse_codex_file(
        f"sessions/p/s/{name}", _without_lines(text, CODEX_JUNK_LINES)
    )

    assert expected["records"]
    assert [row["line_num"] for row in expected["records"]] == [4, 6]
    assert expected["prompt_count"] == 0
    assert actual == expected


def test_browser_codex_non_array_item_content_matches_backend():
    name = "non_object_codex_item_content.jsonl"
    text = (FIX / name).read_text(encoding="ascii")
    variants = {}
    for label, content in (("number", 123), ("boolean", True), ("object", {})):
        lines = text.splitlines()
        item_line = json.loads(lines[4])
        item_line["payload"]["item"]["content"] = content
        lines[4] = json.dumps(item_line, separators=(",", ":"))
        variants[f"{name}:{label}"] = "\n".join(lines)
    got = _node_parse(variants)

    for variant, variant_text in variants.items():
        parsed = _parse_codex_file(
            f"sessions/p/s/{name}", variant_text.encode("ascii")
        )
        expected = _parse_codex_file(
            f"sessions/p/s/{name}",
            _without_lines(variant_text, CODEX_JUNK_LINES),
        )
        assert parsed == expected, variant
        assert parsed["records"], variant
        assert parsed["prompt_count"] == 0, variant
        assert "error" not in got[variant], got[variant].get("stack", got[variant])
        assert got[variant]["lines"] == [], variant
        assert got[variant]["userMsgs"] == parsed["prompt_count"], variant
        assert got[variant]["usageCount"] == len(parsed["records"]) > 0, variant
        assert got[variant]["parseErrors"] == 0, variant


def test_codex_non_object_rate_limit_primary_matches_empty_map():
    name = "non_object_codex_item_content.jsonl"
    lines = (FIX / name).read_text(encoding="ascii").splitlines()
    record = json.loads(lines[2])
    record["payload"]["rate_limits"] = {
        "rate_limit_reached_type": "fixture_limit", "primary": 123,
    }
    lines[2] = json.dumps(record, separators=(",", ":"))
    malformed_primary = "\n".join(lines)
    empty_primary_record = json.loads(lines[2])
    empty_primary_record["payload"]["rate_limits"].pop("primary")
    lines[2] = json.dumps(empty_primary_record, separators=(",", ":"))
    empty_primary = "\n".join(lines)

    actual = _parse_codex_file(
        f"sessions/p/s/{name}", malformed_primary.encode("ascii")
    )
    expected = _parse_codex_file(
        f"sessions/p/s/{name}", empty_primary.encode("ascii")
    )
    got = _node_parse({name: malformed_primary})[name]

    assert actual["rate_limit_hits"]
    assert actual == expected
    assert "error" not in got, got.get("stack", got)
    assert got["rateLimits"] == [hit["line"] for hit in actual["rate_limit_hits"]]


def test_backend_kimi_code_tool_call_guards_match_empty_lines():
    name = "non_object_nested_kimi_code.jsonl"
    text = (FIX / name).read_text(encoding="ascii")
    actual = parse.parse_file(f"sessions/p/s/{name}", text.encode("ascii"))
    expected = parse.parse_file(
        f"sessions/p/s/{name}",
        _without_lines(text, KIMI_CODE_TOOL_CALL_JUNK_LINES),
    )

    assert expected["records"]
    assert expected["tool_uses"]
    assert expected["prompt_count"] == 2
    assert actual == expected


def test_kimi_code_non_object_usage_matches_empty_usage():
    name = "non_object_nested_kimi_code.jsonl"
    lines = (FIX / name).read_text(encoding="ascii").splitlines()
    record = json.loads(lines[6])
    record["usage"] = 123
    lines[6] = json.dumps(record, separators=(",", ":"))
    malformed_usage = "\n".join(lines)
    empty_usage_record = json.loads(lines[6])
    empty_usage_record["usage"] = {}
    lines[6] = json.dumps(empty_usage_record, separators=(",", ":"))
    empty_usage = "\n".join(lines)

    actual = parse.parse_file(
        f"sessions/p/s/{name}", malformed_usage.encode("ascii")
    )
    expected = parse.parse_file(
        f"sessions/p/s/{name}", empty_usage.encode("ascii")
    )
    got = _node_parse({name: malformed_usage})[name]

    assert actual["records"]
    assert actual["prompt_count"] == 2
    assert actual == expected
    assert "error" not in got, got.get("stack", got)
    assert got["lines"] == _backend_prompt_lines(malformed_usage, actual)
    assert got["userMsgs"] == actual["prompt_count"]
    assert got["usageCount"] == len(actual["records"]) > 0


def test_claude_non_object_usage_matches_empty_usage():
    name = "non_object_nested_claude.jsonl"
    lines = (FIX / name).read_text(encoding="ascii").splitlines()
    record = json.loads(lines[2])
    record["message"]["usage"] = 123
    lines[2] = json.dumps(record, separators=(",", ":"))
    malformed_usage = "\n".join(lines)
    empty_usage_record = json.loads(lines[2])
    empty_usage_record["message"]["usage"] = {}
    lines[2] = json.dumps(empty_usage_record, separators=(",", ":"))
    empty_usage = "\n".join(lines)

    actual = parse.parse_file(
        f"sessions/p/s/{name}", malformed_usage.encode("ascii")
    )
    expected = parse.parse_file(
        f"sessions/p/s/{name}", empty_usage.encode("ascii")
    )
    got = _node_parse({name: malformed_usage})[name]

    assert actual["records"]
    assert actual["prompt_count"] == 2
    assert actual == expected
    assert "error" not in got, got.get("stack", got)
    assert got["lines"] == _backend_prompt_lines(malformed_usage, actual)
    assert got["userMsgs"] == actual["prompt_count"]
    assert got["usageCount"] == len(actual["records"]) > 0
