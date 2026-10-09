"""The browser lane parser stores the same web-search count as the backend."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from backend import parse
from tests.test_parser_js_lanes import (
    CODEX_JS, LANES_JS, LOADER_JS, PARSER_JS, RATES_JS, _backend_records,
    _browser_records,
)

ROOT = Path(__file__).resolve().parents[1]
CLAUDE_FIXTURE = ROOT / "fixtures" / "parser" / "web_search.jsonl"


@pytest.mark.parametrize("name", [
    "rollout_web_search.jsonl",
    "rollout_model_switch.jsonl", "kimi_single_turn.jsonl",
])
def test_browser_search_counts_match_each_lane_backend_record(name):
    backend = _backend_records(name)
    browser = _browser_records(name)
    assert [record["web_search_requests"] for record in browser] == [
        record["web_search_requests"] for record in backend]


@pytest.mark.parametrize("name", [
    "kimi_code_min.jsonl",
    "kimi_legacy_min.jsonl",
])
def test_each_kimi_wire_has_an_explicit_null_search_count(name):
    backend = _backend_records(name)
    browser = _browser_records(name)

    assert backend
    assert [record["web_search_requests"] for record in backend] == [
        None] * len(backend)
    assert [record["web_search_requests"] for record in browser] == [
        None] * len(browser)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_claude_server_search_count_matches_backend():
    blob = CLAUDE_FIXTURE.read_bytes()
    backend_counts = [record["web_search_requests"] for record in
                      parse.parse_file("claude/search.jsonl", blob)["records"]]
    script = f"""
      global.window = {{}};
      require({str(LANES_JS)!r});
      require({str(CODEX_JS)!r});
      require({str(LOADER_JS)!r});
      require({str(RATES_JS)!r});
      require({str(PARSER_JS)!r});
      const text = {json.dumps(blob.decode('utf-8'))};
      const {{ meta }} = window.parseTranscript(text);
      const records = meta.filter((item) => item.type === 'assistant_usage');
      console.log(JSON.stringify(records.map((record) =>
        record.web_search_requests ?? null)));
    """
    proc = subprocess.run(["node", "-e", script], capture_output=True,
                          text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == backend_counts
