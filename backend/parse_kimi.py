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

from bisect import bisect_right
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from orjson import JSONDecodeError, loads

from backend.flags import ALL_COLLECTIONS_ON, CollectionPolicy
from backend.kimi_tool_rows import (  # pylint: disable=unused-import
    _args_to_dict, _edit_churn, _kimi_dispatch_args, _legacy_tool_call, _legacy_tool_result)
from backend.parse_common import (_append_tool_use, _append_usage_record,
                                  _arm_reply_anchor, _close_turn,
                                  _end_turn, _finish_parse,
                                  _mark_assistant_event,
                                  _note_agent_role,
                                  _settle_lane_tool_result, _start_turn,
                                  _to_dt, _turn_boundary)
from backend.kimi_content import (_count_content_text, _kc_parse_tool_call)
from backend.tool_errors import _flatten_result_text
from backend.json_shape import as_dict, as_list, dict_list

if TYPE_CHECKING:
    from backend.lane_state import _KimiCodeState, _ParseState


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


# --------------------------------------------------------------------------
# Legacy kimi-cli format
# --------------------------------------------------------------------------


def _legacy_content_part(st: _ParseState, ts: datetime | None,
                         payload: dict) -> None:
    if payload.get("type", "") == "text":
        st.text_chars_since_turn += len(str(payload.get("text", "")))
    # Any ContentPart or ToolCall counts as an assistant event
    _mark_assistant_event(st, ts)


def _legacy_status_update(st: _ParseState, line_num: int,
                          ts: datetime | None, payload: dict) -> None:
    tu = payload.get("token_usage")
    if not isinstance(tu, dict) or not tu:
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
        generation_s=(st.lane_generation.bind() if st.lane_generation is not None else None),
    )


def _legacy_dispatch(st: _ParseState, msg_type: str, line_num: int,
                     ts: datetime | None, payload: dict) -> None:
    clock = st.lane_generation
    if msg_type == "TurnBegin":
        if clock is not None:
            clock.anchor_line(ts)
        _turn_boundary(st, line_num, ts)
    elif msg_type == "TurnEnd":
        _end_turn(st, line_num, ts)
    elif msg_type == "ContentPart":
        if clock is not None:
            clock.response_line(ts)
        _legacy_content_part(st, ts, payload)
    elif msg_type == "ToolCall":
        if clock is not None:
            clock.response_line(ts)
        _legacy_tool_call(st, line_num, ts, payload)
    elif msg_type == "ToolResult":
        if clock is not None:
            clock.anchor_line(ts)
        _legacy_tool_result(st, payload)
    elif msg_type == "StatusUpdate":
        _legacy_status_update(st, line_num, ts, payload)
    elif msg_type == "ToolCallPart" and clock is not None:
        clock.response_line(ts)


def parse_legacy(file_key: str, blob: bytes, *, collection: CollectionPolicy = ALL_COLLECTIONS_ON) -> dict:
    """Parse one legacy kimi-cli wire.jsonl.

    records: list of dicts with keys
      file_key, line_num, uuid, ts, model,
      fresh_tokens, cache_creation_tokens, cache_read_tokens,
      output_tokens, text_chars, reply_latency_s, cost_usd

    ctx_turns: list of dicts with keys
      idx, ts, line, input, output, delta
    """
    from backend.lane_state import _ParseState  # pylint: disable=import-outside-toplevel
    st = _ParseState(file_key, collection=collection)
    for line_num, raw in enumerate(blob.splitlines(), 1):
        if not raw:
            continue
        try:
            obj = loads(raw)
        except JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue

        ts_dt = _to_dt(obj.get("timestamp"))
        if ts_dt is not None and st.first_event_ts is None:
            st.first_event_ts = ts_dt

        msg = as_dict(obj.get("message"))
        payload = as_dict(msg.get("payload"))
        _legacy_dispatch(
            st, msg.get("type", ""), line_num, ts_dt, payload
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


def _kc_append_message(st: _KimiCodeState, line_num: int,
                       ts: datetime | None, obj: dict) -> None:
    msg = as_dict(obj.get("message"))
    role = msg.get("role")
    clock = st.lane_generation
    if role == "assistant":
        if clock is not None:
            clock.response_line(ts)
        content = as_list(msg.get("content") or [])
        st.text_chars_since_turn += _count_content_text(
            content
        )
        for tc in dict_list(msg.get("toolCalls") or []):
            name, raw_args, tcid = _kc_parse_tool_call(tc)
            args = _args_to_dict(raw_args)
            _append_tool_use(st, line_num, ts, name, tcid,
                             _edit_churn(name, args), model=None,
                             dispatch=_kimi_dispatch_args(name, args, st.collect))
        _mark_assistant_event(st, ts)
    elif role == "tool":
        if clock is not None:
            clock.anchor_line(ts)
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
            if st.lane_generation is not None:
                st.lane_generation.anchor_line(ts)
            return


def _kc_step_begin(st: _ParseState, line_num: int, ts: datetime | None,
                   turn_id: str | None) -> None:
    if turn_id != st.current_turn_id:
        _close_turn(st, line_num, ts)
        st.current_turn_id = turn_id
        _start_turn(st, line_num, ts)


def _kc_loop_event(st: _ParseState, line_num: int, ts: datetime | None,
                   obj: dict) -> None:
    ev = as_dict(obj.get("event"))
    et = ev.get("type")
    clock = st.lane_generation
    if et == "step.begin":
        _kc_step_begin(st, line_num, ts, ev.get("turnId"))
    elif et == "content.part":
        if clock is not None:
            clock.response_line(ts)
        part = ev.get("part")
        if isinstance(part, dict) and part.get("type") == "text":
            st.text_chars_since_turn += len(str(part.get("text", "")))
        _mark_assistant_event(st, ts)
    elif et == "tool.call":
        if clock is not None:
            clock.response_line(ts)
        name = str(ev.get("name", ""))
        args = _args_to_dict(ev.get("args"))
        _append_tool_use(
            st, line_num, ts, name, str(ev.get("toolCallId", "")),
            _edit_churn(name, args), model=None, dispatch=_kimi_dispatch_args(name, args, st.collect),
        )
        _mark_assistant_event(st, ts)
    elif et == "tool.result":
        if clock is not None:
            clock.anchor_line(ts)
        res = as_dict(ev.get("result"))
        tcid = ev.get("toolCallId", "")
        if tcid:
            is_err = bool(res.get("isError"))
            text = (_flatten_result_text(res.get("output"))
                    if is_err else "")
            _settle_lane_tool_result(st, tcid, is_err, text)
    # et == "step.end": informational; the turn stays open until the next
    # turn boundary (new turnId or turn.prompt).


def _kc_usage_record(st: _ParseState, line_num: int, ts: datetime | None,
                     obj: dict) -> None:
    usage = as_dict(obj.get("usage"))
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
        generation_s=(st.lane_generation.bind() if st.lane_generation is not None else None),
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
    if st.lane_generation is not None:
        st.lane_generation.fail()
    if obj.get("kind") != "quota_exhausted":
        return
    st.rate_limit_hits.append({
        "line": line_num,
        "ts": ts.isoformat() if ts is not None else "",
        "content": str(obj.get("message") or "")[:500],
    })


def _kc_dispatch(st: _KimiCodeState, typ: object, line_num: int,
                 ts: datetime | None, obj: dict) -> None:
    # Turn boundaries. A prompt reaches the model at once; a steer may wait
    # behind an in-flight step (see _kc_steer).
    if typ == "turn.prompt":
        if st.lane_generation is not None:
            st.lane_generation.anchor_line(ts)
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


def parse_kimi_code(file_key: str, blob: bytes, *, collection: CollectionPolicy = ALL_COLLECTIONS_ON) -> dict:
    """Parse one kimi-code wire.jsonl.

    Returns the same shape as _parse_legacy.
    """
    from backend.lane_state import _KimiCodeState  # pylint: disable=import-outside-toplevel
    st = _KimiCodeState(file_key, collection=collection)
    for line_num, raw in enumerate(blob.splitlines(), 1):
        if not raw:
            continue
        try:
            obj = loads(raw)
        except JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue

        typ = obj.get("type")
        if typ == "metadata":
            _kc_metadata(st, obj)
            continue

        ts_dt = _kc_event_ts(st, obj)
        _kc_dispatch(st, typ, line_num, ts_dt, obj)

    _attribute_tool_models(st)
    return _finish_parse(st, len(blob.splitlines()))
