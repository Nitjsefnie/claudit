"""JSONL → per-file (records list + ctx_turns array).

Each call processes ONE jsonl with within-file requestId max-merge
(message.id where a line has no requestId — see _merge_key).
Cross-file uuid dedup is handled separately by ingest. Cost is
precomputed using pricing.MODEL_RATES; semantic or rate changes
require a PARSER_VERSION bump to invalidate stored results.

_LineWalk collects per-file state; helpers project records and ctx_turns.
"""
from __future__ import annotations

from collections import Counter

from orjson import JSONDecodeError, loads

from backend import parse_collection, parse_lanes, parse_tool_rows
from backend.flags import ALL_COLLECTIONS_ON, CollectionPolicy
from backend.parse_tool_rows import (  # pylint: disable=unused-import
    _content_metrics, _tool_use_row, _tool_churn, _tool_access,
    _dispatch_args, _dispatch_name, _resolve_tool_errors, _line_count,
    READ_TOOLS, WRITE_TOOLS, DISPATCH_TOOLS)
from backend.agent_types import canonical_agent_type
from backend.constants import (DEFAULT_AGENT_TYPE, INTERRUPT_MARKER)
from backend.tool_errors import (ERROR_KIND_FAILED,  # pylint: disable=unused-import
                                 ERROR_KIND_REJECTED,  # pylint: disable=unused-import
                                 ERROR_KIND_TOOL_ERROR, ERROR_TEXT_MAX,
                                 _classify_error, _flatten_result_text,
                                 _pg_text, _result_size)
from backend.turn_flags import TurnWindow
from backend.ctx_turns import _build_ctx_turns
from backend.prompt_gate import _is_prompt_text
from backend.parse_common import _to_dt, iter_lines, resolve_cache_writes
from backend.parse_lanes import LANE_PARSERS, sniff_format, to_claudit
from backend.rereads import resolve_rereads
from backend.json_shape import as_dict, as_list, dict_list
from backend.parse_projection import project_records
from backend.reply_timing import GenerationTiming, ReplyTiming, failed_attempt


# A `type:"assistant"` record with isApiErrorMessage=True and
# error="rate_limit" IS an account-cap hit unless its text matches one
# of these. Matching the exceptions (a deny-list) rather than the
# wanted wordings is deliberate: the cap wording has already moved
# twice ("You're out of extra usage" → "You've hit your weekly limit"
# 2026-08 → "You've hit your session limit" 2026-09), and an allow-list
# stops counting SILENTLY when it moves again, whereas a missed
# exception over-counts visibly. Every entry below was found by
# enumerating each distinct error/text pair in the live corpus, not
# guessed:
#   "temporarily limiting"  transient per-minute 429 (n=32)
#   "request rejected (429)" per-request 429 from a proxy lane (n=1)
#   "currently unavailable"  model availability, mislabelled
#                            error="rate_limit" (n=4)
_TRANSIENT_RATE_LIMIT_MARKERS = (
    "server is temporarily limiting requests",
    "request rejected (429)",
    "currently unavailable",
)


def _merge_usage_max(existing, incoming):
    if existing is None:
        return incoming
    if incoming is None:
        return existing
    if isinstance(existing, (int, float)) and isinstance(incoming, (int, float)):
        return max(existing, incoming)
    if isinstance(existing, dict) and isinstance(incoming, dict):
        out = dict(existing)
        for k, v in incoming.items():
            out[k] = _merge_usage_max(out.get(k), v) if k in out else v
        return out
    return existing


def _merge_key(obj: dict, msg: dict) -> str:
    """The Phase 1 merge key of one assistant line; '' leaves it unmerged.

    Claude Code writes one line per content block, and every line of one
    API message shares its requestId. A transcript served by another
    endpoint (Z.ai) writes no requestId, so the message.id those lines
    still share is the key instead. Namespaced so a message id can never
    collide with a requestId.
    """
    req_id = obj.get("requestId", "") or ""
    if req_id:
        return req_id
    msg_id = msg.get("id")
    return f"msg:{msg_id}" if isinstance(msg_id, str) and msg_id else ""


def _flatten_usage(usage: dict) -> dict:
    """Flatten a usage dict by summing iterations[] into top-level fields.

    Anthropic's API returns multi-turn responses (e.g. advisor_message
    iterations) as an ``iterations`` array. The top-level ``input_tokens``,
    ``output_tokens``, etc. in these records are partial snapshots — only
    the sum across iterations reflects the true token spend.
    """
    iters = usage.get("iterations")
    if not iters:
        return usage
    iters = dict_list(iters)
    if not iters:
        return usage
    out = dict(usage)
    int_keys = (
        "input_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "output_tokens",
    )
    nested_keys = ("cache_creation", "server_tool_use")
    for k in int_keys:
        out[k] = sum((i.get(k) or 0) for i in iters)
    for k in nested_keys:
        merged: dict[str, int] = {}
        for i in iters:
            for nk, nv in as_dict(i.get(k)).items():
                if isinstance(nv, int):
                    merged[nk] = merged.get(nk, 0) + nv
        if merged:
            out[k] = merged
    return out


class _LineWalk:
    """Mutable per-file state for the line-by-line pass."""

    def __init__(self, file_key: str, collection: CollectionPolicy = ALL_COLLECTIONS_ON) -> None:
        self.collect = parse_collection.parse_collection(collection)
        self.window = TurnWindow() if self.collect.turn_context else None
        self.file_key = file_key
        self.seen_request: dict[str, dict] = {}
        self.records_in_order: list[dict] = []
        self.user_text_lines: list[int] = []
        # Per-prompt timestamps (issue #214), index-aligned with
        # user_text_lines: a range's prompt total counts per own ts.
        self.user_text_ts: list[str | None] = []
        self.rate_limit_hits: list[dict] = []
        self.tool_uses: list[dict] = []
        self.seen_tool_ids: set[str] = set()
        # Tool results settle the pending tool calls after the line walk.
        self.tool_result_is_error: dict[str, bool] = {}
        # Failed-result text for error_kind and error_text.
        self.tool_result_text: dict[str, str] = {}
        # Optional result sizes include failures: their text also fills context.
        self.tool_result_chars: dict[str, int] = {}
        # All non-interrupt user text anchors latency, independently of
        # prompt counting. Interrupts, failed attempts and replies clear it.
        self.reply_timing = ReplyTiming()
        self.generation_timing = GenerationTiming() if collection.enabled("generation_speed") else None
        # A UUID on an earlier, different line is a replay; neither clock moves.
        self.seen_user_uuids: dict[str, int] = {}
        # Per-file attribution; resolve_agent_type defines precedence.
        self.attribution_agents: Counter[str] = Counter()
        self.agent_setting: str | None = None

    def observe_metadata(self, obj: dict, kind: str) -> None:
        """Track optional request windows and mandatory per-file role identity."""
        if self.window is not None:
            self.window.observe(obj)
        if kind == "agent-setting":
            name = obj.get("agentSetting")
            if isinstance(name, str) and name:
                self.agent_setting = name
        else:
            attribution = obj.get("attributionAgent")
            if isinstance(attribution, str) and attribution:
                self.attribution_agents[attribution] += 1

    def handle_user_text(
        self, text: str, line_num: int, ts_str: str, mutate_anchor: bool = True
    ) -> None:
        """Apply instrumentation/interrupt/anchor logic to a user text string.

        Mirrors the string-content branch for list-form text blocks.
        Replayed user records pass ``mutate_anchor=False`` so they still
        count toward ``prompt_count`` and ctx-turn boundaries but never
        touch the latency anchor.
        """
        if not text.strip():
            return
        stripped = text.lstrip()
        if stripped.startswith(INTERRUPT_MARKER):
            if mutate_anchor:
                self.reply_timing.clear()
            return
        if not _is_prompt_text(text):
            if mutate_anchor:
                self.reply_timing.anchor(_to_dt(ts_str))
            return
        # Keep prompt_count and prompt_ts index-aligned (issue #214).
        self._count_prompt(line_num, ts_str, mutate_anchor)

    def _count_prompt(self, line_num: int, ts_str: str,
                      mutate_anchor: bool) -> None:
        """Book ONE prompt: the count line, the #214 timestamp and the
        reply-latency anchor land together, once per counted record.
        Injected text anchors separately without booking a prompt."""
        self.user_text_lines.append(line_num)
        ts_dt = _to_dt(ts_str)
        self.user_text_ts.append(ts_dt.isoformat() if ts_dt is not None else None)
        if mutate_anchor:
            self.reply_timing.anchor(ts_dt)

    def handle_rate_limit(self, obj: dict, line_num: int) -> bool:
        """Book a rate-limit hit if this record is one. Returns True when
        the record was consumed — a rate-limit error record carries no
        usage, so there is nothing else to do with it.

        Detection: hits live on type:"assistant" records with
        isApiErrorMessage=True and error="rate_limit". That shape IS
        the signal; the text is consulted only to drop the transient
        per-minute 429 (_TRANSIENT_RATE_LIMIT_MARKERS), which is a
        retry rather than an account cap.
        """
        if not (
            obj.get("type", "") == "assistant"
            and obj.get("isApiErrorMessage") is True
            and obj.get("error") == "rate_limit"
        ):
            return False
        message = as_dict(obj.get("message"))
        content_list = as_list(message.get("content") or [])
        joined = " ".join(
            str(c.get("text", ""))
            for c in content_list
            if isinstance(c, dict) and c.get("type") == "text"
        )
        low = joined.lower()
        if not any(m in low for m in _TRANSIENT_RATE_LIMIT_MARKERS):
            self.rate_limit_hits.append({
                "line": line_num,
                "ts": obj.get("timestamp", "") or "",
                "content": joined[:500],
            })
        # Still the assistant responding, so it terminates any open
        # reply-latency window (see ReplyTiming.consume).
        self.reply_timing.consume(obj)
        if self.generation_timing:
            self.generation_timing.observe(obj, message)
        return True

    def handle_message_line(self, obj: dict, msg: dict, line_num: int) -> None:
        """Role dispatch, after the line walk has observed generation by type."""
        role = msg.get("role")
        if role == "user":
            self.handle_user_line(obj, msg, line_num)
        elif role == "assistant":
            self.handle_assistant_line(obj, msg, line_num)

    def handle_user_line(self, obj: dict, msg: dict, line_num: int) -> None:
        """One user-role record: replay detection, then content bookkeeping."""
        uuid = obj.get("uuid") or ""
        is_replay = False
        if uuid:
            first_line = self.seen_user_uuids.get(uuid)
            if first_line is None:
                self.seen_user_uuids[uuid] = line_num
            elif first_line != line_num:
                is_replay = True
        mutate_anchor = not is_replay

        content = msg.get("content")
        ts_str = obj.get("timestamp", "") or ""
        if isinstance(content, str) and content.strip():
            self.handle_user_text(
                content, line_num, ts_str, mutate_anchor=mutate_anchor
            )
        elif isinstance(content, list):
            self._handle_user_blocks(content, line_num, ts_str, mutate_anchor)

    def _handle_user_blocks(self, blocks, line_num: int, ts_str: str,
                            mutate_anchor: bool) -> None:
        """List-content records: tool-result bookkeeping per block, then
        ONE prompt decision per record (issue #215). A record counts
        once when any top-level block is human — a text block passing
        the #213 gate, or an image block. An image inside a tool_result
        is result payload, never a prompt. An interrupt marker still
        clears the latency anchor without counting the record. Injected
        text also anchors, but never counts or splits a context turn."""
        saw_human = False
        saw_text = False
        for blk in map(as_dict, blocks):
            btype = blk.get("type")
            if btype == "tool_result":
                self._note_tool_result(blk)
            elif btype == "text":
                text = blk.get("text", "") or ""
                if isinstance(text, str) and text.strip():
                    if text.lstrip().startswith(INTERRUPT_MARKER):
                        if mutate_anchor:
                            self.reply_timing.clear()
                    else:
                        saw_text = True
                        saw_human = saw_human or _is_prompt_text(text)
            elif btype == "image":
                saw_human = True
        if saw_human:
            self._count_prompt(line_num, ts_str, mutate_anchor)
        elif saw_text and mutate_anchor:
            self.reply_timing.anchor(_to_dt(ts_str))

    def _note_tool_result(self, blk) -> None:
        """Book one tool_result block: settle the call's error flag,
        result size and (for failures) the error text."""
        parse_tool_rows.note_tool_result(
            blk, self.tool_result_is_error, self.tool_result_text,
            self.tool_result_chars, self.collect.context_intake)

    def handle_assistant_line(self, obj: dict, msg: dict,
                              line_num: int) -> None:
        """Close reply latency, then usage-bearing metrics, Phase 1 merge and tool calls."""
        usage = msg.get("usage")
        # Terminate the reply-latency window FIRST, before any of the
        # early returns below. Canonical semantics count every
        # assistant-side content block as "the assistant has started
        # responding", including records this table has no row for.
        reply_latency_s = self.reply_timing.consume(obj)
        generation = self.generation_timing.observe(obj, msg) if self.generation_timing else None
        if not isinstance(usage, dict) or not usage:
            return
        usage = _flatten_usage(usage)
        # Skip synthetic stubs: Claude Code emits these after `/exit`
        # (text='No response requested.') and for interrupted partial
        # responses. They have all-zero usage and no requestId, but
        # they sit at the end of the file — without this filter, the
        # ctx_turns walk picks them as the trailing `last_usage`,
        # then the post-walk input>0 filter drops them, leaving
        # ctx_turns empty even when real records preceded them.
        if (msg.get("model") or "") == "<synthetic>":
            return

        text_chars, msg_tool_uses = _content_metrics(
            msg, str(obj.get("cwd", "") or ""), self.collect
        )
        # Reuse the request identity, timestamp and merge lookup across this line.
        req_id = obj.get("requestId", "") or ""
        merge_key = req_id or _merge_key(obj, msg)
        ts_str = obj.get("timestamp", "") or ""
        existing = self.seen_request.get(merge_key) if merge_key else None
        ev = {
            "line_num": line_num,
            "uuid": obj.get("uuid") or None,
            "request_id": req_id,
            "ts": ts_str,
            "model": msg.get("model") or None,  # no placeholder: parse_file refuses a None model (issue #688)
            "usage": dict(usage),
            "text_chars": text_chars,
            "reply_latency_s": reply_latency_s,
            "generation": generation,
            # Only a reply's CLOSING line carries stop_reason; NULL = never closed.
            "stop_reason": None,
            "effort": None,
            # The serving host (OpenRouter only); NULL on every other lane.
            "provider": _provider(msg),
        }
        if self.collect.request_metadata:
            ev["stop_reason"] = msg.get("stop_reason") or None
            ev["effort"] = obj.get("effort") or None
        if self.window is not None and existing is None:
            ev["turn_flags"], ev["turn_tool_results"], ev["cli_version"] = self.window.take(obj)  # live list: a later prompt_snapshot amends it in place (turn_flags.TurnWindow)
        if existing is not None:
            existing["usage"] = _merge_usage_max(existing["usage"], usage)
            if ev["stop_reason"] is not None:
                existing["stop_reason"] = ev["stop_reason"]
            if existing["provider"] is None:
                existing["provider"] = ev["provider"]
            # Same Phase 1 max-merge for text_chars: streaming responses
            # log incrementally; the largest sample is the final size.
            if text_chars > existing.get("text_chars", 0):
                existing["text_chars"] = text_chars
        else:
            if merge_key:
                self.seen_request[merge_key] = ev
            self.records_in_order.append(ev)

        # Attribute tool calls to their own line's model, not the merged record.
        self._record_tool_uses(
            msg_tool_uses, req_id, line_num, ts_str,
            ev["model"],
        )

    def _record_tool_uses(self, msg_tool_uses: list, req_id: str,
                          line_num: int, ts_str: str, model: str) -> None:
        # Dedupe on tool_use.id, including later blocks of the same request.
        # Idless blocks fall back to (req, line, idx).
        for tu in msg_tool_uses:
            tu_key = tu["tool_use_id"] or f"{req_id}:{line_num}:{tu['idx']}"
            if tu_key in self.seen_tool_ids:
                continue
            self.seen_tool_ids.add(tu_key)
            # Same ordered literal in small batches; avoid CPython's
            # per-field MAP_ADD path for dictionaries above 15 fields.
            self.tool_uses.append({
                "file_key": self.file_key,
                "line_num": line_num,
                "idx": tu["idx"],
                "ts": _to_dt(ts_str),
                "tool_name": tu["tool_name"],
                "model": model,
                "tool_use_id": tu["tool_use_id"],
                "command": tu["command"],  # popped after the line walk
                "is_error": None,  # filled after the line walk
                "error_kind": None,  # filled after the line walk
                "error_text": None,  # filled after the line walk
                "lines_added": tu["lines_added"],
                "lines_deleted": tu["lines_deleted"],
                "agent_type": tu["agent_type"],
            } | {
                "agent_model": tu["agent_model"],
                "dispatch_prompt_chars": tu["dispatch_prompt_chars"],
                "dispatch_brief_ref": tu["dispatch_brief_ref"],
                "dispatch_name": tu["dispatch_name"],
                "read_kind": tu["read_kind"],
                "read_targets": tu["read_targets"],
                "write_targets": tu["write_targets"],
                "result_chars": None,  # filled after the line walk
                "is_reread": None,     # filled after the line walk
            })


def _provider(msg: dict) -> str | None:
    """message.provider, the host an OpenRouter request was served by."""
    value = msg.get("provider")
    return (value.strip() or None) if isinstance(value, str) else None


def resolve_agent_type(walk: _LineWalk) -> str:
    """The agent type one transcript ran as. Never None.

    Two disjoint signals carry it, in precedence order:

    1. ``attributionAgent`` on assistant records — a dispatched agent.
       Present from Claude Code 2.1.126 onward. The mode is taken rather
       than the first value purely as a guard; no sampled file carried
       more than one distinct value.
    2. ``{"type": "agent-setting", "agentSetting": ...}`` — a session
       started with the CLI's ``--agent`` flag. Disjoint from (1) in
       every file sampled: a transcript carries one or the other.

    A file matching neither is attributed to DEFAULT_AGENT_TYPE. That
    bucket is genuinely mixed and cannot be split: the transcript records
    which role profile was selected, never who selected it, so a plain
    lead, a lead started with an explicit ``--agent`` flag, and a
    pre-2.1.126 subagent are indistinguishable here.

    The answer is per-FILE: a transcript is homogeneous — files carrying
    ``isSidechain`` records carry nothing else (verified across 88 such
    files, zero non-sidechain assistant records among them), so a
    top-level agent session and a lead never share one file.
    """
    if walk.attribution_agents:
        return canonical_agent_type(walk.attribution_agents.most_common(1)[0][0])
    return canonical_agent_type(walk.agent_setting) if walk.agent_setting else DEFAULT_AGENT_TYPE


def _parse_claude(file_key: str, blob: bytes, collection: CollectionPolicy = ALL_COLLECTIONS_ON) -> dict:
    """Parse Claude JSONL, max-merging requestIds and retaining idless records.
    Account-cap detection follows _LineWalk.handle_rate_limit."""
    walk = _LineWalk(file_key, collection)
    for line_num, raw in enumerate(iter_lines(blob), 1):
        if not raw:
            continue
        try:
            obj = loads(raw)
        except JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue

        kind = obj.get("type", "")
        walk.observe_metadata(obj, kind)
        if kind == "system" and obj.get("subtype") == "api_error":
            failed_attempt(walk.reply_timing, walk.generation_timing)
        if kind not in ("user", "assistant"):
            continue
        if walk.handle_rate_limit(obj, line_num):
            continue

        # Reuse the decoded type; role dispatch stays independent of collection.
        if kind == "user" and walk.generation_timing:
            walk.generation_timing.observe_user(obj, line_num)
        walk.handle_message_line(obj, as_dict(obj.get("message")), line_num)

    _resolve_tool_errors(walk.tool_uses, walk.tool_result_is_error,
                         walk.tool_result_text, walk.tool_result_chars,
                         error_text=walk.collect.error_text)
    if walk.collect.context_intake:
        resolve_rereads(walk.tool_uses)
    records = project_records(
        file_key, walk.records_in_order,
    )
    ctx_turns = _build_ctx_turns(records, walk.user_text_lines)

    return {
        "records": records,
        "ctx_turns": ctx_turns,
        "turn_count": len(ctx_turns) if walk.collect.file_extras else None,
        # Substantive user prompts (instrumentation and interrupt markers
        # excluded), whether or not a usage-bearing reply followed.
        "prompt_count": len(walk.user_text_lines) if walk.collect.file_extras else None,
        # Per-prompt ts, stored on files (issue #214); ingest-side only —
        # the browser parser has no range concept and needs no counterpart.
        "prompt_ts": walk.user_text_ts,
        # Every model that answered in this file, BEFORE ingest purges
        # suppressed ones: the only trace that a session changed lanes.
        "models": parse_collection.collected_models(records) if walk.collect.file_extras else None,
        "rate_limit_hits": walk.rate_limit_hits,
        "tool_uses": walk.tool_uses,
        "agent_type": resolve_agent_type(walk),
        "agent_type_in_band": bool(walk.attribution_agents
                                   or walk.agent_setting),
    }


def parse_file(file_key: str, blob: bytes, *, collection: CollectionPolicy = ALL_COLLECTIONS_ON) -> dict:
    """Parse one JSONL in any supported format, max-merging requestIds.

    sniff_format names the format: a Claude transcript takes this
    module's own path (_parse_claude); a Codex or Kimi one is parsed by
    codexmeter's ported parser and projected onto claudit's row shape by
    parse_lanes.to_claudit. Returns records, ctx_turns, turn_count,
    prompt_count, models, rate_limit_hits, tool_uses and agent_type
    either way, plus ``agent_type_in_band`` (whether the transcript named
    its role itself), which agent_sidecar.apply_agent_sidecar reads.
    """
    fmt = sniff_format(blob)
    parsed = (_parse_claude(file_key, blob, collection) if fmt == "claude" else
              to_claudit(LANE_PARSERS[fmt](file_key, blob, collection=collection),
                         fmt, collection=collection))
    if "cache_write_tokens" not in collection.disabled:
        resolve_cache_writes(parsed["records"])
    parse_lanes.refuse_unattributed(parsed, fmt, file_key)
    return parse_collection.mask_collected_fields(parsed, collection)
