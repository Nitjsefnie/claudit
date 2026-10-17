"""Parse Codex rollout JSONL into the shared per-file records/turns shape.

parse_file detects the lane. Read flat events and item_completed envelopes;
FileChange/AgentMessage carry churn/text, while CommandExecution duplicates
response_item tool calls and must not count twice.

Difference cumulative token snapshots. Recover the first baseline as
total - last so fork-inherited usage is excluded; skip non-advancing
duplicates. Cached input and cache writes are subsets of input, not addends.
Token events carry no model: use turn_context/thread_settings_applied and
attribute replayed prefixes from the first declaration. Cross-file request
identity must survive replay; ingest handles canonical dedup (SV-PARSER-SPEC)."""
from __future__ import annotations

from datetime import datetime
from operator import sub
from typing import TYPE_CHECKING

from orjson import JSONDecodeError, loads

from backend import pricing
from backend.codex_fork import head_scan, mark_replay
from backend.parse_common import (_append_usage_record,
                                  _end_turn, _finish_parse,
                                  _mark_assistant_event, _nonempty_str,
                                  _note_agent_role,
                                  _to_dt,
                                  _turn_boundary)
from backend.json_shape import as_dict, as_list
from backend.flags import ALL_COLLECTIONS_ON, CollectionPolicy
from backend.codex_tool_rows import (  # pylint: disable=unused-import
    _codex_model, _codex_output_text, _diff_churn, _change_churn,
    _codex_dispatch_args, _codex_tool_call, _codex_tool_result, _codex_patch)

if TYPE_CHECKING:
    from backend.lane_state import _CodexState


# The cumulative counter's fields. All six are differenced so a duplicate
# snapshot is recognised by ALL of them failing to advance, not just by the
# four that are billed.
_CODEX_USAGE_KEYS = (
    "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
    "output_tokens", "reasoning_output_tokens", "total_tokens",
)
_CODEX_ZERO_USAGE = dict.fromkeys(_CODEX_USAGE_KEYS, 0)


# Every top-level record type the format emits; the format's fingerprint.
RECORD_TYPES = frozenset({
    "session_meta", "turn_context", "event_msg", "response_item",
    "world_state", "compacted", "inter_agent_communication_metadata",
})


def _codex_first_declared_model(blob: bytes) -> str | None:
    """The first model this file declares, in file order.

    The first declaration is the model the parent had in force at the
    fork point - the inherited settings - so it attributes a fork's
    replayed prefix. The scan, and the fork/replay facts
    read beside it, live in backend/codex_fork.
    """
    return head_scan(blob)[0]


def _codex_record_uuid(st: _CodexState, cumulative: dict,
                       line_num: int) -> str:
    """Request identity shared by every file replaying the same thread history.

    Use thread id and cumulative-counter position: replay reproduces both.
    A file_key/line identity cannot dedup resumed or forked rollouts, and
    summing their per-file records counts the same requests repeatedly.
    Ingest resolves this uuid to one is_canonical row.

    Without session_meta, a fragment has no shared thread identity and
    falls back to its per-file key; it cannot dedup across files."""
    if not st.session_id:
        return f"{st.file_key}:{line_num}"
    return f"{st.session_id}:{cumulative['total_tokens']}"


def _codex_token_count(st: _CodexState, line_num: int, ts: datetime | None,
                       payload: dict) -> None:
    """Book ONE billing record per real request (traps 1-4)."""
    info = as_dict(payload.get("info"))
    total = as_dict(info.get("total_token_usage"))
    if not total:
        return
    cumulative = {k: int(total.get(k) or 0) for k in _CODEX_USAGE_KEYS}
    if st.prev_usage is None:
        # Trap 1. This file's inherited baseline is its first cumulative
        # snapshot minus the request that produced it, so the first delta is
        # that request alone and the parent's millions never enter a sum.
        last = as_dict(info.get("last_token_usage"))
        st.prev_usage = (_CODEX_ZERO_USAGE if total == last else
                         {k: cumulative[k] - int(last.get(k) or 0)
                          for k in _CODEX_USAGE_KEYS})
    delta = dict(zip(_CODEX_USAGE_KEYS,
                     map(sub, cumulative.values(), st.prev_usage.values())))
    st.prev_usage = cumulative
    advancing = max(delta.values()) > 0
    if not advancing and not st.web_search_requests:
        return  # Trap 2: duplicate token_count, or a non-advancing snapshot.

    # Cached and cache-write input are subsets of input_tokens. Keep
    # cache writes in their own billed bucket: folding into fresh underbills
    # writes, while folding into cached uses the wrong rate. The field is
    # billable even when a transcript reports zero writes.
    total_in = max(0, delta["input_tokens"])
    read = max(0, delta["cached_input_tokens"])
    create = max(0, delta["cache_write_input_tokens"])
    fresh = max(0, total_in - read - create)
    # Subset of output, not an addend — carried, never added to cost.
    reasoning = max(0, delta["reasoning_output_tokens"])
    # Trap 4: the payload names no model; the surrounding turn_context does.

    # The long-context meter is a property of the model's rate card, not of
    # the plan that served the request — every record is billed as if it
    # were an API call, so a subscription rollout above the
    # threshold bills the meter exactly as an API-key one does.
    _append_usage_record(
        st, line_num, ts,
        _codex_record_uuid(st, cumulative, line_num),
        _codex_model(st.model),
        (fresh, create, read, max(0, delta["output_tokens"])),
        reasoning=reasoning,
        long_context=(total_in > pricing.long_context_threshold(st.model)),
        web_search_requests=st.web_search_requests or None,
        generation_s=(st.lane_generation.bind()
                      if advancing and st.lane_generation is not None else None),
    )
    st.web_search_requests = 0


def _codex_rate_limit(st: _CodexState, line_num: int, ts: datetime | None,
                      payload: dict) -> None:
    """Book any non-null reported rate-limit condition, collapsing repeats.

    The field values are not constrained to a known hit vocabulary, so
    retain str(value) for display rather than guessing its encoding."""
    rl = as_dict(payload.get("rate_limits"))
    kind = rl.get("rate_limit_reached_type")
    if not kind and not rl.get("spend_control_reached"):
        st.last_rl_kind = None
        return
    key = str(kind) if kind else "spend_control_reached"
    if key == st.last_rl_kind:
        return
    st.last_rl_kind = key
    primary = as_dict(rl.get("primary"))
    st.rate_limit_hits.append({
        "line": line_num,
        "ts": ts.isoformat() if ts is not None else "",
        "content": (
            f"{key} (plan={rl.get('plan_type')}, "
            f"primary {primary.get('used_percent')}% of "
            f"{primary.get('window_minutes')}m window)"
        )[:500],
    })


def _codex_session_role(payload: dict) -> str | None:
    """The agent role one session_meta names, if any.

    A subagent's rollout carries it at `agent_role`, mirrored under
    `source.subagent.thread_spawn`; a main rollout carries neither.
    Anything but a non-empty string, at either place, is no role.
    """
    role = _nonempty_str(payload.get("agent_role"))
    if role:
        return role
    source = payload.get("source")
    subagent = source.get("subagent") if isinstance(source, dict) else None
    spawn = (subagent.get("thread_spawn")
             if isinstance(subagent, dict) else None)
    return (_nonempty_str(spawn.get("agent_role"))
            if isinstance(spawn, dict) else None)


def _codex_item_completed(st: _CodexState, line_num: int,
                          ts: datetime | None, payload: dict) -> None:
    """Route item_completed by item.type alongside flat event spellings.

    Flat and enveloped spellings are disjoint per file, so reading both
    does not duplicate FileChange churn or AgentMessage text.
    Read FileChange/AgentMessage quantities and Reasoning output, which
    can end reply latency without counting usage. CommandExecution repeats
    a response_item tool call, so counting it doubles tool rows.
    UserMessage, SubAgentActivity, ContextCompaction, Extension, ImageView
    and CollabAgentToolCall have no flat-event counterpart and count
    nothing in either spelling."""
    item = as_dict(payload.get("item"))
    itype = item.get("type")
    clock = st.lane_generation
    if clock is not None and itype in ("AgentMessage", "Reasoning"):
        clock.response_line(ts)
    if itype == "FileChange":
        # FileChange status expresses success as completed, not a boolean.
        _codex_patch(st, line_num, ts, item.get("status") == "completed",
                     item.get("changes"), payload.get("turn_id"))
    elif itype == "AgentMessage":
        st.text_chars_since_turn += len(_codex_item_text(item))
        _mark_assistant_event(st, ts)
    elif itype == "Reasoning":
        _mark_assistant_event(st, ts)


def _codex_item_text(item: dict) -> str:
    """Flatten an item's `content` list into one string.

    Unlike a response_item's content chunks, whose discriminator is the
    lowercase "text", an item's are typed "Text" — matching on the
    discriminator rather than reading the field would silently return "".
    """
    parts = []
    content = as_list(item.get("content") or [])
    for chunk in content:
        if isinstance(chunk, dict) and chunk.get("text"):
            parts.append(str(chunk["text"]))
    return "\n".join(parts)


def _codex_event_msg(st: _CodexState, ptype: str, line_num: int,
                     ts: datetime | None, payload: dict) -> None:
    if ptype == "token_count":
        _codex_rate_limit(st, line_num, ts, payload)
        _codex_token_count(st, line_num, ts, payload)
    elif ptype == "task_started":
        _turn_boundary(st, line_num, ts)
    elif ptype in ("task_complete", "turn_aborted"):
        _end_turn(st, line_num, ts)
    elif ptype == "agent_message":
        if st.lane_generation is not None:
            st.lane_generation.response_line(ts)
        # event_msg/agent_message includes response_item/message assistant
        # text. Count only this side to avoid duplicate output.
        st.text_chars_since_turn += len(str(payload.get("message") or ""))
        _mark_assistant_event(st, ts)
    elif ptype == "agent_reasoning":
        if st.lane_generation is not None:
            st.lane_generation.response_line(ts)
        # The flat event_msg spelling of reasoning, as older rollouts write
        # it: model output, so it can be where the reply began.
        _mark_assistant_event(st, ts)
    elif ptype == "patch_apply_end":
        _codex_patch(st, line_num, ts, bool(payload.get("success")),
                     payload.get("changes"), payload.get("turn_id"))
    elif ptype == "item_completed":
        _codex_item_completed(st, line_num, ts, payload)
    elif ptype == "thread_settings_applied":
        settings = as_dict(payload.get("thread_settings"))
        model = settings.get("model")
        if model:
            st.model = str(model)


def _codex_response_item(st: _CodexState, ptype: str, line_num: int,
                         ts: datetime | None, payload: dict) -> None:
    clock = st.lane_generation
    if ptype in ("custom_tool_call", "function_call"):
        if clock is not None:
            clock.response_line(ts)
        _codex_tool_call(st, line_num, ts, payload)
        _mark_assistant_event(st, ts)
    elif ptype == "web_search_call":
        if clock is not None:
            clock.response_line(ts)
        st.web_search_requests += 1
        _mark_assistant_event(st, ts)
    elif ptype in ("reasoning", "local_shell_call"):
        if clock is not None:
            clock.response_line(ts)
        # Model output that is not a tool_uses row here: it can still be
        # where the reply began.
        _mark_assistant_event(st, ts)
    elif ptype in ("custom_tool_call_output", "function_call_output"):
        if clock is not None:
            clock.anchor_line(ts)
        _codex_tool_result(st, payload)
    elif ptype == "message" and clock is not None:
        role = payload.get("role")
        if role == "user":
            clock.anchor_line(ts)
        elif role == "assistant":
            clock.response_line(ts)
    elif ptype == "agent_message" and clock is not None:
        clock.response_line(ts)


def _codex_dispatch(st: _CodexState, rtype: str, line_num: int,
                    ts: datetime | None, payload: dict) -> None:
    if rtype == "event_msg":
        _codex_event_msg(st, str(payload.get("type") or ""),
                         line_num, ts, payload)
    elif rtype == "response_item":
        _codex_response_item(st, str(payload.get("type") or ""),
                             line_num, ts, payload)
    elif rtype == "turn_context":
        model = payload.get("model")
        if model:
            st.model = str(model)
    elif rtype == "session_meta":
        # First one wins: a rollout declares its own thread once, at the
        # head. Anything later belongs to a replayed parent.
        if st.session_id is None and payload.get("session_id"):
            st.session_id = str(payload["session_id"])
        # Take the first non-empty role only from this thread's session_meta
        # (same id as its first). Forks replay parent session_meta too; that
        # parent role must not become the fork's role.
        thread_id = _nonempty_str(payload.get("id"))
        if not st.thread_seen:
            st.thread_seen = True
            st.thread_id = thread_id
        if thread_id == st.thread_id:
            _note_agent_role(st, _codex_session_role(payload))
    # world_state / compacted / inter_agent_communication_metadata: no
    # billing or tool consequence.
    #
    # mcp_tool_call_end and web_search_end are deliberately NOT tool_uses
    # rows. They share no call_id with the tool calls (0 of 324 and 0 of 11),
    # but every file that makes MCP calls through exec shows the two counts
    # matching exactly (11/11, 4/4) — they are the server side of the same
    # call, and a row each would count it twice. A response_item/web_search_call
    # is counted separately in web_search_requests; web_search_end never adds
    # another search count.


def parse(file_key: str, blob: bytes, *, collection: CollectionPolicy = ALL_COLLECTIONS_ON) -> dict:
    """Parse one Codex rollout JSONL. Same return shape as _parse_legacy."""
    from backend.lane_state import _CodexState  # pylint: disable=import-outside-toplevel
    lines = blob.splitlines()
    st = _CodexState(file_key, collection=collection)
    first_model, is_fork, declared_at = head_scan(blob)
    st.model = first_model

    for line_num, raw in enumerate(lines, 1):
        if not raw:
            continue
        try:
            obj = loads(raw)
        except JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        payload = as_dict(obj.get("payload"))

        ts_dt = _to_dt(obj.get("timestamp"))
        if ts_dt is not None and st.first_event_ts is None:
            st.first_event_ts = ts_dt

        _codex_dispatch(st, str(obj.get("type") or ""), line_num, ts_dt, payload)

    out = _finish_parse(st, len(lines))
    mark_replay(out, declared_at if is_fork else None)
    return out
