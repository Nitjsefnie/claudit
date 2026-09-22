"""Format sniffing and the lane-to-claudit row adapter (D2, D3).

claudit persists four transcript formats through one parse_file: Claude
Code transcripts (parsed in parse.py), Codex rollouts, and the two Kimi
wire formats (parsed by codexmeter's ported parsers, which keep
codexmeter's field names and row shapes). sniff_format names a blob's
format; to_claudit projects a lane parse onto claudit's records and
tool_uses columns.

Detection is codexmeter's: the first line that identifies a format
wins, Codex's rung is checked FIRST (its records also carry a
"timestamp" like legacy Kimi's, so a later rung must not claim one),
and a Claude line is the LAST rung. The order is cheapest-first for
the lane formats and tie-breaking for the last: a Claude transcript
carries no lane marker at all, so its rung can sit anywhere -- last is
where codexmeter had it, and it keeps a hypothetical line carrying BOTH
a lane marker and a sessionId routing to the lane parser, whose parse
is the stricter reading of the bytes. sniff_format returns "claude"
where codexmeter raised UnsupportedTranscriptError: claudit has a
Claude parser, so the detection's answer is data, not an error.
"""
from __future__ import annotations

from io import BytesIO
from typing import Callable, Literal

from orjson import JSONDecodeError, loads

from backend import parse_codex, parse_kimi
from backend.constants import DEFAULT_AGENT_TYPE

# The lane parsers parse_file dispatches to, keyed by sniff_format's
# labels. parse.py imports this mapping (and sniff_format) so that
# parse.parse_file and parse.sniff_format exist as its interface.
LANE_PARSERS: dict[str, Callable[[str, bytes], dict]] = {
    "codex": parse_codex.parse,
    "kimi-code": parse_kimi.parse_kimi_code,
    "legacy": parse_kimi.parse_legacy,
}

# Claude Code writes ~/.claude/projects/<slug>/<uuid>.jsonl, and every line it
# emits — conversation turns as well as the sidecar bookkeeping records it
# interleaves into the same file — carries the session id under `sessionId`.
# Neither Kimi format nor the Codex rollout format has any such field
# (verified against production objects in both buckets: zero occurrences), so
# its presence alone identifies the format, and it keeps identifying it as
# Claude Code adds line types.
#
# The two exceptions are file-history records, which carry no sessionId; their
# type names are distinctive enough to match on their own.
_CLAUDE_UNKEYED_TYPES = frozenset({
    "file-history-snapshot", "file-history-delta",
})


def _is_claude_line(obj: dict) -> bool:
    """Is this one line of a Claude Code transcript?"""
    if obj.get("type") in _CLAUDE_UNKEYED_TYPES:
        return True
    return isinstance(obj.get("sessionId"), str)


def sniff_format(blob: bytes) -> Literal["claude", "codex", "kimi-code", "legacy"]:
    """The transcript format a blob is written in.

    The lane rungs are codexmeter's detection verbatim. Two claudit
    differences, both forced by claudit's own corpus:

    - codexmeter RAISED for a Claude transcript; claudit HAS a Claude
      parser, so the detection's answer is data, and where codexmeter
      raised this names the format "claude".
    - codexmeter defaulted a file no line identifies to legacy, its
      bucket's residue. claudit's catch-all is the claude parser it has
      always run over every blob: the rungs exist to REROUTE the lane
      formats positively, and a file no rung identifies keeps the
      pre-dispatch behaviour -- the claude path. A genuinely legacy
      file is identified positively by its rung, a message.type line,
      with or without a timestamp: a legacy StatusUpdate may carry no
      timestamp, and no other format puts a message.type in that set.
    """
    for raw in (blob.splitlines() if b"\r" in blob else BytesIO(blob)):
        if not raw.strip():
            continue
        try:
            obj = loads(raw)
        except JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        # Codex first: its records also carry a "timestamp", so a later rung
        # must not claim one. The pairing of a Codex record type with a dict
        # payload is what identifies the format -- "type" alone collides with
        # nothing here, but a bare event_msg with no payload would be a
        # truncated line rather than evidence.
        if obj.get("type") in parse_codex.RECORD_TYPES and isinstance(
                obj.get("payload"), dict):
            return "codex"
        if obj.get("type") == "metadata":
            return "kimi-code" if "created_at" in obj else "legacy"
        if obj.get("type") in {
            "context.append_message",
            "context.append_loop_event",
            "usage.record",
            "turn.prompt",
            "turn.steer",
        }:
            return "kimi-code"
        # A legacy wire line carries message.type; claudit's rung does not
        # require the timestamp codexmeter's did, because a legacy
        # StatusUpdate may carry none and no other format puts a
        # message.type in this set (a Claude line has message.role); and
        # the isinstance guard matters, because kimi-code's llm.error
        # carries a STRING message.
        msg = obj.get("message")
        if isinstance(msg, dict) and msg.get("type") in {
            "StatusUpdate", "TurnBegin", "ToolCall", "ContentPart"
        }:
            return "legacy"
        # Last rung, codexmeter's order kept: a Claude line carries no
        # lane marker, so this rung's position only matters for a
        # hypothetical line carrying both, and the lane parser is the
        # stricter reading of such bytes.
        if _is_claude_line(obj):
            return "claude"
    # No rung identified a lane format, so this keeps the behaviour the
    # claude path has always had: it parses the blob.
    return "claude"


def to_claudit(parsed: dict, fmt: str | None = None) -> dict:
    """Project one lane parse onto claudit's records/tool_uses columns.

    The lane parsers keep codexmeter's row shapes. claudit's schema needs
    every column its ingest binds by name, so this fills the Claude-side
    columns the lane formats do not express with their honest NULL/zero,
    and renames the two fields the formats spell differently:

    - a Codex reasoning breakdown is claudit's ``thinking_tokens``
      (same quantity, SV-SUBSET-TOKENS: a subset of output_tokens,
      never added to it);
    - a lane ``tool_call_id`` is claudit's ``tool_use_id`` (ingest keys
      ``tool_uses.is_canonical`` on it).

    A lane tool row's ``model`` is dropped: the column lives on the
    record, and the tool_uses table has no model of its own.

    ``fmt`` namespaces one format's ids: a legacy ToolCall's payload.id is
    a per-session sequence ("tc1", "ReadFile:0" -- sampled over the kimi
    bucket, ids repeat across unrelated sessions), while Codex call_ids
    and kimi-code tool_<random> ids are globally unique. ingest groups
    tool_uses by tool_use_id ACROSS files (is_canonical), so a bare
    legacy id would collide with its namesakes in other sessions and
    wrongly mark real calls non-canonical. The file key is what makes it
    unique.
    """
    for r in parsed["records"]:
        r["thinking_tokens"] = r.pop("reasoning_output_tokens", 0)
        # "" is the schema's sentinel for "no request id" (the
        # records_request_idx partial index keys on request_id <> '').
        r["request_id"] = ""
        r["stop_reason"] = None
        r["effort"] = None
        r["cli_version"] = None
        r["turn_flags"] = []
        r["turn_tool_results"] = 0
        r["eph5_tokens"] = 0
        r["eph1h_tokens"] = 0
    for tu in parsed["tool_uses"]:
        tc_id = str(tu.pop("tool_call_id", "") or "")
        if fmt == "legacy" and tc_id:
            tu["tool_use_id"] = f'{tu["file_key"]}:{tc_id}'
        else:
            tu["tool_use_id"] = tc_id or None
        del tu["model"]
        tu["error_kind"] = None
        tu["error_text"] = None
        tu["agent_type"] = None
        tu["agent_model"] = None
        tu["dispatch_prompt_chars"] = None
        tu["dispatch_brief_ref"] = None
        tu["result_chars"] = None
        tu["read_kind"] = None
        tu["read_targets"] = None
        tu["write_targets"] = None
        tu["is_reread"] = None
    out = dict(parsed)
    out["prompt_count"] = parsed.get("prompt_count") or 0
    out["models"] = sorted({r["model"] for r in parsed["records"]})
    # files.agent_type is NOT NULL; a lane transcript records no role,
    # so it lands in the same unattributable bucket as a Claude one.
    out["agent_type"] = DEFAULT_AGENT_TYPE
    return out
