"""Prompt-gate whitespace parity across the backend and browser."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend import parse
from backend import prompt_gate


ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "fixtures" / "parser"
LOADER_JS = ROOT / "src" / "pricing-loader.js"
RATES_JS = ROOT / "src" / "rates.js"
LANES_JS = ROOT / "src" / "parser-lanes.js"
PARSER_JS = ROOT / "src" / "parser.js"

PROMPT_LINES = {
    "prompt_ws_bom_before_tag.jsonl": [1, 2, 3],
    "prompt_ws_nel_in_tag.jsonl": [1],
    "prompt_ws_c0_in_tag.jsonl": [1],
    "prompt_ws_bom_in_tag.jsonl": [1, 2, 3],
    "prompt_ws_c0_before_interrupt.jsonl": [1],
}

JS_WHITESPACE = (
    set(range(0x09, 0x0E))
    | {
        0x20, 0xA0, 0x1680, 0x2028, 0x2029, 0x202F, 0x205F, 0x3000,
        0xFEFF,
    }
    | set(range(0x2000, 0x200B))
)


def _backend_prompt_lines(text: str, parsed: dict) -> list[int]:
    """Map stored prompt timestamps back to their user-record lines."""
    timestamp_lines = {}
    for line_num, line in enumerate(text.splitlines(), 1):
        record = json.loads(line)
        if record.get("type") == "user":
            timestamp = datetime.fromisoformat(
                record["timestamp"].replace("Z", "+00:00")
            ).isoformat()
            assert timestamp not in timestamp_lines, (
                f"duplicate user-record timestamp {timestamp}"
            )
            timestamp_lines[timestamp] = line_num
    return [timestamp_lines[ts] for ts in parsed["prompt_ts"]]


def _node_prompt_results(transcripts: dict[str, str]) -> dict:
    """Parse transcripts with the real browser parser in one Node process."""
    script = f"""
      global.window = {{}};
      require({str(LANES_JS)!r});
      require({str(LOADER_JS)!r});
      require({str(RATES_JS)!r});
      require({str(PARSER_JS)!r});
      const fixtures = {json.dumps(transcripts)};
      const out = {{}};
      for (const [name, text] of Object.entries(fixtures)) {{
        const {{ events, meta }} = window.parseTranscript(text);
        out[name] = {{
          lines: events.filter(e => e.type === 'user_message')
                       .map(e => e.line),
          userMsgs: window.computeSessionStats(events, meta).userMsgs,
        }};
      }}
      console.log(JSON.stringify(out));
    """
    # The generated sweep exceeds Windows' command-line length limit.
    proc = subprocess.run(
        ["node", "-"], input=script, capture_output=True, text=True,
        encoding="utf-8", timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _make_transcript(texts: list[str]) -> str:
    """Encode user texts as one valid, ordered JSONL transcript."""
    base = datetime(2026, 9, 20, tzinfo=timezone.utc)
    records = []
    for index, text in enumerate(texts):
        timestamp = (base + timedelta(seconds=index)).isoformat().replace(
            "+00:00", "Z"
        )
        records.append({
            "type": "user",
            "timestamp": timestamp,
            "uuid": f"ws-sweep-u{index}",
            "message": {"role": "user", "content": text},
        })
    return "\n".join(
        json.dumps(record, ensure_ascii=True, separators=(",", ":"))
        for record in records
    )


@pytest.mark.parametrize(
    ("name", "want_lines"),
    PROMPT_LINES.items(),
    ids=[name.removesuffix(".jsonl") for name in PROMPT_LINES],
)
def test_backend_prompt_gate_whitespace_fixtures(name, want_lines):
    """The backend's stored prompt timestamps match the pinned lines."""
    text = (FIX / name).read_text(encoding="ascii")
    parsed = parse.parse_file(f"k/s/{name}", text.encode("ascii"))
    assert parsed["prompt_count"] == len(want_lines), name
    assert _backend_prompt_lines(text, parsed) == want_lines, name


def test_backend_prompt_line_mapping_rejects_duplicate_user_timestamps():
    """Duplicate user timestamps must not silently overwrite a line."""
    records = [
        json.loads(line)
        for line in _make_transcript(["first", "second"]).splitlines()
    ]
    records[1]["timestamp"] = records[0]["timestamp"]
    text = "\n".join(json.dumps(record) for record in records)

    with pytest.raises(AssertionError) as exc_info:
        _backend_prompt_lines(text, {"prompt_ts": []})

    assert "2026-09-20T00:00:00+00:00" in str(exc_info.value)


def test_backend_whitespace_class_matches_python_isspace():
    """The fixed class covers exactly Python's current isspace repertoire."""
    class_body = getattr(prompt_gate, "_PROMPT_WS_CLASS_BODY", None)
    assert class_body is not None, "explicit prompt whitespace class is missing"
    matcher = re.compile(f"[{class_body}]")
    expected = {
        codepoint for codepoint in range(0x110000)
        if chr(codepoint).isspace()
    }
    actual = {
        codepoint for codepoint in range(0x110000)
        if matcher.fullmatch(chr(codepoint))
    }
    assert expected
    assert actual
    assert actual == expected
    assert prompt_gate.lstrip_prompt_ws("\x1c\x85\ufeffx") == "\ufeffx"


def test_browser_prompt_gate_matches_backend_whitespace_fixtures():
    """Browser prompt lines and counts match the backend fixture results."""
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    texts = {
        name: (FIX / name).read_text(encoding="ascii")
        for name in PROMPT_LINES
    }
    got = _node_prompt_results(texts)
    for name, want_lines in PROMPT_LINES.items():
        assert want_lines, name
        parsed = parse.parse_file(
            f"k/s/{name}", (FIX / name).read_bytes()
        )
        backend_lines = _backend_prompt_lines(texts[name], parsed)
        assert backend_lines, name
        assert got[name]["lines"] == backend_lines, name
        assert got[name]["userMsgs"] == parsed["prompt_count"], name


def test_node_prompt_program_keeps_large_payload_off_argv(monkeypatch):
    """A Windows-sized transcript is sent as stdin, not command arguments."""
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["input"] = kwargs.get("input")
        return subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    transcript = _make_transcript(["x" * 40_000])
    assert len(transcript) > 32_767

    assert _node_prompt_results({"sweep": transcript}) == {}

    assert sum(len(arg) for arg in captured["args"]) < 1000
    assert json.dumps({"sweep": transcript}) in captured["input"]


def test_browser_prompt_gate_matches_generated_whitespace_sweep():
    """Generated whitespace, control and format characters stay in parity."""
    if shutil.which("node") is None:
        pytest.skip("node is not installed")
    python_whitespace = {
        codepoint for codepoint in range(0x110000)
        if chr(codepoint).isspace()
    }
    controls = set(range(0x20)) | set(range(0x7F, 0xA0))
    codepoints = sorted(
        python_whitespace
        | JS_WHITESPACE
        | controls
        | {0xFEFF, 0x200B}
    )
    assert codepoints
    texts = [
        shape
        for codepoint in codepoints
        for shape in (
            chr(codepoint) + "<unknown>x",
            "<unknown" + chr(codepoint) + "x>",
            chr(codepoint) + "[Request interrupted by user]",
        )
    ]
    transcript = _make_transcript(texts)
    parsed = parse.parse_file("k/s/whitespace-sweep.jsonl", transcript.encode())
    backend_lines = _backend_prompt_lines(transcript, parsed)
    assert backend_lines
    got = _node_prompt_results({"sweep": transcript})["sweep"]
    assert got["lines"] == backend_lines
    assert got["userMsgs"] == parsed["prompt_count"]
