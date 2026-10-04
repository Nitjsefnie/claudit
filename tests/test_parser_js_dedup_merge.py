"""SV-PARSER-SPEC: the dedup retraction keeps a requestId-merged winner's
usage (issue #568). When the canonical winner shares the superseded copy's
requestId, the streaming merge folds the winner's usage into the FIRST
fragment's entry, which sits on the superseded copy's line -- without the
re-point, the retraction drops the uuid's only usage entry wholesale and
computeSessionStats reports no turn, tokens or cost for the call.
"""
import json
import subprocess

from tests.test_parser_js_mirror import (  # pylint: disable=unused-import
    PARSER_JS, RECORD_DEDUP_JS, _claude_line, _node_dedup_survivor,
)


def _node_usage_identity(text: str) -> list[dict]:
    """The assistant_usage entries' identity after one dedup parse: the
    re-pointed entry must be attributable to the winner (line, uuid and
    model), with no merge bookkeeping left on the entry."""
    script = f"""
      global.window = {{}};
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


def test_dedup_shared_request_id_repoints_the_merged_entry_to_the_winner():
    """The re-pointed entry carries the winning line, uuid and model."""
    text = (_claude_line("u-1", None, "loser copy", 200, request_id="req-1")
            + _claude_line("u-1", "claude-sonnet-4-5", "winner copy", 300,
                           request_id="req-1"))
    assert _node_usage_identity(text) == [
        {"line": 2, "uuid": "u-1", "model": "claude-sonnet-4-5"}]
