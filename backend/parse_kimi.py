"""The two Kimi wire formats → per-file (records list + ctx_turns array).

Ported from codexmeter's parse.py (its model attribution, _edit_churn and
the legacy and kimi-code parsers). claudit's format DETECTION lives in
backend/parse_lanes.py and the machinery all lane formats share in
backend/parse_common.py; this module owns the two Kimi formats and the
Kimi model ladder only.

Cost is precomputed per-record using the pricing module's rate tables so the
read path doesn't need to JOIN against rates. Bumps to a rate table OR to the
parse algorithm both require a PARSER_VERSION bump to invalidate every files
row.
"""
from __future__ import annotations

import json
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timezone

from orjson import JSONDecodeError, loads

from backend.bash_churn import bash_churn
from backend.parse_common import (_append_tool_use, _append_usage_record,
                                  _arm_reply_anchor, _close_turn,
                                  _dispatch_prompt_shape,
                                  _end_turn, _finish_parse, _line_count,
                                  _mark_assistant_event, _nonempty_str,
                                  _note_agent_role, _ParseState,
                                  _settle_lane_tool_result, _start_turn,
                                  _to_dt, _turn_boundary)
from backend.tool_errors import _flatten_result_text

# Model attribution, oldest first. Each constant is a frozen UTC epoch, NOT a
# live expression.
#
# The wire is the primary source: a kimi-code usage.record carries the provider
# id (e.g. "kimi-code/k3") on the record that becomes a billing row, so it is
# authoritative when it names a model unambiguously.
#
# Dates are the fallback, needed for two real cases the wire cannot express:
#   - Legacy-format transcripts carry NO model string at all (and are still
#     being produced).
#   - "kimi-for-coding" spans BOTH k2.6 and k2.7-code — the branding did not
#     change at that transition — so only a date can separate those two.
#
# MODEL_CUTOFF applies to any record the wire did not settle; K3_CUTOFF applies
# ONLY to records with no wire model string at all. A model string that is
# present but unrecognized is not the "no model string" case, so it never
# reaches the k3 rung — see _model_for.
#
# Boundaries are strictly-before / inclusive-at, so each cutoff instant
# belongs to the NEWER model.

# 2026-06-11 22:30:35 UTC  k2-6 -> k2-7-code
MODEL_CUTOFF_EPOCH = 1781217035
# 2026-07-16 14:45:55 UTC  earliest observed k3 usage.record; only applies
# to records with no wire model string.
K3_CUTOFF_EPOCH = 1784213155
MODEL_CUTOFF_DT = datetime.fromtimestamp(MODEL_CUTOFF_EPOCH, tz=timezone.utc)
K3_CUTOFF_DT = datetime.fromtimestamp(K3_CUTOFF_EPOCH, tz=timezone.utc)

# Raw provider id -> canonical pricing label. Only ids that identify a pricing
# model on their own belong here; "kimi-for-coding" deliberately does not.
_WIRE_MODEL_MAP = {"k3": "kimi-k3"}


def _canonical_model(wire_model: str | None) -> str | None:
    """Raw provider id -> canonical pricing label, or None if the wire cannot
    settle it alone (ambiguous id, unrecognized id, or no id at all).

    Only canonical labels may reach pricing.compute_cost: pricing.rate_for
    matches keys anchored at the start of the id, so a raw "kimi-code/k3"
    would match no key and silently bill at the cheaper generic fallback.
    """
    if not wire_model:
        return None
    return _WIRE_MODEL_MAP.get(wire_model.rsplit("/", 1)[-1])


def _model_for(wire_model: str | None, ts: datetime | None) -> str:
    """Pricing model for ONE billing record.

    The wire wins when it names a model unambiguously; dates decide only what
    the wire cannot express. Resolved per-record, not per-session: a session
    can switch model mid-flight (observed: kimi-for-coding -> k3, 24s apart).

    Unknowns resolve conservatively, never to the newest label, because K3's
    rates are ~3x higher and a wrong undercount beats a wrong overcount:
      - No usable timestamp -> kimi-k2-7-code. An unstamped record is
        already-ingested history, so it cannot postdate the K3 cutoff.
      - A model string that is present but unrecognized -> the k2 rungs only.
        It is not the "transcript carries no model string" case that justifies
        the date ladder's k3 rung, so the date must not promote it to k3.
    """
    canonical = _canonical_model(wire_model)
    if canonical is not None:
        return canonical

    if ts is None:
        return "kimi-k2-7-code"

    if ts < MODEL_CUTOFF_DT:
        return "kimi-k2-6"

    # The wire named a model and it is not k3 (else _canonical_model caught
    # it), so the date may only separate the k2 generations — never promote
    # to k3.
    if wire_model:
        return "kimi-k2-7-code"

    if ts < K3_CUTOFF_DT:
        return "kimi-k2-7-code"
    return "kimi-k3"


def _edit_churn(tool_name: str, args: dict) -> tuple[int, int]:
    """(lines_added, lines_deleted) for ONE file-mutating tool call.

    The wire's tool RESULT carries no diff — an Edit result is a prose
    string like "Replaced 1 occurrence in <path>" — so the counts are
    derived from the call's ARGS. Verified against the R2 corpus
    (2026-07, ~300 files sampled): the file-mutating tools come in
    exactly these shapes, two per wire format:

      kimi-code  Edit  {path, old_string, new_string}   (163/163 calls)
                 Write {path, content, mode?}
      legacy     StrReplaceFile {path, edit: {old, new} | [{old, new}, ...]}
                 WriteFile      {path, content, mode?}

    A Write's deleted count is unknowable — the args carry only the new
    content, not what was overwritten — so Writes contribute added lines
    only (append mode and fresh files are the common case anyway). Every
    other tool returns (0, 0).

    The counts describe the ATTEMPT: a call whose result is an error
    changed nothing, and _resolve_tool_errors zeroes its churn once the
    result's is_error is known.
    """
    if tool_name == "Edit":
        return (
            _line_count(args.get("new_string")),
            _line_count(args.get("old_string")),
        )
    if tool_name in ("Write", "WriteFile"):
        return (_line_count(args.get("content")), 0)
    if tool_name in ("Bash", "Shell"):
        return bash_churn(str(args.get("command") or ""))
    if tool_name == "StrReplaceFile":
        edit = args.get("edit")
        edits = edit if isinstance(edit, list) else [edit]
        added = deleted = 0
        for e in edits:
            if isinstance(e, dict):
                added += _line_count(e.get("new"))
                deleted += _line_count(e.get("old"))
        return (added, deleted)
    return (0, 0)


def _args_to_dict(args) -> dict:
    """Tool-call arguments as a dict, whatever the wire shape: kimi-code
    carries them already-parsed, legacy as a JSON string. Anything
    unreadable is {} — the churn of an unparsable edit is 0, not a
    dropped tool call."""
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            parsed = json.loads(args) if args else {}
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


# The tool that dispatches a subagent, on both Kimi wires. Sampled over the
# kimi bucket: kimi-code and legacy both call it `Agent`; neither wire
# carries a `Task` dispatch. AgentSwarm fans out several agents in one call
# and has no single type to attribute, so it is deliberately absent.
_KIMI_DISPATCH_TOOLS = ("Agent",)


def _kimi_dispatch_args(name: str, args: dict) -> tuple | None:
    """(agent_type, agent_model, prompt_chars, brief_ref) an `Agent` call
    asked for, or None for any other tool.

    Same argument names as a Claude dispatch (`subagent_type`, `model`,
    `prompt`), and the prompt is plain text, so its shape is measured by
    the Claude path's own helper. A legacy Agent call names no
    subagent_type, so it carries a prompt shape and no type.
    """
    if name not in _KIMI_DISPATCH_TOOLS:
        return None
    chars, brief_ref = _dispatch_prompt_shape(args)
    return (_nonempty_str(args.get("subagent_type")),
            _nonempty_str(args.get("model")), chars, brief_ref)


# --------------------------------------------------------------------------
# Legacy kimi-cli format
# --------------------------------------------------------------------------


def _legacy_content_part(st: _ParseState, ts: datetime | None,
                         payload: dict) -> None:
    if payload.get("type", "") == "text":
        st.text_chars_since_turn += len(str(payload.get("text", "")))
    # Any ContentPart or ToolCall counts as an assistant event
    _mark_assistant_event(st, ts)


def _legacy_tool_call(st: _ParseState, line_num: int, ts: datetime | None,
                      payload: dict) -> None:
    func = payload.get("function", {})
    name = func.get("name", "")
    args = _args_to_dict(func.get("arguments"))
    _append_tool_use(st, line_num, ts, name, payload.get("id", ""),
                     _edit_churn(name, args),
                     dispatch=_kimi_dispatch_args(name, args))
    _mark_assistant_event(st, ts)


def _legacy_tool_result(st: _ParseState, payload: dict) -> None:
    return_value = payload.get("return_value", {})
    tc_id = payload.get("tool_call_id", "")
    is_err = isinstance(return_value, dict) and bool(
        return_value.get("is_error", False)
    )
    if tc_id:
        text = (str(return_value.get("output") or "")
                if isinstance(return_value, dict) else "")
        _settle_lane_tool_result(st, tc_id, is_err, text if is_err else "")


def _legacy_status_update(st: _ParseState, line_num: int,
                          ts: datetime | None, payload: dict) -> None:
    tu = payload.get("token_usage")
    if not tu:
        return
    toks = (
        int(tu.get("input_other", 0) or 0),
        int(tu.get("input_cache_creation", 0) or 0),
        int(tu.get("input_cache_read", 0) or 0),
        int(tu.get("output", 0) or 0),
    )
    # Legacy transcripts carry no model string anywhere — verified across
    # the corpus: StatusUpdate payloads expose only context_usage /
    # context_tokens / max_context_tokens / token_usage / message_id /
    # plan_mode / mcp_status. Dates are all we have here. Use the RECORD's
    # own ts so a session spanning a cutoff splits correctly;
    # first_event_ts is only the fallback for a record with no timestamp.
    _append_usage_record(
        st, line_num, ts, payload.get("message_id") or None,
        _model_for(None, ts or st.first_event_ts), toks,
    )


def _legacy_dispatch(st: _ParseState, msg_type: str, line_num: int,
                     ts: datetime | None, payload: dict) -> None:
    if msg_type == "TurnBegin":
        _turn_boundary(st, line_num, ts)
    elif msg_type == "TurnEnd":
        _end_turn(st, line_num, ts)
    elif msg_type == "ContentPart":
        _legacy_content_part(st, ts, payload)
    elif msg_type == "ToolCall":
        _legacy_tool_call(st, line_num, ts, payload)
    elif msg_type == "ToolResult":
        _legacy_tool_result(st, payload)
    elif msg_type == "StatusUpdate":
        _legacy_status_update(st, line_num, ts, payload)


def parse_legacy(file_key: str, blob: bytes) -> dict:
    """Parse one legacy kimi-cli wire.jsonl.

    records: list of dicts with keys
      file_key, line_num, uuid, ts, model,
      fresh_tokens, cache_creation_tokens, cache_read_tokens,
      output_tokens, text_chars, reply_latency_s, cost_usd

    ctx_turns: list of dicts with keys
      idx, ts, line, input, output, delta
    """
    st = _ParseState(file_key)
    for line_num, raw in enumerate(blob.splitlines(), 1):
        if not raw:
            continue
        try:
            obj = loads(raw)
        except JSONDecodeError:
            continue

        ts_dt = _to_dt(obj.get("timestamp"))
        if ts_dt is not None and st.first_event_ts is None:
            st.first_event_ts = ts_dt

        msg = obj.get("message") or {}
        _legacy_dispatch(
            st, msg.get("type", ""), line_num, ts_dt, msg.get("payload", {})
        )

    _attribute_tool_models(st)
    return _finish_parse(st)


def _attribute_tool_models(st: _ParseState) -> None:
    """Give every tool call the model of the usage record billing its step.

    Both Kimi wires write a step's tool calls BEFORE the usage record that
    closes the step (every step of the 24 kimi-code and legacy files
    sampled from the kimi bucket reads calls-then-usage), and the model is
    resolved per record by _model_for. So a call's model is that of the
    first record after its line; a call after the last record (the file
    ends inside a step) takes the last record's, and a file with no record
    resolves the way a record naming no model would.
    """
    record_lines = [r["line_num"] for r in st.records]
    for tu in st.tool_uses:
        at = bisect_right(record_lines, tu["line_num"])
        if at < len(st.records):
            tu["model"] = st.records[at]["model"]
        elif st.records:
            tu["model"] = st.records[-1]["model"]
        else:
            tu["model"] = _model_for(None, tu["ts"] or st.first_event_ts)


# --------------------------------------------------------------------------
# kimi-code format
# --------------------------------------------------------------------------


@dataclass
class _KimiCodeState(_ParseState):
    """_ParseState plus the state only the kimi-code format needs."""
    # The `input` of every turn.steer not yet delivered into context, oldest
    # first. A steer that arrives while a step is in flight waits for that
    # step to finish, so its reply-latency anchor arms at delivery.
    pending_steers: list[list] = field(default_factory=list)


def _kc_parse_tool_call(tc: dict) -> tuple[str, str | None, str]:
    """Extract name, arguments, id from a kimi-code ToolCall (v1.0/v1.1)."""
    if tc.get("type") != "function":
        return "", None, ""
    tcid = tc.get("id", "")
    if "name" in tc:
        return str(tc.get("name", "")), tc.get("arguments"), tcid
    func = tc.get("function") or {}
    return str(func.get("name", "")), func.get("arguments"), tcid


def _kc_args_to_input(args) -> dict:
    if args is None:
        return {}
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            return json.loads(args) if args else {}
        except json.JSONDecodeError:
            return {"_raw": args}
    return {"_raw": args}


def _kc_metadata(st: _ParseState, obj: dict) -> None:
    ts_ms = obj.get("created_at")
    if ts_ms and st.first_event_ts is None:
        st.first_event_ts = datetime.fromtimestamp(
            ts_ms / 1000.0, tz=timezone.utc
        )


def _kc_event_ts(st: _ParseState, obj: dict) -> datetime | None:
    ts_ms = obj.get("time")
    ts_dt = (
        datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
        if ts_ms else None
    )
    if ts_dt is not None and st.first_event_ts is None:
        st.first_event_ts = ts_dt
    return ts_dt


def _count_content_text(content: list[dict]) -> int:
    chars = 0
    for p in content:
        if not isinstance(p, dict):
            continue
        if p.get("type") == "text":
            chars += len(str(p.get("text", "")))
    return chars


def _kc_append_message(st: _KimiCodeState, line_num: int,
                       ts: datetime | None, obj: dict) -> None:
    msg = obj.get("message") or {}
    role = msg.get("role")
    if role == "assistant":
        st.text_chars_since_turn += _count_content_text(
            msg.get("content") or []
        )
        for tc in msg.get("toolCalls") or []:
            name, raw_args, tcid = _kc_parse_tool_call(tc)
            args = _args_to_dict(raw_args)
            _append_tool_use(st, line_num, ts, name, tcid,
                             _edit_churn(name, args),
                             dispatch=_kimi_dispatch_args(name, args))
        _mark_assistant_event(st, ts)
    elif role == "tool":
        tcid = msg.get("toolCallId", "")
        if tcid:
            is_err = bool(msg.get("isError"))
            text = (_flatten_result_text(msg.get("content"))
                    if is_err else "")
            _settle_lane_tool_result(st, tcid, is_err, text)
    elif role == "user":
        _kc_deliver_steer(st, ts, msg.get("content"))
    # role == system: no parser-side action


def _kc_steer(st: _KimiCodeState, line_num: int, ts: datetime | None,
              obj: dict) -> None:
    """Queue a steer until it is delivered into context.

    It stays a turn boundary for the turn bookkeeping, as turn.prompt is,
    but it does NOT arm the reply-latency anchor on arrival: a steer that
    arrives mid-step reaches the model only after that step ends -- which
    can be minutes later, when the step is waiting on the user -- and the
    step's billing record is the EARLIER request's, not the steer's reply.
    """
    _close_turn(st, line_num, ts)
    _start_turn(st, line_num, ts)
    steer_input = obj.get("input")
    if isinstance(steer_input, list) and steer_input:
        st.pending_steers.append(steer_input)


def _kc_deliver_steer(st: _KimiCodeState, ts: datetime | None,
                      content: object) -> None:
    """Arm the reply-latency anchor if this user message delivers a steer.

    kimi-code delivers queued steers first-in first-out, each as a user
    message whose content IS the steer's input (547 of 547 steers across
    125 corpus files). Any other user message -- a prompt's own, or a Skill
    tool's injected instructions landing mid-step -- is not a delivery, and
    arming there would let the in-flight step's record consume the anchor.
    Every delivery re-arms, so a batch is timed from its last message, when
    the next request can start.
    """
    for i, pending in enumerate(st.pending_steers):
        if content == pending:
            del st.pending_steers[:i + 1]
            _arm_reply_anchor(st, ts)
            return


def _kc_step_begin(st: _ParseState, line_num: int, ts: datetime | None,
                   turn_id: str | None) -> None:
    if turn_id != st.current_turn_id:
        _close_turn(st, line_num, ts)
        st.current_turn_id = turn_id
        _start_turn(st, line_num, ts)


def _kc_loop_event(st: _ParseState, line_num: int, ts: datetime | None,
                   obj: dict) -> None:
    ev = obj.get("event") or {}
    et = ev.get("type")
    if et == "step.begin":
        _kc_step_begin(st, line_num, ts, ev.get("turnId"))
    elif et == "content.part":
        part = ev.get("part") or {}
        if part.get("type") == "text":
            st.text_chars_since_turn += len(str(part.get("text", "")))
        _mark_assistant_event(st, ts)
    elif et == "tool.call":
        name = str(ev.get("name", ""))
        args = _args_to_dict(ev.get("args"))
        _append_tool_use(
            st, line_num, ts, name, str(ev.get("toolCallId", "")),
            _edit_churn(name, args), dispatch=_kimi_dispatch_args(name, args),
        )
        _mark_assistant_event(st, ts)
    elif et == "tool.result":
        res = ev.get("result") or {}
        tcid = ev.get("toolCallId", "")
        if tcid:
            is_err = bool(res.get("isError") if isinstance(res, dict)
                          else False)
            text = (_flatten_result_text(res.get("output"))
                    if is_err else "")
            _settle_lane_tool_result(st, tcid, is_err, text)
    # et == "step.end": informational; the turn stays open until the next
    # turn boundary (new turnId or turn.prompt).


def _kc_usage_record(st: _ParseState, line_num: int, ts: datetime | None,
                     obj: dict) -> None:
    usage = obj.get("usage") or {}
    toks = (
        int(usage.get("inputOther") or 0),
        int(usage.get("inputCacheCreation") or 0),
        int(usage.get("inputCacheRead") or 0),
        int(usage.get("output") or 0),
    )
    # Every usage.record names its model (verified: 7,397/7,397 in the
    # corpus, values "kimi-code/k3" and "kimi-code/kimi-for-coding").
    # Honour it; dates decide only what the wire cannot express.
    _append_usage_record(
        st, line_num, ts, f"{st.file_key}:{line_num}",
        _model_for(obj.get("model"), ts or st.first_event_ts), toks,
    )


def _kc_llm_error(st: _ParseState, line_num: int, ts: datetime | None,
                  obj: dict) -> None:
    """Book a failed provider request as a rate-limit hit, if it is one.

    kimi-code journals every non-aborted provider failure as an `llm.error`
    op (Nitjsefnie-OSC/kimi-code@e7bb820) and carries its own classification
    in `kind`, so a hard quota stop is told apart from an ordinary
    per-minute 429 by a field rather than by matching message text — the
    signal claudit has to recover from "out of extra usage" in the body.

    Only `quota_exhausted` is recorded. `rate_limit` is the provider
    shaping traffic; it clears itself and is not the wall the user hits.
    claudit excludes the same case deliberately, so counting it here would
    make the two dashboards' stats mean different things.

    Content is re-capped at 500 even though the producer already truncates
    there: the cap belongs to whatever writes the column, not to a
    journal written by a different program.
    """
    if obj.get("kind") != "quota_exhausted":
        return
    st.rate_limit_hits.append({
        "line": line_num,
        "ts": ts.isoformat() if ts is not None else "",
        "content": str(obj.get("message") or "")[:500],
    })


def _kc_dispatch(st: _KimiCodeState, typ: str, line_num: int,
                 ts: datetime | None, obj: dict) -> None:
    # Turn boundaries. A prompt reaches the model at once; a steer may wait
    # behind an in-flight step (see _kc_steer).
    if typ == "turn.prompt":
        _turn_boundary(st, line_num, ts)
    elif typ == "turn.steer":
        _kc_steer(st, line_num, ts, obj)
    elif typ == "context.append_message":
        _kc_append_message(st, line_num, ts, obj)
    elif typ == "context.append_loop_event":
        _kc_loop_event(st, line_num, ts, obj)
    elif typ == "usage.record":
        _kc_usage_record(st, line_num, ts, obj)
    elif typ == "llm.error":
        _kc_llm_error(st, line_num, ts, obj)
    elif typ == "config.update":
        # The role profile this session ran under; the first non-empty
        # profileName wins. The default profile is named `agent`, which
        # parse_lanes.to_claudit normalises.
        _note_agent_role(st, obj.get("profileName"))


def parse_kimi_code(file_key: str, blob: bytes) -> dict:
    """Parse one kimi-code wire.jsonl.

    Returns the same shape as _parse_legacy.
    """
    st = _KimiCodeState(file_key)
    for line_num, raw in enumerate(blob.splitlines(), 1):
        if not raw:
            continue
        try:
            obj = loads(raw)
        except JSONDecodeError:
            continue

        typ = obj.get("type")
        if typ == "metadata":
            _kc_metadata(st, obj)
            continue

        ts_dt = _kc_event_ts(st, obj)
        _kc_dispatch(st, typ, line_num, ts_dt, obj)

    _attribute_tool_models(st)
    return _finish_parse(st, len(blob.splitlines()))
