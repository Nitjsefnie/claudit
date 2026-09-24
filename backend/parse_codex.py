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

The logic is ported from ~/.agent-bundle/scripts/parse_codex.py, a
standalone CLI over the same format, validated against a 46-file corpus.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime

from orjson import JSONDecodeError, loads

from backend import pricing
from backend.bash_argv import argv_churn
from backend.bash_churn import bash_churn
from backend.parse_common import (_append_tool_use, _append_usage_record,
                                  _end_turn, _finish_parse,
                                  _mark_assistant_event, _nonempty_str,
                                  _note_agent_role, _ParseState,
                                  _settle_lane_tool_result, _to_dt,
                                  _turn_boundary)
from backend.tool_errors import ERROR_KIND_FAILED

# The single custom tool is `exec`, whose input is a JS program calling
# tools.<api>({...}). The api is the useful tool name — `exec` alone would
# collapse every shell command, patch and plan update into one bucket.
_CODEX_API_RE = re.compile(r"tools\.([A-Za-z_][A-Za-z_0-9]*)\s*\(")

# Model in force -> canonical pricing label. Substring, not prefix: the corpus
# carries "gpt5.6-sol" (missing separator) alongside "gpt-5.6-sol". An
# unrecognised model bills at the FLAGSHIP rate, the opposite of the Kimi
# ladder's conservative fallback: here a wrong overcount is visible and
# arguable, while a wrong undercount silently understates the bill.
#
# EVERY MODEL THE CORPUS NAMES NEEDS A ROW HERE, and the cost of forgetting
# one is not merely a mispriced record: the fallback RELABELS it, so the
# model disappears from the dashboard entirely and its usage is reported as
# the flagship's. That is what happened to Astra, which shipped on
# 2026-09-04 and read as Sol until this row was added.
#
# The flagship is whichever current model is most expensive, so the
# fallback keeps erring upward — Astra at 2.5x Sol now holds that seat.
_CODEX_FLAGSHIP = "gpt-6-astra"
# Matched as SUBSTRINGS, in order, so a more specific id must come before
# any needle it contains: `gpt-6-sol` contains `sol`, and listed after it
# would be relabelled GPT-5.6 Sol and billed at twice its price.
_CODEX_MODEL_MAP = (
    ("astra", "gpt-6-astra"),
    ("gpt-6-sol", "gpt-6-sol"),
    ("gpt-6-luna", "gpt-6-luna"),
    ("terra", "gpt-5.6-terra"),
    ("luna", "gpt-5.6-luna"),
    ("sol", "gpt-5.6-sol"),
)

# The cumulative counter's fields. All six are differenced so a duplicate
# snapshot is recognised by ALL of them failing to advance, not just by the
# four that are billed.
_CODEX_USAGE_KEYS = (
    "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
    "output_tokens", "reasoning_output_tokens", "total_tokens",
)

# A tool result whose first line starts with one of these is a failure. The
# payload carries no status field of its own.
_CODEX_FAILURE_HEADS = ("Script failed", "collab spawn failed")

# function_call names that dispatch a subagent. Both spellings are in the
# corpus: the prefixed one in older rollouts.
_CODEX_DISPATCH_TOOLS = ("spawn_agent", "multi_agent_v1__spawn_agent")

# Every top-level record type the format emits; the format's fingerprint.
RECORD_TYPES = frozenset({
    "session_meta", "turn_context", "event_msg", "response_item",
    "world_state", "compacted", "inter_agent_communication_metadata",
})


def _codex_model(raw: str | None) -> str:
    """Model string in force -> canonical pricing label.

    Only canonical labels may reach pricing.compute_cost: an unmapped id
    resolves to the cheapest Codex fallback — off by 50x on fresh input
    for the flagship, silently.
    """
    lowered = (raw or "").lower()
    for needle, label in _CODEX_MODEL_MAP:
        if needle in lowered:
            return label
    return _CODEX_FLAGSHIP


@dataclass
class _CodexState(_ParseState):
    """_ParseState plus the state only the Codex format needs."""
    # Previous cumulative token snapshot, for differencing. None until the
    # file's first token_count seeds the inherited baseline.
    prev_usage: dict | None = None
    # Model in force, from the most recent turn_context / settings record.
    model: str | None = None
    # The file's only declared model, when it declares exactly one. Attributes
    # the token_count records that precede the first turn_context.
    sole_model: str | None = None
    # (index into tool_uses, turn_id) of the most recent apply_patch call, to
    # carry a patch_apply_end's churn back onto the call that caused it.
    patch_target: tuple[int, str | None] | None = None
    # Last rate-limit condition booked, so a condition spanning hundreds of
    # token_count events books one hit rather than hundreds.
    last_rl_kind: str | None = None
    # True once any token_count has named a ChatGPT plan. Sticky: a later
    # payload that omits rate_limits does not turn a subscription rollout
    # back into an API one.
    subscription: bool = False
    # This thread's session id, from session_meta. A fork REUSES its
    # parent's, which is what makes it the right half of a record's
    # cross-file identity — see _codex_record_uuid.
    session_id: str | None = None
    # This rollout's own thread: the FIRST session_meta's `id`, and whether
    # one has been seen yet. A later session_meta under another id is a
    # replayed parent's, and its agent_role is the parent's, not ours.
    thread_id: str | None = None
    thread_seen: bool = False


def _codex_declared_models(blob: bytes) -> set[str]:
    """Every model this file declares, from a cheap pre-scan.

    A forked rollout replays its parent's history before the new thread emits
    a turn_context, so the leading token_count records have no model in front
    of them. Where the file declares exactly one model overall there is only
    one answer; where it mixes models the prefix stays on the fallback rather
    than guessing which side of the switch it belonged to.

    The scan JSON-decodes only lines that mention a model-declaring record —
    on a 20MB rollout that is a few hundred lines out of tens of thousands.
    """
    models: set[str] = set()
    for raw in blob.splitlines():
        if b'"turn_context"' not in raw and b'"thread_settings_applied"' not in raw:
            continue
        try:
            obj = loads(raw)
        except JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            continue
        if obj.get("type") == "turn_context":
            name = payload.get("model")
        elif payload.get("type") == "thread_settings_applied":
            name = (payload.get("thread_settings") or {}).get("model")
        else:
            continue
        if name:
            models.add(str(name))
    return models


def _codex_output_text(payload: dict) -> str:
    """Flatten a *_call_output payload into one string."""
    out = payload.get("output")
    if isinstance(out, str):
        return out
    if isinstance(out, list):
        parts = []
        for chunk in out:
            if isinstance(chunk, dict) and chunk.get("text"):
                parts.append(str(chunk["text"]))
            elif isinstance(chunk, str):
                parts.append(chunk)
        return "\n".join(parts)
    return ""


def _diff_churn(diff: object) -> tuple[int, int]:
    """(lines_added, lines_deleted) for one unified diff.

    Unlike the Kimi formats, whose tool results carry no diff and whose churn
    has to be inferred from the CALL's arguments, Codex journals the applied
    diff — so this counts what was actually written, not what was requested.
    """
    added = deleted = 0
    for line in str(diff or "").splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            deleted += 1
    return added, deleted


def _change_churn(change: dict) -> tuple[int, int]:
    """(lines_added, lines_deleted) for one entry of a patch's `changes`.

    Only an `update` carries a unified_diff. An `add` and a `delete` carry
    the whole file body under `content` instead, and reading unified_diff
    alone books them at zero — 1,355 of the 3,084 changes on the reference
    corpus (2026-08-20..25) are one of those two, so it is not an edge case.
    A pure rename is an `update` whose unified_diff is absent: no line moved.
    """
    if change.get("unified_diff") is not None:
        return _diff_churn(change.get("unified_diff"))
    lines = len(str(change.get("content") or "").splitlines())
    if change.get("type") == "add":
        return lines, 0
    if change.get("type") == "delete":
        return 0, lines
    return 0, 0


_CODEX_CMD_STR = re.compile(r"""["']?cmd["']?\s*:\s*(?=")""")
_CODEX_CMD_ARGV = re.compile(r"""["']?command["']?\s*:\s*(?=\[)""")


def _codex_json_values(src: str, pattern: re.Pattern[str]):
    """Every JSON value in `src` introduced by `pattern`.

    Yields the decoded value and skips whatever span it occupied, so a
    key that appears again INSIDE a value already read is not mined a
    second time.
    """
    decoder = json.JSONDecoder()
    consumed_to = 0
    for match in pattern.finditer(src):
        if match.end() < consumed_to:
            continue
        try:
            value, end = decoder.raw_decode(src, match.end())
        except ValueError:
            continue
        consumed_to = end
        yield value


def _codex_program_churn(src: str) -> tuple[int, int]:
    """Churn enumerable from ONE exec program's shell payloads.

    A custom_tool_call's input is a JS program, so the shell text is a
    string literal inside it: `tools.exec_command({cmd:"..."})`, and
    `tools.monitor({command:["bash","-lc","..."]})` for the API that
    takes an argv array instead. Both values are written as JSON, so
    raw_decode reads them back exactly — escapes, embedded quotes and
    all — rather than a regex guessing where the literal ends.

    stdlib json, not orjson, because only JSONDecoder.raw_decode can
    start at an offset inside a larger document and report where the
    value ended — orjson parses whole documents only.

    Backtick templates (3% of cmd values) are skipped: JS interpolation
    means the text is not the command, and a template is not JSON.

    A decoded value's own span is skipped afterwards, so a script that
    happens to contain `cmd:"..."` in its own text is not mined twice.
    """
    added = deleted = 0
    for value in _codex_json_values(src, _CODEX_CMD_STR):
        a, d = bash_churn(str(value))
        added += a
        deleted += d
    for value in _codex_json_values(src, _CODEX_CMD_ARGV):
        a, d = argv_churn(value)
        added += a
        deleted += d
    return added, deleted


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
    info = payload.get("info") or {}
    total = info.get("total_token_usage") or {}
    if not total:
        return
    cumulative = {k: int(total.get(k) or 0) for k in _CODEX_USAGE_KEYS}
    if st.prev_usage is None:
        # Trap 1. This file's inherited baseline is its first cumulative
        # snapshot minus the request that produced it, so the first delta is
        # that request alone and the parent's millions never enter a sum.
        last = info.get("last_token_usage") or {}
        st.prev_usage = {k: cumulative[k] - int(last.get(k) or 0)
                         for k in _CODEX_USAGE_KEYS}
    delta = {k: cumulative[k] - st.prev_usage[k] for k in _CODEX_USAGE_KEYS}
    st.prev_usage = cumulative
    if all(delta[k] <= 0 for k in _CODEX_USAGE_KEYS):
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

    # Trap 5: the long-context meter is an API-billing tier, not a property
    # of the request. Codex on a ChatGPT plan spends credits off one
    # short-context rate card that has no long-context column at all, so a
    # subscription rollout is billed flat however large its prompt gets.
    # plan_type rides rate_limits on every one of the 27,029 token_count
    # payloads in the local corpus, all of them "pro" — a rollout that names
    # no plan is the pay-as-you-go shape, where the meter is real.
    if (payload.get("rate_limits") or {}).get("plan_type"):
        st.subscription = True

    # Trap 4: the payload names no model; the surrounding turn_context does.
    _append_usage_record(
        st, line_num, ts,
        _codex_record_uuid(st, cumulative, line_num),
        _codex_model(st.model or st.sole_model),
        (fresh, create, read, output),
        reasoning=reasoning,
        long_context=(not st.subscription
                      and total_in > pricing.LONG_CONTEXT_THRESHOLD),
    )


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
    rl = payload.get("rate_limits") or {}
    kind = rl.get("rate_limit_reached_type")
    if not kind and not rl.get("spend_control_reached"):
        st.last_rl_kind = None
        return
    key = str(kind) if kind else "spend_control_reached"
    if key == st.last_rl_kind:
        return
    st.last_rl_kind = key
    primary = rl.get("primary") or {}
    st.rate_limit_hits.append({
        "line": line_num,
        "ts": ts.isoformat() if ts is not None else "",
        "content": (
            f"{key} (plan={rl.get('plan_type')}, "
            f"primary {primary.get('used_percent')}% of "
            f"{primary.get('window_minutes')}m window)"
        )[:500],
    })


def _codex_dispatch_args(payload: dict) -> tuple | None:
    """(agent_type, agent_model, prompt_chars, brief_ref) a spawn_agent
    function_call asked for, or None for any other call.

    `arguments` is a JSON string. Its `message` -- the brief -- is
    ENCRYPTED, so neither its length nor a brief reference inside it
    means anything: both prompt-shape fields stay None on this lane.
    Arguments that do not parse to an object attribute nothing.
    """
    if payload.get("type") != "function_call" or (
            payload.get("name") not in _CODEX_DISPATCH_TOOLS):
        return None
    args = payload.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args) if args else None
        except json.JSONDecodeError:
            args = None
    if not isinstance(args, dict):
        return None, None, None, None
    return (_nonempty_str(args.get("agent_type")),
            _nonempty_str(args.get("model")), None, None)


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


def _codex_tool_call(st: _CodexState, line_num: int, ts: datetime | None,
                     payload: dict) -> None:
    """Record one tool call.

    A custom_tool_call is always `exec`, whose real identity is the first
    tools.<api> its JS program calls — exec_command, apply_patch, write_stdin,
    update_plan. A function_call names its function directly (spawn_agent,
    wait_agent, send_message, ...).
    """
    churn = (0, 0)
    if payload.get("type") == "custom_tool_call":
        program = str(payload.get("input") or "")
        apis = _CODEX_API_RE.findall(program)
        name = apis[0] if apis else str(payload.get("name") or "")
        churn = _codex_program_churn(program)
    else:
        apis = [str(payload.get("name") or "")]
        name = apis[0]
    _append_tool_use(
        st, line_num, ts, name, str(payload.get("call_id") or ""), churn,
        model=_codex_model(st.model or st.sole_model),
        dispatch=_codex_dispatch_args(payload),
    )
    if "apply_patch" in apis:
        turn_id = (payload.get("internal_chat_message_metadata_passthrough")
                   or {}).get("turn_id")
        st.patch_target = (len(st.tool_uses) - 1, turn_id)


def _codex_tool_result(st: _CodexState, payload: dict) -> None:
    """Settle one tool call's is_error.

    The payload carries no status field; failure is announced in the first
    line of the output text. A backgrounded script ("Script running with cell
    ID ...") has not failed — it has not finished.
    """
    call_id = payload.get("call_id")
    if not call_id:
        return
    text = _codex_output_text(payload)
    head = text.split("\n", 1)[0] if text else ""
    is_err = head.startswith(_CODEX_FAILURE_HEADS)
    _settle_lane_tool_result(st, call_id, is_err, text if is_err else "")


def _codex_patch(st: _CodexState, line_num: int, ts: datetime | None,
                 ok: bool, changes: object, turn_id: object) -> None:
    """Attribute one applied patch's line churn.

    Called for both spellings of the same event — the `patch_apply_end`
    event_msg, and the `FileChange` item of the `item_completed` event_msg
    that replaced it (see _codex_item_completed). The caller resolves the
    success flag and the changes map; everything below is common to both.

    A patch does NOT share a call_id namespace with the tool calls (0 of
    1,727 patch call_ids matched one in the old spelling, 0 of 2,623
    FileChange item ids in the new — both carry "exec-<uuid>"), so the churn
    is carried back onto the most recent apply_patch call of the SAME turn.
    One exec can apply several patches; they sum onto that call.

    Where no such call exists in this file the patch came from a subagent,
    whose tool calls the parent rollout does not record (522 of the 524
    unattributable patches in the local corpus sit in files with
    sub_agent_activity). Those get a row of their own rather than being
    dropped: the work happened, and this file is the only record of it.

    A failed patch changed nothing, so it contributes no churn — the same
    rule _resolve_tool_errors applies to a rejected edit.
    """
    added = deleted = 0
    if ok and isinstance(changes, dict):
        for change in changes.values():
            if isinstance(change, dict):
                a, d = _change_churn(change)
                added += a
                deleted += d
    target = st.patch_target
    if target is not None and target[1] == turn_id:
        tool_use = st.tool_uses[target[0]]
        tool_use["lines_added"] += added
        tool_use["lines_deleted"] += deleted
        return
    # No tool_call_id: _resolve_tool_errors must leave this row's is_error,
    # which is settled here and nowhere else, alone. A failed patch
    # carried no output text to classify, so its kind is `failed` — the
    # patch attempt is the thing that never produced a change.
    _append_tool_use(
        st, line_num, ts, "apply_patch", "", (added, deleted),
        model=_codex_model(st.model or st.sole_model),
    )
    st.tool_uses[-1]["is_error"] = not ok
    st.tool_uses[-1]["error_kind"] = None if ok else ERROR_KIND_FAILED


def _codex_item_completed(st: _CodexState, line_num: int,
                          ts: datetime | None, payload: dict) -> None:
    """Route one item_completed to the handler for its item type.

    Codex 2026-08-18 rewrapped several event_msg payloads as `item_completed`
    envelopes discriminated by `item.type`, and stopped emitting the flat
    ones. The two spellings are disjoint per file — no file among the 287 on
    this box holds both — so handling each is not double counting.

    Only the two items that carry measured quantities are read here.
    CommandExecution restates the response_item/custom_tool_call that
    _codex_tool_call already books; counting it too would double every tool
    row. Reasoning, UserMessage, SubAgentActivity, ContextCompaction,
    Extension, ImageView and CollabAgentToolCall have no counterpart in the
    old event_msg set and are not counted by either spelling.
    """
    item = payload.get("item")
    if not isinstance(item, dict):
        return
    itype = item.get("type")
    if itype == "FileChange":
        # `status` replaces patch_apply_end's boolean `success`; the only
        # other value observed on an item is "failed".
        _codex_patch(st, line_num, ts, item.get("status") == "completed",
                     item.get("changes"), payload.get("turn_id"))
    elif itype == "AgentMessage":
        st.text_chars_since_turn += len(_codex_item_text(item))
        _mark_assistant_event(st)


def _codex_item_text(item: dict) -> str:
    """Flatten an item's `content` list into one string.

    Unlike a response_item's content chunks, whose discriminator is the
    lowercase "text", an item's are typed "Text" — matching on the
    discriminator rather than reading the field would silently return "".
    """
    parts = []
    for chunk in (item.get("content") or []):
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
        _mark_assistant_event(st)
    elif ptype == "patch_apply_end":
        _codex_patch(st, line_num, ts, bool(payload.get("success")),
                     payload.get("changes"), payload.get("turn_id"))
    elif ptype == "item_completed":
        _codex_item_completed(st, line_num, ts, payload)
    elif ptype == "thread_settings_applied":
        model = (payload.get("thread_settings") or {}).get("model")
        if model:
            st.model = str(model)


def _codex_response_item(st: _CodexState, ptype: str, line_num: int,
                         ts: datetime | None, payload: dict) -> None:
    if ptype in ("custom_tool_call", "function_call"):
        _codex_tool_call(st, line_num, ts, payload)
        _mark_assistant_event(st)
    elif ptype in ("custom_tool_call_output", "function_call_output"):
        _codex_tool_result(st, payload)


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
    # call, and a row each would count it twice.


def parse(file_key: str, blob: bytes) -> dict:
    """Parse one Codex rollout JSONL. Same return shape as _parse_legacy."""
    lines = blob.splitlines()
    declared = _codex_declared_models(blob)
    st = _CodexState(file_key)
    st.sole_model = next(iter(declared)) if len(declared) == 1 else None

    for line_num, raw in enumerate(lines, 1):
        if not raw:
            continue
        try:
            obj = loads(raw)
        except JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            payload = {}

        ts_dt = _to_dt(obj.get("timestamp"))
        if ts_dt is not None and st.first_event_ts is None:
            st.first_event_ts = ts_dt

        _codex_dispatch(st, str(obj.get("type") or ""), line_num, ts_dt, payload)

    return _finish_parse(st, len(lines))
