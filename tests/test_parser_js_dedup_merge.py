"""SV-PARSER-SPEC: the dedup retraction keeps a requestId-merged winner's
usage (issue #568). When the canonical winner shares the superseded copy's
requestId, the streaming merge folds the winner's usage into the FIRST
fragment's entry, which sits on the superseded copy's line -- without the
re-point, the retraction drops the uuid's only usage entry wholesale and
computeSessionStats reports no turn, tokens or cost for the call.
"""
import json
import shutil
import subprocess

import pytest

from tests.test_parser_js_mirror import (
    LOADER_JS, PARSER_JS, RATES_JS, RECORD_DEDUP_JS, _claude_line,
    _node_dedup_survivor,
)


def _claude_msgid_line(uuid, model, text, output_tokens, msg_id):
    """A requestId-less line; message.id is the merge key when set, and
    with no id either the merge key is the empty string."""
    message = {"role": "assistant",
               "content": [{"type": "text", "text": text}],
               "usage": {"input_tokens": 100, "output_tokens": output_tokens}}
    if model is not None:
        message["model"] = model
    if msg_id is not None:
        message["id"] = msg_id
    return json.dumps({"type": "assistant", "timestamp": "2026-05-07T10:00:00Z",
                       "uuid": uuid, "sessionId": "sessS",
                       "message": message}, separators=(",", ":")) + "\n"


def _node_usage_identity(text: str) -> list[dict]:
    """The assistant_usage entries' identity after one dedup parse: the
    re-pointed entry must be attributable to the winner (line, uuid and
    model), with no merge bookkeeping left on the entry."""
    script = f"""
      global.window = {{}};
      require({str(LOADER_JS)!r});
      require({str(RATES_JS)!r});
      require({str(RECORD_DEDUP_JS)!r});
      require({str(PARSER_JS)!r});
      const {{ meta }} = window.parseTranscript({json.dumps(text)},
        {{ seenUuids: new Map() }});
      console.log(JSON.stringify(meta
        .filter(m => m.type === 'assistant_usage')
        .map(m => ({{ line: m.line, uuid: m.uuid, model: m.model }}))));
    """
    proc = subprocess.run(["node", "-e", script], capture_output=True,
                          text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_dedup_replace_with_shared_request_id_keeps_the_merged_usage():
    """issue #568: the winner shares the superseded copy's requestId, so
    the streaming merge folds the winner's usage into the loser-line entry
    -- the retraction re-points that entry to the winner's line (not drop
    the uuid's only usage), and the loser's events still leave."""
    text = (_claude_line("u-1", None, "loser copy", 200, tool="Read",
                         request_id="req-1")
            + _claude_line("u-1", "claude-sonnet-4-5", "winner copy", 300,
                           tool="Write", request_id="req-1"))
    assert _node_dedup_survivor(text) == {
        "toolCalls": 1, "toolNames": ["Write"], "texts": ["winner copy"],
        "usage": [{"model": "claude-sonnet-4-5", "output": 300}]}


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_dedup_replace_with_shared_request_id_winner_first_skips_the_merge():
    """The mirrored order: the attributed copy parses first, so the
    unattributed one is skipped outright (prev === true) before any usage
    merge -- one entry, the winner's own fragment, nothing folded in."""
    text = (_claude_line("u-1", "claude-sonnet-4-5", "winner copy", 200,
                         tool="Write", request_id="req-1")
            + _claude_line("u-1", None, "loser copy", 300, tool="Read",
                           request_id="req-1"))
    assert _node_dedup_survivor(text) == {
        "toolCalls": 1, "toolNames": ["Write"], "texts": ["winner copy"],
        "usage": [{"model": "claude-sonnet-4-5", "output": 200}]}


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_dedup_shared_request_id_repoints_the_merged_entry_to_the_winner():
    """The re-pointed entry carries the winning line, uuid and model."""
    text = (_claude_line("u-1", None, "loser copy", 200, request_id="req-1")
            + _claude_line("u-1", "claude-sonnet-4-5", "winner copy", 300,
                           request_id="req-1"))
    assert _node_usage_identity(text) == [
        {"line": 2, "uuid": "u-1", "model": "claude-sonnet-4-5"}]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_dedup_merge_key_msg_arm_spans_the_parsers_key():
    """A requestId-less transcript merges on message.id (parser.js's
    merge-key fallback); the retraction stamp carries the same key or the
    re-point silently disables -- the spanning control for the msg arm."""
    text = (_claude_msgid_line("u-1", None, "loser copy", 200, "msg-1")
            + _claude_msgid_line("u-1", "claude-sonnet-4-5", "winner copy",
                                 300, "msg-1"))
    assert _node_usage_identity(text) == [
        {"line": 2, "uuid": "u-1", "model": "claude-sonnet-4-5"}]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_dedup_empty_merge_key_never_re_points():
    """No requestId and no message.id: the merge key is empty, no merge
    can have folded a fragment in, so the masked line's entry must drop
    and the winner keeps its own entry -- one usage, not a double count."""
    text = (_claude_msgid_line("u-1", None, "loser copy", 200, None)
            + _claude_msgid_line("u-1", "claude-sonnet-4-5", "winner copy",
                                 300, None))
    assert _node_dedup_survivor(text) == {
        "toolCalls": 0, "toolNames": [], "texts": ["winner copy"],
        "usage": [{"model": "claude-sonnet-4-5", "output": 300}]}


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_dedup_re_point_prefers_the_entry_uuids_winner():
    """Two attributed lines share the loser's requestId (malformed for
    Claude), so all fragments merge into the one entry: it re-points to
    the entry's own uuid's winner, not the latest contributor."""
    text = (_claude_line("u-1", None, "loser copy", 200, request_id="req-1")
            + _claude_line("u-1", "claude-sonnet-4-5", "u-1 winner", 300,
                           request_id="req-1")
            + _claude_line("d-1", "claude-opus-4-8", "d-1 copy", 400,
                           request_id="req-1"))
    assert _node_usage_identity(text) == [
        {"line": 2, "uuid": "u-1", "model": "claude-sonnet-4-5"}]


def _claude_tool_line(uuid, request_id, blocks, results):
    """One assistant line carrying tool_use blocks beside its usage, or one
    user line carrying tool_result blocks (pick by what is given)."""
    if blocks is not None:
        message = {"role": "assistant",
                   "content": blocks + [{"type": "text", "text": "t"}],
                   "model": "claude-sonnet-4-5",
                   "usage": {"input_tokens": 100, "output_tokens": 10}}
    else:
        message = {"role": "user", "content": results}
    return json.dumps({"type": message["role"], "requestId": request_id,
                       "timestamp": "2026-05-07T10:00:00Z", "uuid": uuid,
                       "sessionId": "sessS", "message": message},
                      separators=(",", ":")) + "\n"


def _node_tool_dedup(main, sidecar):
    """The main file and the sidecar parsed in BOTH load orders through
    ONE shared seenUuids AND seenToolIds map (the multi-load mode),
    followed by the caller's post-load drops -- the #793 caller pattern,
    over the Claude path; then the lone sidecar, which has no main file
    to lose to."""
    script = f"""
      global.window = {{}};
      require({str(LOADER_JS)!r});
      require({str(RATES_JS)!r});
      require({str(RECORD_DEDUP_JS)!r});
      require({str(PARSER_JS)!r});
      const run = (texts) => {{
        const seen = new Map(), seenToolIds = new Map();
        const allEvents = [], allMeta = [];
        for (const text of texts) {{
          const {{ events, meta }} = window.parseTranscript(
            text, {{ seenUuids: seen, seenToolIds }});
          allEvents.push(...events);
          allMeta.push(...meta);
        }}
        window.recordDedup.dropMasked(allMeta, seen);
        window.recordDedup.dropMaskedTools(allEvents, seenToolIds);
        const stats = window.computeSessionStats(allEvents, allMeta);
        return {{
          calls: allEvents.filter(e => e.type === 'tool_call')
            .map(e => [e.tool_use_id, e.tool_name]).sort(),
          spawns: allEvents.filter(e => e.type === 'agent_spawn')
            .map(e => [e.tool_use_id, e.agent_name]).sort(),
          results: allEvents.filter(e => e.type === 'tool_result')
            .map(e => [e.tool_use_id, e.detail]).sort(),
          statsToolCalls: stats.toolCalls,
        }};
      }};
      const main = {json.dumps(main)}, sidecar = {json.dumps(sidecar)};
      console.log(JSON.stringify(
        [run([main, sidecar]), run([sidecar, main]), run([sidecar])]));
    """
    proc = subprocess.run(["node", "-e", script], capture_output=True,
                          text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_tool_use_id_dedup_splices_a_sidecar_s_replayed_tool_blocks():
    """Issue #795's browser half: a compaction sidecar replays the main
    file's tool_use blocks under new line uuids, so the line-uuid dedup
    cannot catch them; with the shared seenToolIds map passed, the same
    winner rule keys on tool_use_id (issue #766's rule, the Claude path
    now beside the codex one) -- the first-loaded copy wins (arrival order
    stands in for file_key), the losing copies leave call AND result
    together, and computeSessionStats does not double-count. An Agent
    dispatch is a tool_use block too (an agent_spawn event here, one
    tool_uses row in the DB), so it dedups the same way; empty tool_use_id
    (the NULL-identity rows) is never deduped; a lone file keeps its own
    copy."""
    main = (_claude_tool_line("u-1", "r-1",
                              [{"type": "tool_use", "id": "tu-1",
                                "name": "Bash", "input": {}}], None)
            + _claude_tool_line("u-2", "r-2", None,
                                [{"type": "tool_result",
                                  "tool_use_id": "tu-1", "content": "ok"}])
            + _claude_tool_line("u-3", "r-3",
                                [{"type": "tool_use", "id": "ag-1",
                                  "name": "Agent",
                                  "input": {"subagent_type": "Explore"}}], None)
            + _claude_tool_line("u-4", "r-4", None,
                                [{"type": "tool_result",
                                  "tool_use_id": "ag-1", "content": "done"}])
            + _claude_tool_line("u-5", "r-5",
                                [{"type": "tool_use", "id": "",
                                  "name": "Gamma", "input": {}}], None)
            + _claude_tool_line("u-6", "r-6", None,
                                [{"type": "tool_result",
                                  "tool_use_id": "", "content": "ok-g"}]))
    sidecar = (_claude_tool_line("v-1", "s-1",
                                 [{"type": "tool_use", "id": "tu-1",
                                   "name": "Bash", "input": {}}], None)
               + _claude_tool_line("v-2", "s-2", None,
                                   [{"type": "tool_result",
                                     "tool_use_id": "tu-1",
                                     "content": "replayed-ok"}])
               + _claude_tool_line("v-3", "s-3",
                                   [{"type": "tool_use", "id": "ag-1",
                                     "name": "Agent",
                                     "input": {"subagent_type": "Explore"}}], None)
               + _claude_tool_line("v-4", "s-4", None,
                                   [{"type": "tool_result",
                                     "tool_use_id": "ag-1",
                                     "content": "replayed-done"}])
               + _claude_tool_line("v-5", "s-5",
                                   [{"type": "tool_use", "id": "tu-2",
                                     "name": "Write", "input": {}}], None)
               + _claude_tool_line("v-6", "s-6", None,
                                   [{"type": "tool_result",
                                     "tool_use_id": "tu-2",
                                     "content": "written"}])
               + _claude_tool_line("v-7", "s-7",
                                   [{"type": "tool_use", "id": "",
                                     "name": "Delta", "input": {}}], None)
               + _claude_tool_line("v-8", "s-8", None,
                                   [{"type": "tool_result",
                                     "tool_use_id": "", "content": "ok-d"}]))
    # Both orders hold ONE copy per id and do not double-count; whose
    # payload survives follows the first-loaded copy (arrival order stands
    # in for file_key), so the results' detail text differs by order.
    calls = sorted([["", "Delta"], ["", "Gamma"],
                    ["tu-1", "Bash"], ["tu-2", "Write"]])
    spawns = [["ag-1", "Explore"]]
    together_main = {
        "calls": calls,
        "spawns": spawns,
        "results": sorted([["", "ok-d"], ["", "ok-g"], ["ag-1", "done"],
                           ["tu-1", "ok"], ["tu-2", "written"]]),
        "statsToolCalls": 5,
    }
    together_sidecar = {
        "calls": calls,
        "spawns": spawns,
        "results": sorted([["", "ok-d"], ["", "ok-g"],
                           ["ag-1", "replayed-done"], ["tu-1", "replayed-ok"],
                           ["tu-2", "written"]]),
        "statsToolCalls": 5,
    }
    lone = {
        "calls": sorted([["", "Delta"], ["tu-1", "Bash"], ["tu-2", "Write"]]),
        "spawns": [["ag-1", "Explore"]],
        "results": sorted([["", "ok-d"], ["ag-1", "replayed-done"],
                           ["tu-1", "replayed-ok"], ["tu-2", "written"]]),
        "statsToolCalls": 4,
    }
    assert _node_tool_dedup(main, sidecar) == [
        together_main, together_sidecar, lone]
