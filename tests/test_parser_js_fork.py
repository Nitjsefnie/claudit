"""The browser's codex fork handling, end to end through node.

The fork fixtures' browser pins: the replayed prefix is marked isReplay
(issue #687), the head-scan fork flag reads the FIRST real session_meta,
and a parent + fork loaded together agree on the parent's model (issue
#713) — the dedup winners are the parent's originals, in both load
orders. Split from test_parser_js_lanes.py, whose size ratchet the fork
browser tests outgrew.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LANES_JS = ROOT / "src" / "parser-lanes.js"
CODEX_JS = ROOT / "src" / "parser-codex.js"
LOADER_JS = ROOT / "src" / "pricing-loader.js"
PARSER_JS = ROOT / "src" / "parser.js"
RECORD_DEDUP_JS = ROOT / "src" / "record-dedup.js"
FIX_CODEX = ROOT / "fixtures" / "codex"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available"
)

DIFFERENT_MODEL_FORK = FIX_CODEX / "rollout_fork_different_model.jsonl"

_FORK_NODE_HEAD = f"""
      global.window = {{}};
      require({str(LANES_JS)!r});
      require({str(CODEX_JS)!r});
      require({str(LOADER_JS)!r}); require({str(PARSER_JS)!r});"""


def test_the_browser_marks_a_forks_replayed_prefix():
    """The codex lane parser stamps a fork's leading entries isReplay."""
    script = _FORK_NODE_HEAD + f"""
      const text = {json.dumps(DIFFERENT_MODEL_FORK.read_text(encoding="utf-8"))};
      const {{ events, meta }} = window.parseTranscript(text);
      console.log(JSON.stringify(meta.filter(m => m.type === 'assistant_usage')
        .map(m => [m.line, m.isReplay === true])));
    """
    proc = subprocess.run(["node", "-e", script], capture_output=True,
                          text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == [[3, True], [4, True], [7, False]]


def test_the_browser_fork_scan_advances_past_a_needle_mention():
    """The mirror of the backend's fork-advance edge (issue #687 delta):
    a needle mention does not decide the fork flag - the first real meta
    does."""
    mention = ('{"timestamp":"2026-06-14T12:00:00.000Z","type":"event_msg",'
               '"payload":{"type":"agent_message",'
               '"message":"roles: [\\"session_meta\\"]"}}\n')
    fixture = DIFFERENT_MODEL_FORK.read_text(encoding="utf-8")
    text = mention + fixture
    # Lockstep with backend _nonempty_str: a whitespace-only forked_from_id
    # is a non-empty string, so the fork flag fires on both sides.
    ws = fixture.replace('"forked_from_id":"00000000-0000-4000-8000-000000000003"',
                         '"forked_from_id":" "')
    script = _FORK_NODE_HEAD + f"""
      const verdict = (text) => window.parseTranscript(text).meta
        .filter(m => m.type === 'assistant_usage').map(m => [m.line, m.isReplay === true]);
      console.log(JSON.stringify([verdict({json.dumps(text)}), verdict({json.dumps(ws)})]));
    """
    proc = subprocess.run(["node", "-e", script], capture_output=True,
                          text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == [
        [[4, True], [5, True], [8, False]], [[3, True], [4, True], [7, False]]]


def test_the_browser_parent_and_fork_agree_on_the_parents_model():
    """Issue #713's browser half: a lone fork file cannot know the model
    its replayed prefix ran on (the parent is a different file; a lone
    parse keeps the #653 fallback — SV-PARSER-SPEC records why), but with
    a parent and its fork loaded together the dedup winner of a replayed
    uuid is the parent's original, so the surviving entries carry the
    model the parent had in force. Pinned in BOTH load orders: the
    arrival order never decides, the rank does."""
    s = "00000000-0000-4000-8000-000000000001"

    def tc(ts, cum, last):
        return {"timestamp": ts, "type": "event_msg",
                "payload": {"type": "token_count",
                            "info": {"total_token_usage": {
                                "input_tokens": cum[0],
                                "cached_input_tokens": cum[1],
                                "cache_write_input_tokens": 0,
                                "output_tokens": cum[2],
                                "reasoning_output_tokens": cum[3],
                                "total_tokens": cum[0] + cum[2]},
                                "last_token_usage": {
                                "input_tokens": last[0],
                                "cached_input_tokens": last[1],
                                "cache_write_input_tokens": 0,
                                "output_tokens": last[2],
                                "reasoning_output_tokens": last[3],
                                "total_tokens": last[0] + last[2]}}}}

    parent_lines = [
        {"timestamp": "2026-06-14T11:00:01.000Z", "type": "session_meta",
         "payload": {"session_id": s,
                     "id": "00000000-0000-4000-8000-000000000003",
                     "cwd": "/workspace/toy-project",
                     "originator": "codex-tui", "cli_version": "1.0.0"}},
        {"timestamp": "2026-06-14T11:00:02.000Z", "type": "turn_context",
         "payload": {"model": "gpt-5.6-sol"}},
        tc("2026-06-14T11:00:03.000Z",
           (100000, 98000, 500, 300), (100000, 98000, 500, 300)),
        {"timestamp": "2026-06-14T11:00:04.000Z", "type": "turn_context",
         "payload": {"model": "gpt-5.6-terra"}},
        tc("2026-06-14T11:00:05.000Z",
           (109800, 107604, 1120, 520), (9800, 9604, 620, 220)),
    ]
    parent_text = "".join(
        json.dumps(lne, separators=(",", ":")) + "\n" for lne in parent_lines)
    fork_text = DIFFERENT_MODEL_FORK.read_text(encoding="utf-8")

    script = _FORK_NODE_HEAD + f"""
      require({str(ROOT / 'src' / 'record-dedup.js')!r});
      const parent = {json.dumps(parent_text)};
      const fork = {json.dumps(fork_text)};
      const replayed = ['{s}:100500', '{s}:110920'];
      const run = (texts) => {{
        const seen = new Map();
        const allMeta = [];
        for (const text of texts) {{
          const {{ meta }} = window.parseTranscript(text, {{ seenUuids: seen }});
          allMeta.push(...meta.filter(m => m.type === 'assistant_usage'));
        }}
        window.recordDedup.dropMasked(allMeta, seen);
        return allMeta.filter(m => replayed.includes(m.uuid))
          .map(m => [m.uuid.slice(-6), m.model, m.isReplay === true])
          .sort();
      }};
      console.log(JSON.stringify([run([parent, fork]), run([fork, parent])]));
    """
    proc = subprocess.run(["node", "-e", script], capture_output=True,
                          text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == [
        [["100500", "gpt-5.6-sol", False],
         ["110920", "gpt-5.6-terra", False]],
    ] * 2
