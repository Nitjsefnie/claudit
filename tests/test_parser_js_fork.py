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
RATES_JS = ROOT / "src" / "rates.js"
FIX_CODEX = ROOT / "fixtures" / "codex"

DIFFERENT_MODEL_FORK = FIX_CODEX / "rollout_fork_different_model.jsonl"

_FORK_NODE_HEAD = f"""
      global.window = {{}};
      require({str(LANES_JS)!r});
      require({str(CODEX_JS)!r});
      require({str(LOADER_JS)!r}); require({str(RATES_JS)!r});
      require({str(PARSER_JS)!r});"""


@pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available")
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


@pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available")
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


@pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available")
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


def _tool_rollout(text, session_id, lines):
    base = [
        {"timestamp": "2026-06-14T11:00:01.000Z", "type": "session_meta",
         "payload": {"session_id": session_id,
                     "id": "00000000-0000-4000-8000-000000000003",
                     "cwd": "/workspace/toy-project",
                     "originator": "codex-tui", "cli_version": "1.0.0"}},
        {"timestamp": "2026-06-14T11:00:02.000Z", "type": "turn_context",
         "payload": {"model": text}},
    ]
    for ts, kind, call_id, body in lines:
        ts_iso = f"2026-06-14T11:00:{ts}.000Z"
        if kind == "call":
            payload = {"type": "custom_tool_call", "name": "exec",
                       "input": body}
            if call_id is not None:
                payload["call_id"] = call_id
        else:
            payload = {"type": "custom_tool_call_output", "output": body}
            if call_id is not None:
                payload["call_id"] = call_id
        base.append({"timestamp": ts_iso, "type": "response_item",
                     "payload": payload})
    return "".join(
        json.dumps(lne, separators=(",", ":")) + "\n" for lne in base)


@pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available")
def test_the_browser_dedups_a_forks_replayed_tool_calls():
    """Issue #766's browser half: with a parent and its fork loaded
    together, one copy of each replayed tool call survives — the parent's
    original, the same winner the DB's tool_uses.is_canonical keeps — and
    computeSessionStats does not double-count the replayed calls. The
    fork's own calls (its first declaration onward) and the empty-id calls
    (the NULL tool_use_id rows, always canonical) are never deduped. The
    record-side contract is test_the_browser_parent_and_fork_agree_on_
    the_parents_model's; this is the tool_use_id keyspace beside it."""
    parent = _tool_rollout(
        "gpt-5.6-sol", "00000000-0000-4000-8000-000000000001",
        [("03", "call", "c1", "alpha(1);"),
         ("04", "out", "c1", "ok"),
         ("05", "call", None, "gamma(3);"),
         ("06", "out", None, "ok")])
    # The fork: session_meta (forked_from_id) + replayed c1, then the
    # boundary turn_context, then its own c2 and an empty-id call.
    fork_lines = [
        {"timestamp": "2026-06-14T11:00:01.000Z", "type": "session_meta",
         "payload": {"session_id": "00000000-0000-4000-8000-000000000002",
                     "id": "00000000-0000-4000-8000-000000000005",
                     "forked_from_id": "00000000-0000-4000-8000-000000000003",
                     "parent_thread_id": "00000000-0000-4000-8000-000000000003",
                     "cwd": "/workspace/toy-project",
                     "originator": "codex-tui", "cli_version": "1.0.0"}},
        {"timestamp": "2026-06-14T11:00:02.000Z", "type": "event_msg",
         "payload": {"type": "agent_reasoning",
                     "text": "Replaying the parent's history."}},
        {"timestamp": "2026-06-14T11:00:03.000Z", "type": "response_item",
         "payload": {"type": "custom_tool_call", "name": "exec",
                     "input": "alpha(1)-REPLAY;", "call_id": "c1"}},
        {"timestamp": "2026-06-14T11:00:04.000Z", "type": "response_item",
         "payload": {"type": "custom_tool_call_output", "output": "replayed-ok",
                     "call_id": "c1"}},
        {"timestamp": "2026-06-14T11:00:05.000Z", "type": "turn_context",
         "payload": {"model": "gpt-5.6-terra"}},
        {"timestamp": "2026-06-14T11:00:06.000Z", "type": "event_msg",
         "payload": {"type": "agent_reasoning",
                     "text": "Picking up where the parent thread left off."}},
        {"timestamp": "2026-06-14T11:00:07.000Z", "type": "response_item",
         "payload": {"type": "custom_tool_call", "name": "exec",
                     "input": "beta(2);", "call_id": "c2"}},
        {"timestamp": "2026-06-14T11:00:08.000Z", "type": "response_item",
         "payload": {"type": "custom_tool_call_output", "output": "own-ok",
                     "call_id": "c2"}},
        {"timestamp": "2026-06-14T11:00:09.000Z", "type": "response_item",
         "payload": {"type": "custom_tool_call", "name": "exec",
                     "input": "delta(4);"}},
        {"timestamp": "2026-06-14T11:00:10.000Z", "type": "response_item",
         "payload": {"type": "custom_tool_call_output", "output": "ok"}},
    ]
    fork_text = "".join(
        json.dumps(lne, separators=(",", ":")) + "\n" for lne in fork_lines)

    script = _FORK_NODE_HEAD + f"""
      require({str(ROOT / 'src' / 'record-dedup.js')!r});
      const parent = {json.dumps(parent)};
      const fork = {json.dumps(fork_text)};
      const run = (texts) => {{
        const seen = new Map();
        const seenToolIds = new Map();
        const allEvents = [], allMeta = [];
        for (const text of texts) {{
          const {{ events, meta }} =
            window.parseTranscript(text, {{ seenUuids: seen, seenToolIds }});
          allEvents.push(...events);
          allMeta.push(...meta);
        }}
        window.recordDedup.dropMasked(allMeta, seen);
        window.recordDedup.dropMaskedTools(allEvents, seenToolIds);
        const stats = window.computeSessionStats(allEvents, allMeta);
        return {{
          calls: allEvents.filter(e => e.type === 'tool_call')
            .map(e => [e.tool_use_id, e.tool_input._raw, e.isReplay === true])
            .sort(),
          results: allEvents.filter(e => e.type === 'tool_result')
            .map(e => [e.tool_use_id, e.detail]).sort(),
          statsToolCalls: stats.toolCalls,
        }};
      }};
      console.log(JSON.stringify(
        [run([parent, fork]), run([fork, parent]), run([fork])]));
    """
    proc = subprocess.run(["node", "-e", script], capture_output=True,
                          text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    together = {
        "calls": sorted([
            ["c1", "alpha(1);", False],      # the parent's original wins
            ["c2", "beta(2);", False],       # the fork's own call survives
            ["", "delta(4);", False],        # NULL-identity: never deduped
            ["", "gamma(3);", False],
        ]),
        "results": sorted([
            ["c1", "ok"], ["c2", "own-ok"], ["", "ok"], ["", "ok"],
        ]),
        "statsToolCalls": 4,
    }
    lone_fork = {
        "calls": sorted([
            ["c1", "alpha(1)-REPLAY;", True],  # no parent: the fallback
            ["c2", "beta(2);", False],
            ["", "delta(4);", False],
        ]),
        "results": sorted([
            ["c1", "replayed-ok"], ["c2", "own-ok"], ["", "ok"],
        ]),
        "statsToolCalls": 3,
    }
    assert json.loads(proc.stdout) == [together, together, lone_fork]
