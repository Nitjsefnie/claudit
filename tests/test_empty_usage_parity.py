"""Empty usage maps follow the backend parser in the browser parser."""
from __future__ import annotations

import json
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest

from backend import parse


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures" / "parser"
LANES_JS = ROOT / "src" / "parser-lanes.js"
LOADER_JS = ROOT / "src" / "pricing-loader.js"
PARSER_JS = ROOT / "src" / "parser.js"
EXPECTED = [
    ("empty_usage_record.jsonl", [3, 5], 2, 2),
    ("empty_usage_legacy_kimi.jsonl", [4], 1, 1),
]


@lru_cache(maxsize=1)
def _browser_results() -> dict[str, dict[str, Any]]:
    """Parse the empty-usage fixtures through the production browser code."""
    if shutil.which("node") is None:
        pytest.skip("node not available")
    fixtures = {
        name: (FIXTURES / name).read_text(encoding="utf-8")
        for name, _, _, _ in EXPECTED
    }
    script = f"""
      global.window = {{}};
      require({json.dumps(str(LANES_JS))});
      require({json.dumps(str(LOADER_JS))});
      require({json.dumps(str(PARSER_JS))});
      const fixtures = {json.dumps(fixtures)};
      const out = {{}};
      for (const [name, text] of Object.entries(fixtures)) {{
        const {{ events, meta }} = window.parseTranscript(text);
        out[name] = {{
          usage_lines: meta.filter(m => m.type === 'assistant_usage')
                           .map(m => m.line),
          user_msgs: window.computeSessionStats(events, meta).userMsgs,
        }};
      }}
      console.log(JSON.stringify(out));
    """
    proc = subprocess.run(
        ["node", "-"], input=script, capture_output=True, text=True,
        encoding="utf-8", timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.parametrize(
    ("name", "expected_lines", "expected_prompts", "expected_user_msgs"),
    EXPECTED,
)
def test_empty_usage_records_match_backend_and_browser(
    name: str,
    expected_lines: list[int],
    expected_prompts: int,
    expected_user_msgs: int,
) -> None:
    blob = (FIXTURES / name).read_bytes()
    backend = parse.parse_file(f"sessions/p/s/{name}", blob)
    backend_lines = [record["line_num"] for record in backend["records"]]
    browser = _browser_results()[name]

    assert backend_lines == expected_lines
    assert browser["usage_lines"] == expected_lines
    assert backend["prompt_count"] == expected_prompts
    assert browser["user_msgs"] == expected_user_msgs


def test_empty_claude_usage_consumes_reply_latency_anchor_before_skip() -> None:
    name = "empty_usage_record.jsonl"
    parsed = parse.parse_file(
        f"sessions/p/s/{name}", (FIXTURES / name).read_bytes()
    )

    assert [record["line_num"] for record in parsed["records"]] == [3, 5]
    assert [record["reply_latency_s"] for record in parsed["records"]] == [
        None, 5.0,
    ]
