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
from datetime import datetime, timezone
from io import BytesIO

from orjson import JSONDecodeError, loads

from backend import pricing
from backend.constants import (DEFAULT_AGENT_TYPE, INTERRUPT_MARKER,
                               MAX_PLAUSIBLE_CTX)
from backend.tool_errors import (ERROR_KIND_FAILED,  # pylint: disable=unused-import
                                 ERROR_KIND_REJECTED,  # pylint: disable=unused-import
                                 ERROR_KIND_TOOL_ERROR, ERROR_TEXT_MAX,
                                 _classify_error, _flatten_result_text,
                                 _pg_text, _result_size)
from backend.turn_flags import TurnWindow
from backend.bash_churn import BashCommand, bash_churn, churn_survives_error, replace_churn
from backend import bash_reads
from backend.parse_common import _dispatch_prompt_shape, iter_lines
from backend.parse_lanes import LANE_PARSERS, sniff_format, to_claudit
from backend.target_paths import target_key


_INSTRUMENTATION_USER_PREFIXES = (
    "<bash-input>",
    "<bash-stdout>",
    "<bash-stderr>",
    "<local-command-caveat>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<command-name>",
    "<command-message>",
    "<command-args>",
)
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


def _usage_ctx_input(u: dict) -> int:
    # Per-call context-window size (SV-PARSER-SPEC):
    # when the harness fans out multiple sub-calls (advisor()/retries),
    # they get rolled into one `usage` envelope as `iterations`. The
    # top-level fresh+create+read is the BILLING sum across iterations,
    # not the peak single-call window. For context-growth panels we
    # want the peak, so take max-of-iteration-totals when >1 iters.
    # Single-iter (or absent) → fall back to top-level sum.
    iters = u.get("iterations") or []
    if isinstance(iters, list) and len(iters) > 1:
        return max(
            (int(it.get("input_tokens", 0) or 0)
             + int(it.get("cache_creation_input_tokens", 0) or 0)
             + int(it.get("cache_read_input_tokens", 0) or 0))
            for it in iters
        )
    return (
        int(u.get("input_tokens", 0) or 0)
        + int(u.get("cache_creation_input_tokens", 0) or 0)
        + int(u.get("cache_read_input_tokens", 0) or 0)
    )


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
            nested = i.get(k)
            if not isinstance(nested, dict):
                continue
            for nk, nv in nested.items():
                if isinstance(nv, int):
                    merged[nk] = merged.get(nk, 0) + nv
        if merged:
            out[k] = merged
    return out


def _line_count(s: str) -> int:
    """Lines in a payload string, git-style: "a\nb\n" and "a\nb" are both
    2 lines; "" is 0."""
    if not s:
        return 0
    return s.count("\n") + (0 if s.endswith("\n") else 1)


def _tool_churn(name: str, tool_input: dict) -> tuple[int, int]:
    """(lines_added, lines_deleted) for one edit/write tool CALL.

    Derived from the call ARGUMENTS, not the tool result: the result's
    text is a bare confirmation ("The file ... has been updated
    successfully"), and while ~80% of results also carry a structured
    toolUseResult diff, 20% carry none — the call args are the one
    source present on every call (verified over the full corpus:
    27,045 Edit + 8,365 Write, all with old/new payloads). Two
    separate POSITIVE series per issue #10 — deletions are NOT
    encoded as negative additions.

    Edit, Write and Bash are the names handled (no MultiEdit /
    NotebookEdit rows exist in the corpus at all). Bash carries the
    bulk of it: under bypass permissions the model edits through the
    shell, so `backend.bash_churn` recovers what the command text
    enumerates directly — heredoc bodies written to a file, inline
    patch hunks, literal python replacements — and 0 for everything
    that would need the command to be RUN to know.
    Edit's old/new are diffed (`replace_churn`): repeated context is not churn.
    Known approximations: an Edit with replace_all=true is counted
    once (the call does not say how many occurrences exist), and a
    Write overwriting an existing file counts its old content as 0
    deletions (the call does not carry it).
    """
    if name == "Edit":
        return replace_churn(
            str(tool_input.get("old_string", "") or ""),
            str(tool_input.get("new_string", "") or ""),
        )
    if name == "Write":
        return (
            _line_count(str(tool_input.get("content", "") or "")),
            0,
        )
    if name == "Bash":
        return bash_churn(str(tool_input.get("command", "") or ""))
    return 0, 0


def _to_dt(s: str | None):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


# Tools whose arguments name a file they WRITE. Their targets invalidate
# a pending re-read: reading a file again after changing it is not a
# duplicate read, it is the only way to see the new bytes.
WRITE_TOOLS = ("Edit", "Write", "NotebookEdit")

# Tools that put a whole named file into the transcript by tool argument
# rather than by command text.
READ_TOOLS = ("Read",)


def _tool_access(name: str, tool_input: dict,
                 cwd: str) -> tuple[str | None, list[str], list[str]]:
    """(read_kind, read targets, write targets) for one tool CALL.

    Three shapes reach this. `Read` names its file outright. `Edit`,
    `Write` and `NotebookEdit` name a file they change. Everything else
    that reads goes through Bash, where the target has to be recovered
    from the command TEXT — which is most of the intake in practice:
    under bypass permissions the harness prefers shell commands, so
    `Read` sees a small minority of the files actually opened.

    A `Read` with an offset or limit is a SLICE, exactly as `sed -n`
    is: it put part of the file in context, not the file.
    """
    if name in WRITE_TOOLS:
        path = str(tool_input.get("file_path", "") or "")
        return None, [], [path] if path else []
    if name in READ_TOOLS:
        path = str(tool_input.get("file_path", "") or "")
        if not path:
            return None, [], []
        sliced = bool(tool_input.get("offset") or tool_input.get("limit"))
        return ("slice" if sliced else "whole"), [path], []
    if name == "Bash":
        return bash_reads.scan(str(tool_input.get("command", "") or ""), cwd)
    return None, [], []


def _content_metrics(msg: dict, cwd: str = "") -> tuple[int, list]:
    """(text_chars, tool_use blocks) for one assistant message.

    Visible-response size: sum character lengths of `text` blocks
    in the assistant message. Thinking tokens roll into output_tokens
    undifferentiated, so token-based response-size metrics conflate
    "size" with "how much the model thought". Character count of text
    content blocks is the clean measure of visible response size.
    Also extract every `tool_use` block — the `name` field is what
    the per-tool panels group by. Stored later as one
    row per tool call in the `tool_uses` table for the per-tool
    ratio panel.
    """
    msg_content = msg.get("content")
    text_chars = 0
    msg_tool_uses: list[dict] = []
    if isinstance(msg_content, list):
        for idx, blk in enumerate(msg_content):
            if not isinstance(blk, dict):
                continue
            btype = blk.get("type")
            if btype == "text":
                text_chars += len(str(blk.get("text", "")))
            elif btype in ("tool_use", "server_tool_use"):
                # server_tool_use = model invoked an Anthropic-hosted
                # tool (e.g. WebSearch). Same shape as tool_use; treat
                # both as tool calls in the panel.
                row = _tool_use_row(idx, blk, cwd)
                if row is not None:
                    msg_tool_uses.append(row)
    elif isinstance(msg_content, str):
        text_chars = len(msg_content)
    return text_chars, msg_tool_uses


def _tool_use_row(idx: int, blk: dict, cwd: str) -> dict | None:
    """One tool_use content block -> its pending tool_uses row.

    None for a nameless block. Split out of _content_metrics because
    every per-call column derived here is another local in a function
    whose job is only to walk the content list.
    """
    name = str(blk.get("name", "") or "")
    if not name:
        return None
    args = blk.get("input") or {}
    if name == "Bash":
        command = BashCommand(str(args.get("command", "") or ""))
        added, deleted = command.churn()
        r_kind, r_targets, w_targets = bash_reads.scan_command(command, cwd)
    else:
        added, deleted = _tool_churn(name, args)
        r_kind, r_targets, w_targets = _tool_access(name, args, cwd)
    a_type, a_model, p_chars, brief = _dispatch_args(name, args)
    return {
        "idx": idx,
        "tool_name": name,
        "tool_use_id": str(blk.get("id", "") or ""),
        "command": str(args.get("command", "") or "") if added else "",
        "lines_added": added,
        "lines_deleted": deleted,
        "agent_type": a_type,
        "agent_model": a_model,
        "dispatch_prompt_chars": p_chars,
        "dispatch_brief_ref": brief,
        "dispatch_name": _dispatch_name(name, args),
        "read_kind": r_kind,
        "read_targets": r_targets,
        "write_targets": w_targets,
    }


# Tool names that dispatch a subagent. Both spellings have shipped.
DISPATCH_TOOLS = ("Agent", "Task")


def _dispatch_args(name: str, args: dict) -> tuple:
    """(agent_type, agent_model, prompt_chars, brief_ref) for a
    subagent-dispatching CALL.

    Read off the call arguments, so a dispatch is attributable even
    when the subagent writes no JSONL of its own. `files.agent_type`
    answers "what ran"; this answers "what was ASKED for, on which
    model" -- the two differ exactly when a dispatch fails or the
    request omits the field. Non-dispatch tools get all-None.
    """
    if name not in DISPATCH_TOOLS or not isinstance(args, dict):
        return None, None, None, None
    a_type = args.get("subagent_type")
    a_model = args.get("model")
    chars, brief_ref = _dispatch_prompt_shape(args)
    return (
        str(a_type) if isinstance(a_type, str) and a_type else None,
        str(a_model) if isinstance(a_model, str) and a_model else None,
        chars,
        brief_ref,
    )


def _dispatch_name(name: str, args: dict) -> str | None:
    """The `name` a dispatching call gave its agent: a teammate's link to its role."""
    value = args.get("name") if name in DISPATCH_TOOLS and isinstance(args, dict) else None
    return value if isinstance(value, str) and value else None


class _LineWalk:
    """Mutable per-file state for the line-by-line pass."""

    def __init__(self, file_key: str) -> None:
        self.window = TurnWindow()
        self.file_key = file_key
        self.seen_request: dict[str, dict] = {}
        self.records_in_order: list[dict] = []
        self.user_text_lines: list[int] = []
        self.rate_limit_hits: list[dict] = []
        self.tool_uses: list[dict] = []
        self.seen_tool_ids: set[str] = set()
        # Per-file map of assistant tool_use.id -> bool(is_error).
        # Populated from later user tool_result blocks; consumed after
        # the line walk to fill tool_uses[*]["is_error"].
        self.tool_result_is_error: dict[str, bool] = {}
        # Same key -> leading text of a FAILED result, for error_kind
        # classification and the error_text drill-down column.
        self.tool_result_text: dict[str, str] = {}
        # Same key -> characters the result put into the transcript.
        # Recorded for every settled call, errored or not: the question
        # this answers is what filled the context window, and a failed
        # call's error text occupies it exactly like a success does.
        self.tool_result_chars: dict[str, int] = {}
        # Reply-latency anchor: last NON-instrumentation, NON-interrupt user
        # message timestamp. Cleared when consumed by an assistant message,
        # an interrupt marker, or superseded by a fresher user message.
        # Measured at message granularity (we treat the assistant message
        # line as the terminator since all assistant content blocks share
        # its ts).
        self.last_user_ts: datetime | None = None
        # Per-file first-seen line for each user-record uuid. A user record
        # whose uuid already appeared on an EARLIER, DIFFERENT line is a
        # replay and is invisible to reply-latency anchoring/superseding.
        self.seen_user_uuids: dict[str, int] = {}
        # Agent-type attribution, per FILE. See resolve_agent_type for the
        # precedence and for why the answer is per-file rather than
        # per-record.
        self.attribution_agents: Counter[str] = Counter()
        self.agent_setting: str | None = None

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
        if any(stripped.startswith(p) for p in _INSTRUMENTATION_USER_PREFIXES):
            return
        if stripped.startswith(INTERRUPT_MARKER):
            if mutate_anchor:
                self.last_user_ts = None
            return
        self.user_text_lines.append(line_num)
        ts_dt = _to_dt(ts_str)
        if ts_dt is not None and mutate_anchor:
            self.last_user_ts = ts_dt

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
        content_list = (obj.get("message") or {}).get("content") or []
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
        # reply-latency window (see _consume_anchor).
        self._consume_anchor(obj)
        return True

    def handle_user_line(self, obj: dict, msg: dict, line_num: int) -> None:
        """One user-role record: replay detection, then the content walk."""
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
            for blk in content:
                self._handle_user_block(blk, line_num, ts_str, mutate_anchor)

    def _handle_user_block(self, blk, line_num: int, ts_str: str,
                           mutate_anchor: bool) -> None:
        if not isinstance(blk, dict):
            return
        btype = blk.get("type")
        if btype == "tool_result":
            # Tool results live here. Each block carries
            # tool_use_id (referencing the assistant's
            # tool_use.id) and an optional is_error flag.
            tu_id = blk.get("tool_use_id")
            if not tu_id:
                return
            is_err = bool(blk.get("is_error", False))
            self.tool_result_is_error[str(tu_id)] = is_err
            self.tool_result_chars[str(tu_id)] = _result_size(
                blk.get("content")
            )
            if is_err:  # whole; _resolve_tool_errors truncates it
                self.tool_result_text[str(tu_id)] = _pg_text(
                    _flatten_result_text(blk.get("content")).strip()
                )
        elif btype == "text":
            text = blk.get("text", "") or ""
            if isinstance(text, str) and text.strip():
                self.handle_user_text(
                    text, line_num, ts_str, mutate_anchor=mutate_anchor
                )

    def handle_assistant_line(self, obj: dict, msg: dict,
                              line_num: int) -> None:
        """One assistant-role record: close the reply-latency window,
        then (for usage-bearing records only) metrics, Phase 1 merge,
        tool calls."""
        usage = msg.get("usage")
        # Terminate the reply-latency window FIRST, before any of the
        # early returns below. Canonical semantics count every
        # assistant-side content block as "the assistant has started
        # responding", including records this table has no row for.
        reply_latency_s = self._consume_anchor(obj)
        if not usage:
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
            msg, str(obj.get("cwd", "") or "")
        )
        req_id = obj.get("requestId", "") or ""
        merge_key = _merge_key(obj, msg)
        ev = {
            "line_num": line_num,
            "uuid": obj.get("uuid") or None,
            "request_id": req_id,
            "ts": obj.get("timestamp", "") or "",
            "model": msg.get("model") or "(unknown)",
            "usage": dict(usage),
            "text_chars": text_chars,
            "reply_latency_s": reply_latency_s,
            # Only a reply's CLOSING line carries stop_reason; NULL = never closed.
            "stop_reason": msg.get("stop_reason") or None,
            "effort": obj.get("effort") or None,
            # The serving host (OpenRouter only); NULL on every other lane.
            "provider": _provider(msg),
        }
        if not (merge_key and merge_key in self.seen_request):
            ev["turn_flags"], ev["turn_tool_results"], ev["cli_version"] = self.window.take(obj)  # live list: a later prompt_snapshot amends it in place (turn_flags.TurnWindow)
        if merge_key and merge_key in self.seen_request:
            existing = self.seen_request[merge_key]
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

        # The call's model is its OWN line's -- the value records.model
        # gets for that line -- never a join to the merged record, which
        # usually sits on an earlier line of the same requestId (or,
        # with no requestId, the same message.id).
        self._record_tool_uses(
            msg_tool_uses, req_id, line_num, obj.get("timestamp", "") or "",
            ev["model"],
        )

    def _consume_anchor(self, obj: dict) -> float | None:
        """Reply latency: gap from last anchored user-message ts to
        this assistant message's ts. NULL when there's no preceding
        anchored user message (session start, or every recent user
        msg was instrumentation/interrupt) OR when delta is
        negative (negative deltas are
        session-restore / compaction replay artifacts where the
        assistant message came from a prior, re-emitted state and
        isn't actually a reply to the visually-preceding user msg).
        Clamping to 0 would pollute the p0/p50 distribution; drop
        the measurement instead.

        Called for EVERY assistant-role record, including the ones
        that never become a `records` row (`<synthetic>` API-error
        stubs, rate-limit records, records with no usage). Those are
        still the assistant responding, so they must close the window
        — otherwise the anchor survives a failed turn and the next
        real reply, possibly hours later, is charged the whole gap.
        Their own (tiny) measurement has no row to live on and is
        discarded; canonical --reply-latency does report it."""
        reply_latency_s = None
        if self.last_user_ts is not None:
            assistant_dt = _to_dt(obj.get("timestamp", "") or "")
            if assistant_dt is not None:
                delta_s = (assistant_dt - self.last_user_ts).total_seconds()
                if delta_s >= 0:
                    reply_latency_s = delta_s
        self.last_user_ts = None  # anchor consumed by this assistant reply
        return reply_latency_s

    def _record_tool_uses(self, msg_tool_uses: list, req_id: str,
                          line_num: int, ts_str: str, model: str) -> None:
        # Dedupe on tool_use.id (globally unique), never on first line of a
        # request: Claude Code writes one line per content block, so 57-71%
        # of tool_use blocks sit on a LATER line of the same requestId.
        # Idless blocks fall back to (req, line, idx).
        for tu in msg_tool_uses:
            tu_key = tu["tool_use_id"] or f"{req_id}:{line_num}:{tu['idx']}"
            if tu_key in self.seen_tool_ids:
                continue
            self.seen_tool_ids.add(tu_key)
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


def _resolve_tool_errors(tool_uses: list, tool_result_is_error: dict,
                         tool_result_text: dict | None = None,
                         tool_result_chars: dict | None = None) -> None:
    """Resolve tool_result.is_error onto each tool_uses entry by
    tool_use_id. Unmatched entries keep is_error=None and are
    excluded from rate denominators at query time. An errored call
    changed nothing on disk, so its churn is zeroed (an unmatched
    call keeps its churn: the file may simply end before the result
    record) — unless `bash_churn.churn_survives_error` says otherwise."""
    for tu in tool_uses:
        # Kept on the row: ingest dedups compaction-sidecar replays on it.
        tu["tool_use_id"] = tu_id = tu.get("tool_use_id") or None
        command = tu.pop("command", "")
        if tu_id:
            tu["result_chars"] = (tool_result_chars or {}).get(tu_id)
        if tu_id and tu_id in tool_result_is_error:
            tu["is_error"] = tool_result_is_error[tu_id]
            if tu["is_error"]:
                text = (tool_result_text or {}).get(tu_id, "")
                tu["error_kind"] = _classify_error(text)
                tu["error_text"] = text[:ERROR_TEXT_MAX] or None
                ran = tu["error_kind"] == ERROR_KIND_TOOL_ERROR
                if not (ran and churn_survives_error(command, text)):
                    tu["lines_added"], tu["lines_deleted"] = 0, 0
                if not ran:
                    tu["write_targets"] = []  # wrote nothing


def _resolve_rereads(tool_uses: list) -> None:
    """Flag each whole-file read that added nothing new to the context.

    Walked in line order over ONE jsonl, which is the right scope: the
    context this measures is a session's, and a file boundary is where
    that context restarts.

    A read is a re-read when EVERY file it names was already read whole
    in this file and none of them has been written since. Three things
    that look like waste and are not, all excluded here:

    - a SLICE. `sed -n '1,200p' f` then `sed -n '200,400p' f` read
      different halves; only a whole read can be wholly redundant.
    - a read after a WRITE. The bytes changed, so re-reading them is
      the only way to see the new ones.
    - an ERRORED read. It returned a failure, not the file, so it
      neither wasted context nor counts as having seen the file —
      which is why it does not mark its targets either.

    Partial overlap (`cat a b` where only `a` was read before) is NOT
    flagged: something new arrived, so the call was not wasted. The
    flag stays deliberately conservative — it is easier to argue up
    from a floor than to defend a number that counted useful reads.
    """
    seen_whole: set[str] = set()
    for tu in tool_uses:
        targets = [target_key(path) for path in tu.get("read_targets") or []]
        if tu.get("read_kind") == "whole" and targets and not tu["is_error"]:
            tu["is_reread"] = all(path in seen_whole for path in targets)
            seen_whole.update(targets)
        for path in tu.get("write_targets") or []:
            seen_whole.discard(target_key(path))


def _provider(msg: dict) -> str | None:
    """message.provider, the host an OpenRouter request was served by."""
    value = msg.get("provider")
    return (value.strip() or None) if isinstance(value, str) else None


def _project_record(file_key: str, ev: dict) -> dict:
    """One walked event → its records-table row (token columns + cost)."""
    u = ev["usage"]
    fresh = int(u.get("input_tokens", 0) or 0)
    create = int(u.get("cache_creation_input_tokens", 0) or 0)
    read = int(u.get("cache_read_input_tokens", 0) or 0)
    output = int(u.get("output_tokens", 0) or 0)
    eph = u.get("cache_creation") or {}
    eph5 = int(eph.get("ephemeral_5m_input_tokens", 0) or 0)
    eph1h = int(eph.get("ephemeral_1h_input_tokens", 0) or 0)
    unsplit = max(0, create - eph5 - eph1h)
    details = u.get("output_tokens_details") or {}
    thinking = int(details.get("thinking_tokens", 0) or 0) if isinstance(details, dict) else 0
    ts = _to_dt(ev["ts"])
    cost = pricing.compute_cost(
        ev["model"],
        fresh=fresh, output=output,
        eph5=eph5, eph1h=eph1h,
        unsplit_create=unsplit, read=read,
        # Dated rates apply to when the tokens were spent, not to
        # when this file happens to be parsed.
        ts=ts,
        provider=ev.get("provider"),
    )
    return {
        "file_key": file_key,
        "line_num": ev["line_num"],
        "uuid": ev["uuid"],
        "request_id": ev["request_id"],
        "ts": ts,
        "model": ev["model"],
        "provider": ev.get("provider"),
        "fresh_tokens": fresh,
        "cache_creation_tokens": create,
        "cache_read_tokens": read,
        "output_tokens": output,
        "text_chars": int(ev.get("text_chars", 0)),
        "reply_latency_s": ev.get("reply_latency_s"),
        "stop_reason": ev.get("stop_reason"),
        "effort": ev.get("effort"),
        "thinking_tokens": thinking,
        "cli_version": ev.get("cli_version"),
        "turn_flags": ev.get("turn_flags") or [],
        "turn_tool_results": int(ev.get("turn_tool_results") or 0),
        "eph5_tokens": eph5,
        "eph1h_tokens": eph1h,
        "cost_usd": round(cost, 6),
        "ctx_input": _usage_ctx_input(u),
    }


def _build_ctx_turns(records: list, user_text_lines: list) -> list:
    """Build ctx_turns by user-text boundary (SV-PARSER-SPEC)."""
    boundary_lines = sorted(user_text_lines)
    aware_min = datetime.min.replace(tzinfo=timezone.utc)
    sorted_recs = sorted(
        records,
        key=lambda r: (
            r["ts"] is not None,
            r["ts"] if r["ts"] is not None else aware_min,
            r["line_num"],
        ),
    )

    turn_records: list[dict] = []
    last_usage: dict | None = None
    bi = 0
    for rec in sorted_recs:
        while bi < len(boundary_lines) and boundary_lines[bi] <= rec["line_num"]:
            if last_usage is not None:
                turn_records.append(last_usage)
                last_usage = None
            bi += 1
        last_usage = rec
    if last_usage is not None:
        turn_records.append(last_usage)

    # Drop turns with 0 input (refusals/interrupts; they corrupt deltas)
    # and turns above any real context window (cumulative counters
    # written by other harnesses; they destroy the trace's y-axis).
    turn_records = [
        t for t in turn_records
        if 0 < t["ctx_input"] <= MAX_PLAUSIBLE_CTX
    ]

    ctx_turns: list[dict] = []
    prev_input = 0
    for idx, t in enumerate(turn_records, 1):
        ctx_input = t["ctx_input"]
        ctx_turns.append({
            "idx": idx,
            "ts": t["ts"].isoformat() if t["ts"] else "",
            "line": t["line_num"],
            "input": ctx_input,
            "output": t["output_tokens"],
            "delta": ctx_input - prev_input,
        })
        prev_input = ctx_input
    return ctx_turns


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

    The answer is per-FILE because a transcript is homogeneous: files
    carrying ``isSidechain`` records carry nothing else (verified across
    88 such files — zero non-sidechain assistant records among them), so
    a top-level agent session and a lead never share one file.
    """
    if walk.attribution_agents:
        return walk.attribution_agents.most_common(1)[0][0]
    return walk.agent_setting or DEFAULT_AGENT_TYPE


def _parse_claude(file_key: str, blob: bytes) -> dict:
    """Parse one Claude JSONL, max-merging requestIds and retaining idless records.

    Returns records, ctx_turns, turn_count, prompt_count, rate_limit_hits,
    tool_uses and agent_type. Account-cap detection follows
    _LineWalk.handle_rate_limit; the browser parser has a separate detector.
    """
    walk = _LineWalk(file_key)
    for line_num, raw in enumerate(iter_lines(blob), 1):
        if not raw:
            continue
        try:
            obj = loads(raw)
        except JSONDecodeError:
            continue

        kind = obj.get("type", "")
        walk.window.observe(obj)
        if kind == "agent-setting":
            name = obj.get("agentSetting")
            if isinstance(name, str) and name:
                walk.agent_setting = name
            continue
        attribution = obj.get("attributionAgent")
        if isinstance(attribution, str) and attribution:
            walk.attribution_agents[attribution] += 1
        if kind not in ("user", "assistant"):
            continue
        if walk.handle_rate_limit(obj, line_num):
            continue

        msg = obj.get("message") or {}
        role = msg.get("role")
        if role == "user":
            walk.handle_user_line(obj, msg, line_num)
        elif role == "assistant":
            walk.handle_assistant_line(obj, msg, line_num)

    _resolve_tool_errors(walk.tool_uses, walk.tool_result_is_error,
                         walk.tool_result_text, walk.tool_result_chars)
    _resolve_rereads(walk.tool_uses)
    records = [
        _project_record(file_key, ev) for ev in walk.records_in_order
    ]
    ctx_turns = _build_ctx_turns(records, walk.user_text_lines)

    return {
        "records": records,
        "ctx_turns": ctx_turns,
        "turn_count": len(ctx_turns),
        # Substantive user prompts (instrumentation and interrupt markers
        # excluded), whether or not a usage-bearing reply followed.
        "prompt_count": len(walk.user_text_lines),
        # Every model that answered in this file, BEFORE ingest purges
        # suppressed ones: the only trace that a session changed lanes.
        "models": sorted({r["model"] for r in records}),
        "rate_limit_hits": walk.rate_limit_hits,
        "tool_uses": walk.tool_uses,
        "agent_type": resolve_agent_type(walk),
        "agent_type_in_band": bool(walk.attribution_agents
                                   or walk.agent_setting),
    }


def parse_file(file_key: str, blob: bytes) -> dict:
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
    if fmt == "claude":
        return _parse_claude(file_key, blob)
    return to_claudit(LANE_PARSERS[fmt](file_key, blob), fmt)
