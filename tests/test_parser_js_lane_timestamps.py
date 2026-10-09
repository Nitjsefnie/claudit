"""Timestamp semantics shared by the backend and browser lane parser."""
from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime

from backend import parse
from tests.test_parser_js_lanes import (
    CODEX_JS, LANES_JS, LOADER_JS, PARSER_JS, RATES_JS, _NAIVE_LANE_BLOB,
)


def test_lane_parser_reads_an_offset_less_timestamp_as_utc():
    """A legacy Kimi timestamp without an offset resolves as UTC."""
    out = parse.parse_file("sessions/p/s/naive.jsonl", _NAIVE_LANE_BLOB)
    assert len(out["records"]) == 1
    assert out["records"][0]["ts"] == datetime(
        2026, 9, 10, 0, 30, tzinfo=UTC)

    script = f"""
      global.window = {{}};
      require({str(LANES_JS)!r});
      require({str(CODEX_JS)!r});
      require({str(LOADER_JS)!r}); require({str(RATES_JS)!r}); require({str(PARSER_JS)!r});
      const {{ events, meta }} = window.parseTranscriptLanes(
        {json.dumps(_NAIVE_LANE_BLOB.decode())});
      const usage = meta.find((m) => m.type === 'assistant_usage');
      console.log(JSON.stringify({{ usageTs: usage && usage.ts }}));
    """
    env = {**os.environ, "TZ": "Europe/Berlin"}
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60,
        env=env, check=False,  # Return code checked by hand on the next line.
    )
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout)
    assert got["usageTs"] == "2026-09-10T00:30:00.000Z"
