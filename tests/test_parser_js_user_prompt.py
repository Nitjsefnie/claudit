"""Browser-parser parity for the per-record prompt rule (issue #215).

src/parser.js must apply the same rule the backend pins in
tests/test_parse_user_prompt.py: ONE user_message event per user record
carrying at least one human block (a gate-passing text block or an
image block); images inside a tool_result never produce one. Driven
through node against the same fixtures the backend tests read
(SV-PARSER-SPEC lockstep).
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LOADER_JS = ROOT / "src" / "pricing-loader.js"
PARSER_JS = ROOT / "src" / "parser.js"
RATES_JS = ROOT / "src" / "rates.js"
LANES_JS = ROOT / "src" / "parser-lanes.js"
FIX = ROOT / "fixtures" / "parser"


def _user_messages(name):
    script = f"""
      global.window = {{}};
      require({str(LANES_JS)!r});
      require({str(LOADER_JS)!r});
      require({str(RATES_JS)!r});
      require({str(PARSER_JS)!r});
      const fs = require('fs');
      const text = fs.readFileSync({str(FIX / name)!r}, 'utf8');
      const {{ events, meta }} = window.parseTranscript(text);
      const msgs = events.filter(e => e.type === 'user_message');
      const stats = window.computeSessionStats(events, meta);
      console.log(JSON.stringify({{
        details: msgs.map(m => m.detail),
        userMsgs: stats.userMsgs,
        toolResults: stats.toolResults,
      }}));
    """
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
@pytest.mark.parametrize("fixture,details,user_msgs,tool_results", [
    pytest.param("prompt_image_only.jsonl", ["look at this", "[image attachment]"], 2, None, id="image-only"),
    pytest.param("prompt_image_with_injected_text.jsonl", ["[image attachment]"], 1, None, id="image-with-injected-text"),
    pytest.param("prompt_multi_text_block.jsonl", ["first half\n\nsecond half"], 1, None, id="multi-text-block"),
    pytest.param("prompt_image_in_tool_result.jsonl", [], 0, 1, id="image-in-tool-result"),
])
def test_browser_prompt_image_only_record_counts_once(fixture, details, user_msgs, tool_results):
    """Human blocks count once per record; tool-result images never count."""
    out = _user_messages(fixture)
    assert out["details"] == details
    assert out["userMsgs"] == user_msgs
    if tool_results is not None:
        assert out["toolResults"] == tool_results
