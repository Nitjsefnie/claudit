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
PARSER_JS = ROOT / "src" / "parser.js"
FIX = ROOT / "fixtures" / "parser"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available"
)


def _user_messages(name):
    script = f"""
      global.window = {{}};
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


def test_browser_prompt_image_only_record_counts_once():
    """A text prompt and a later image-only record each emit exactly one
    user_message event; the image detail names the attachment."""
    out = _user_messages("prompt_image_only.jsonl")
    assert out["details"] == ["look at this", "[image attachment]"]
    assert out["userMsgs"] == 2


def test_browser_prompt_image_with_injected_text_counts():
    """Injected-only text pushes nothing; the image still emits the
    record's single user_message event."""
    out = _user_messages("prompt_image_with_injected_text.jsonl")
    assert out["details"] == ["[image attachment]"]
    assert out["userMsgs"] == 1


def test_browser_prompt_multi_text_block_counts_once():
    """One record, many human text blocks: ONE event joining them, not
    one per block."""
    out = _user_messages("prompt_multi_text_block.jsonl")
    assert out["details"] == ["first half\n\nsecond half"]
    assert out["userMsgs"] == 1


def test_browser_prompt_image_inside_tool_result_never_counts():
    """An image inside a tool_result is result payload: no user_message
    event, and the tool result still lands."""
    out = _user_messages("prompt_image_in_tool_result.jsonl")
    assert out["details"] == []
    assert out["userMsgs"] == 0
    assert out["toolResults"] == 1
