"""Codex rollout JSONL → per-file (records list + ctx_turns array).

~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl, one JSON object per
line, each {timestamp, type, payload}. `type` is one of session_meta,
turn_context, event_msg, response_item, world_state, compacted,
inter_agent_communication_metadata; event_msg and response_item carry a
second discriminator in payload.type.

TWO SPELLINGS OF THE SAME EVENTS. Codex 2026-08-18 rewrapped part of the
event_msg set as `item_completed` envelopes discriminated by `item.type`,
and stopped emitting the flat spelling: patch_apply_end became a FileChange
item and agent_message an AgentMessage item. Files before and after that
line are both in the corpus and neither mixes the two, so both spellings are
read — see _codex_item_completed. The rest of the event_msg set
(token_count, task_started, task_complete, turn_aborted,
thread_settings_applied) was not rewrapped.

Entry point is backend.parse.parse_file, which detects the format and calls
parse() here. The return shape is identical to the Kimi parsers' — see
parse_file's docstring, which is the contract for all three.

FOUR PROPERTIES MAKE THE OBVIOUS TOKEN SUM WRONG. Each is a measured
correction, not defensive coding. Figures are from the 51 rollout files on
this box on 2026-08-05, holding 30,249 token_count events; that corpus is
live, so the counts date rather than pin:

1. info.total_token_usage is a MONOTONIC CUMULATIVE counter, and a forked
   thread INHERITS its parent's running total. 8 of the 51 files open with
   43M-70M tokens already on the clock. Summing per-file finals inflates the
   corpus by 1.15x. _codex_token_count therefore differences consecutive
   snapshots and never trusts one total; the first snapshot's baseline is
   recovered as total - last, so the inherited head never enters a delta.
2. Duplicate token_count events repeat the previous last_token_usage
   verbatim — 1,360 of the 30,249 here, in runs of up to 4; the reference
   parser reports a run of 47 in its own corpus. Summing last_token_usage
   double-counts. Differencing drops them for free: a non-advancing
   snapshot yields a delta of zero, which is skipped.
3. cached_input_tokens is a SUBSET of input_tokens, not an addend, as is
   cache_write_input_tokens. Observed cache rates run 90-98%, so adding
   instead of subtracting overstates fresh input by ~40x.
4. token_count payloads carry NO model field and NO agent id, while one
   session mixes models and folds subagent usage into the same counter. The
   model comes from the most recent turn_context / thread_settings_applied
   record preceding the token_count line.

The logic is ported from a standalone CLI parser over the same format,
which was validated against a 46-file corpus.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from orjson import JSONDecodeError, loads

from backend import pricing
from backend.codex_fork import head_scan, mark_replay
from backend.lane_generation import codex_generation_line
from backend.parse_common import (_append_usage_record,
                                  _end_turn, _finish_parse,
                                  _mark_assistant_event, _nonempty_str,
                                  _note_agent_role, _ParseState,
                                  _to_dt,
                                  _turn_boundary)
from backend.json_shape import as_dict, as_list
from backend.flags import ALL_COLLECTIONS_ON, CollectionPolicy
from backend.codex_tool_rows import (  # pylint: disable=unused-import
    _codex_model, _codex_output_text, _diff_churn, _change_churn,
    _codex_dispatch_args, _codex_tool_call, _codex_tool_result, _codex_patch)

# The cumulative counter's fields. All six are differenced so a duplicate
# snapshot is recognised by ALL of them failing to advance, not just by the
# four that are billed.
_CODEX_USAGE_KEYS = (
    "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
    "output_tokens", "reasoning_output_tokens", "total_tokens",
)


# Every top-level record type the format emits; the format's fingerprint.
RECORD_TYPES = frozenset({
    "session_meta", "turn_context", "event_msg", "response_item",
    "world_state", "compacted", "inter_agent_communication_metadata",
})


@dataclass
class _CodexState(_ParseState):
    """_ParseState plus the state only the Codex format needs."""
    # Previous cumulative token snapshot, for differencing. None until the
    # file's first token_count seeds the inherited baseline.
    prev_usage: dict | None = None
    # response_item/web_search_call items since the previous billing
    # record. web_search_end is a companion event, not another call.
    web_search_requests: int = 0
    # Model in force, from the most recent turn_context / settings record.
    # parse() opens it as the file's FIRST declared model — the model the
    # parent had in force where a fork cut, inherited by the replayed
    # prefix (issue #653) — and each declaration in the body overwrites it.
    model: str | None = None
    # (index into tool_uses, turn_id) of the most recent apply_patch call, to
    # carry a patch_apply_end's churn back onto the call that caused it.
    patch_target: tuple[int, str | None] | None = None
    # Last rate-limit condition booked, so a condition spanning hundreds of
    # token_count events books one hit rather than hundreds.
    last_rl_kind: str | None = None
    # This thread's session id, from session_meta. A fork REUSES its
    # parent's, which is what makes it the right half of a record's
    # cross-file identity — see _codex_record_uuid.
    session_id: str | None = None
    # This rollout's own thread: the FIRST session_meta's `id`, and whether
    # one has been seen yet. A later session_meta under another id is a
    # replayed parent's, and its agent_role is the parent's, not ours.
    thread_id: str | None = None
    thread_seen: bool = False


def _codex_first_declared_model(blob: bytes) -> str | None:
    """The first model this file declares, in file order.

    The first declaration is the model the parent had in force at the
    fork point - the inherited settings - so it attributes a fork's
    replayed prefix (issue #653). The scan, and the fork/replay facts
    read beside it, live in backend/codex_fork (issue #687).
    """
    return head_scan(blob)[0]


def _codex_record_uuid(st: _CodexState, cumulative: dict,
                       line_num: int) -> str:
    """Cross-file identity for ONE request.

    THE FIFTH TRAP, and the one that only bites once records from several
    files are summed. A resumed or forked rollout REPLAYS its parent's
    history into its own file, so the same request is journalled in many
    files: across the 51 local rollouts, 28,889 per-file records represent
    only 12,945 distinct requests, and one session id spans 39 files.
    Aggregating those per-file rows without dedup over-counts by ~2.2x.

    codexmeter already solved this — `records.uuid` plus `is_canonical`,
    resolved by ingest.recompute_canonical(), which keeps exactly one row
    per uuid. What that mechanism needs from a parser is a uuid that is
    IDENTICAL across every file replaying a given request, which
    "<file_key>:<line>" can never be.

    A request is identified by the position it advanced its thread's
    cumulative counter to: the counter is monotonic within a thread, and a
    replay reproduces the same readings. Measured over the local corpus,
    3,806 keys repeat across files and exactly ONE of them carries
    conflicting usage, so collapsing them is safe to ~0.03%.

    Falls back to the per-file identity when the file declares no
    session_meta — a mid-file window rather than a whole rollout. Such a
    fragment has no thread identity to share, so per-file is the honest
    answer; it just cannot dedup against anything.
    """
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
        st.prev_usage = {k: cumulative[k] - int(last.get(k) or 0)
                         for k in _CODEX_USAGE_KEYS}
    delta = {k: cumulative[k] - st.prev_usage[k] for k in _CODEX_USAGE_KEYS}
    st.prev_usage = cumulative
    if (all(delta[k] <= 0 for k in _CODEX_USAGE_KEYS)
            and not st.web_search_requests):
        return  # Trap 2: duplicate token_count, or a non-advancing snapshot.

    # Trap 3: cached and cache-write inputs are SUBSETS of input_tokens.
    # cache_write is its own billed bucket, NOT part of fresh: Codex charges
    # a cache write at 1.25x uncached input, so folding it into fresh
    # underbills it and folding it into cached overbills by 10x. It reads 0
    # across the whole local corpus, which is a property of the models that
    # corpus used and not of the format — the field is real and billable
    # (codex-rs/protocol/src/protocol.rs declares and sums it), so it is
    # counted and carried on its own.
    total_in = max(0, delta["input_tokens"])
    read = max(0, delta["cached_input_tokens"])
    create = max(0, delta["cache_write_input_tokens"])
    fresh = max(0, total_in - read - create)
    output = max(0, delta["output_tokens"])
    # Subset of output, not an addend — carried, never added to cost.
    reasoning = max(0, delta["reasoning_output_tokens"])
    # Trap 4: the payload names no model; the surrounding turn_context does.

    # The long-context meter is a property of the model's rate card, not of
    # the plan that served the request — every record is billed as if it
    # were an API call (issue #194), so a subscription rollout above the
    # threshold bills the meter exactly as an API-key one does.
    _append_usage_record(
        st, line_num, ts,
        _codex_record_uuid(st, cumulative, line_num),
        _codex_model(st.model),
        (fresh, create, read, output),
        reasoning=reasoning,
        long_context=(total_in > pricing.long_context_threshold(st.model)),
        web_search_requests=st.web_search_requests or None,
        generation_s=st.lane_generation.bind() if st.lane_generation is not None else None,
    )
    st.web_search_requests = 0


def _codex_rate_limit(st: _CodexState, line_num: int, ts: datetime | None,
                      payload: dict) -> None:
    """Book a rate-limit hit, if this token_count reports one.

    UNVERIFIED AGAINST REAL DATA: rate_limit_reached_type and
    spend_control_reached are present on all 30,249 token_count payloads in
    the local corpus and null on every one of them (peak primary utilisation
    28%), so no hit has ever been observed. The fields are read by name; the
    VALUE a hit carries is unknown, so any non-null is booked and rendered
    with str(). A condition holds across many consecutive token_count events,
    so consecutive repeats collapse into one hit.
    """
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
    """Route one item_completed to the handler for its item type.

    Codex 2026-08-18 rewrapped several event_msg payloads as `item_completed`
    envelopes discriminated by `item.type`, and stopped emitting the flat
    ones. The two spellings are disjoint per file — no file among the 287 on
    this box holds both — so handling each is not double counting.

    Only the two items that carry measured quantities are read here, plus
    Reasoning, which counts nothing but is model output: like its
    response_item/reasoning twin it can open the turn's reply, ending the
    reply-latency window. CommandExecution restates the
    response_item/custom_tool_call that _codex_tool_call already books;
    counting it too would double every tool row. UserMessage,
    SubAgentActivity, ContextCompaction, Extension, ImageView and
    CollabAgentToolCall have no counterpart in the old event_msg set and
    are not counted by either spelling.
    """
    item = as_dict(payload.get("item"))
    itype = item.get("type")
    if itype == "FileChange":
        # `status` replaces patch_apply_end's boolean `success`; the only
        # other value observed on an item is "failed".
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
        # event_msg/agent_message is a SUPERSET of response_item/message with
        # role=assistant (every one of 496 distinct assistant texts sampled
        # appears in both), so only this side is counted.
        st.text_chars_since_turn += len(str(payload.get("message") or ""))
        _mark_assistant_event(st, ts)
    elif ptype == "agent_reasoning":
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
    if ptype in ("custom_tool_call", "function_call"):
        _codex_tool_call(st, line_num, ts, payload)
        _mark_assistant_event(st, ts)
    elif ptype == "web_search_call":
        st.web_search_requests += 1
        _mark_assistant_event(st, ts)
    elif ptype in ("reasoning", "local_shell_call"):
        # Model output that is not a tool_uses row here: it can still be
        # where the reply began.
        _mark_assistant_event(st, ts)
    elif ptype in ("custom_tool_call_output", "function_call_output"):
        _codex_tool_result(st, payload)


def _codex_dispatch(st: _CodexState, rtype: str, line_num: int,
                    ts: datetime | None, payload: dict) -> None:
    if st.lane_generation is not None:
        codex_generation_line(st.lane_generation, rtype, payload, ts)
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
        # The role is the first NON-EMPTY one among the session_metas of
        # THIS thread (same `id` as the first). A fork replays its
        # parent's session_meta after its own: in the corpus a forked
        # subagent with no role of its own would otherwise take its
        # parent's (1 of 158 multi-session_meta rollouts, 2026-09-24).
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
