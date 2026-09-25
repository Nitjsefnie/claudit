"""Format sniffing and the lane-to-claudit row adapter (D2, D3).

claudit persists four transcript formats through one parse_file: Claude
Code transcripts (parsed in parse.py), Codex rollouts, and the two Kimi
wire formats (parsed by codexmeter's ported parsers, which keep
codexmeter's field names and row shapes). sniff_format names a blob's
format; to_claudit projects a lane parse onto claudit's records and
tool_uses columns.

Detection scans line by line and the first rung that matches wins.
What each rung matches:

- codex: a type in parse_codex.RECORD_TYPES whose payload is a dict
- kimi-code: a "metadata" line carrying created_at, or one of the
  context./usage./turn. event types listed below
- legacy: a "metadata" line without created_at, or a message.type in
  the four legacy names below (a timestamp is not required)
- claude: a file-history-snapshot/delta type, or any line carrying a
  sessionId; a file NO line identifies also parses as claude, which
  keeps the pre-dispatch behaviour. This is where codexmeter raised
  UnsupportedTranscriptError -- claudit has a Claude parser, so the
  detection's answer is data, not an error.

The order matters only for a line carrying markers of two formats at
once: the earlier rung claims it, and no format's marker set is known
to overlap another's, so on real files any order sniffs the same.
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
        # Codex rung: the pairing of a Codex record type with a dict
        # payload is what identifies the format -- "type" alone collides
        # with nothing here, but a bare event_msg with no payload would be
        # a truncated line rather than evidence.
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
        # Last rung: its position matters only for a hypothetical line
        # carrying both a Claude marker and a lane one -- the earlier
        # rung claims such a line.
        if _is_claude_line(obj):
            return "claude"
    # No rung identified a lane format, so this keeps the behaviour the
    # claude path has always had: it parses the blob.
    return "claude"


# Each lane's own name for "no role profile", which is claudit's
# DEFAULT_AGENT_TYPE rather than a role of its own. Every other role a
# transcript names is stored verbatim.
_LANE_DEFAULT_ROLES = {"codex": "default", "kimi-code": "agent"}


def lane_agent_type(fmt: str, role: str | None) -> str:
    """files.agent_type for a lane transcript that declared `role`.

    A transcript naming no role, or naming its lane's default profile,
    lands in DEFAULT_AGENT_TYPE -- the same unattributable bucket as a
    Claude transcript without one. Legacy kimi-cli records no role at
    all, so it always lands there.
    """
    if not role or role == _LANE_DEFAULT_ROLES.get(fmt):
        return DEFAULT_AGENT_TYPE
    return role


def lane_sidecar_agent_type(role: str) -> str:
    """files.agent_type for the role a lane-tree meta.json sidecar names.

    The lane tree's sidecars are Kimi's (subagent_type / launch_spec;
    Codex writes none), so the role takes Kimi's normalisation whatever
    the wire beside it sniffs as -- an empty legacy wire sniffs as the
    claude catch-all, and its sidecar is still a Kimi one.
    """
    return lane_agent_type("kimi-code", role)


def to_claudit(parsed: dict, fmt: str) -> dict:
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

    A lane tool row keeps its ``model`` (tool_uses.model): a lane tool
    call never shares a line with a record, so no reader can recover it
    by joining one.

    The dispatch columns (agent_type, agent_model, dispatch_prompt_chars,
    dispatch_brief_ref) keep whatever the lane parser read off a
    dispatching call, and are NULL on every other call. The file's raw
    ``agent_role`` becomes ``agent_type`` through lane_agent_type.

    ``fmt`` (the sniff_format label, required) namespaces one format's
    ids: a legacy ToolCall's payload.id is a per-session sequence
    ("tc1", "ReadFile:0" -- sampled over the kimi bucket, ids repeat
    across unrelated sessions), while Codex call_ids and kimi-code
    tool_<random> ids are globally unique. ingest groups tool_uses by
    tool_use_id ACROSS files (is_canonical), so a bare legacy id would
    collide with its namesakes in other sessions and wrongly mark real
    calls non-canonical. The file key is what makes it unique.
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
        # A settled failure already carries its (error_kind, error_text)
        # from parse_common._settle_lane_tool_result; default the rest to
        # NULL (SV-WHY-COLUMNS: a kind only on an errored call).
        tu.setdefault("error_kind", None)
        tu.setdefault("error_text", None)
        tu.setdefault("agent_type", None)
        tu.setdefault("agent_model", None)
        tu.setdefault("dispatch_prompt_chars", None)
        tu.setdefault("dispatch_brief_ref", None)
        tu["dispatch_name"] = None
        tu["result_chars"] = None
        tu["read_kind"] = None
        tu["read_targets"] = None
        tu["write_targets"] = None
        tu["is_reread"] = None
    out = dict(parsed)
    out["prompt_count"] = parsed.get("prompt_count") or 0
    out["models"] = sorted({r["model"] for r in parsed["records"]})
    # files.agent_type is NOT NULL; lane_agent_type never returns None.
    role = out.pop("agent_role", None)
    out["agent_type"] = lane_agent_type(fmt, role)
    # Whether the transcript itself named a role (its lane's default
    # profile included): a meta.json sidecar may only fill in for a
    # transcript that named none (agent_sidecar.apply_agent_sidecar).
    out["agent_type_in_band"] = role is not None
    return out
