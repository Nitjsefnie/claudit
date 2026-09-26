"""Parse machinery shared by every transcript format.

The per-file mutable state, the turn bookkeeping, the record/tool-call
builders and the ctx_turns derivation are format-independent: a Kimi
StatusUpdate, a kimi-code usage.record and a Codex token_count all produce
the same billing row, and all three formats bracket their requests into
turns the same way. Only the field NAMES differ, and that difference is
what each format module owns.

Splitting these out is what keeps a format module readable, and what lets
backend/parse_codex.py exist without importing backend/parse.py — the
cycle that a shared-helpers-live-with-the-Kimi-parser layout would create.

Nothing here knows a format. A function that has to ask which format it is
looking at belongs in that format's module instead.
"""
from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime

from backend import pricing
from backend.tool_errors import (ERROR_TEXT_MAX, _pg_text,
                                 classify_lane_error)


def iter_lines(blob: bytes) -> Iterator[bytes]:
    """The file's lines, one at a time, with splitlines' CR / LF / CRLF
    semantics.

    Both callers used to stream LF files through BytesIO and fall back
    to blob.splitlines() for anything carrying a CR -- an eager copy of
    every line of the file into one list. A Windows checkout writes CRLF
    fixtures, so that fallback was the common case there. This yields
    the same lines without the list: CRLF is one separator, a lone CR is
    a separator, and the trailing fragment yields only when non-empty.
    """
    start = 0
    while start < len(blob):
        nl = blob.find(b"\n", start)
        cr = blob.find(b"\r", start)
        if cr != -1 and (nl == -1 or cr < nl):
            if cr == nl - 1:  # CRLF: one separator, not two
                yield blob[start:cr]
                start = nl + 1
            else:  # lone CR, or CR with a later LF
                yield blob[start:cr]
                start = cr + 1
        elif nl != -1:
            yield blob[start:nl]
            start = nl + 1
        else:
            yield blob[start:]
            return


def _to_dt(s: str | float | None):
    if not s:
        return None
    if isinstance(s, (int, float)):
        return datetime.fromtimestamp(s, tz=datetime.now().astimezone().tzinfo)
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


@dataclass
class _ParseState:
    """Mutable per-file parse state threaded through the message handlers.

    turns entries are {begin_line, begin_ts, end_line, end_ts,
    status_lines: [line_num]}; records entries carry the keys documented
    on parse_file plus an internal ctx_input used to build ctx_turns.
    """
    file_key: str
    records: list[dict] = field(default_factory=list)
    tool_uses: list[dict] = field(default_factory=list)
    rate_limit_hits: list[dict] = field(default_factory=list)
    # Per-file map of tool_call_id -> bool(is_error)
    tool_result_is_error: dict[str, bool] = field(default_factory=dict)
    # Per-file map of tool_call_id -> (error_kind, error_text) for the
    # calls that errored, recorded when the result settles (SV-WHY-COLUMNS).
    tool_result_error_info: dict[str, tuple[str, str | None]] = field(
        default_factory=dict)
    turns: list[dict] = field(default_factory=list)
    current_turn: dict | None = None
    current_turn_id: str | None = None
    # Reply latency: the armed anchor's ts (the turn's start), and the ts
    # of the first assistant event seen while it was armed -- where the
    # window ends, matching the Claude path's first assistant line.
    pending_turn_begin_ts: datetime | None = None
    first_assistant_event_ts: datetime | None = None
    # For text_chars: accumulate ContentPart.text since last TurnBegin
    text_chars_since_turn: int = 0
    # First event timestamp drives the per-session model label.
    first_event_ts: datetime | None = None
    # The role this transcript ran as, in the format's OWN vocabulary
    # (parse_lanes.to_claudit normalises it). None when no line named one.
    agent_role: str | None = None


def _close_turn(st: _ParseState, line_num: int, ts: datetime | None) -> None:
    if st.current_turn is not None:
        st.current_turn["end_line"] = line_num
        st.current_turn["end_ts"] = ts
        st.turns.append(st.current_turn)
        st.current_turn = None


def _start_turn(st: _ParseState, line_num: int, ts: datetime | None) -> None:
    st.current_turn = {
        "begin_line": line_num,
        "begin_ts": ts,
        "end_line": None,
        "end_ts": None,
        "status_lines": [],
    }
    st.text_chars_since_turn = 0


def _turn_boundary(st: _ParseState, line_num: int, ts: datetime | None) -> None:
    """Close any open turn, open the next, and arm the reply-latency anchor."""
    _close_turn(st, line_num, ts)
    _start_turn(st, line_num, ts)
    _arm_reply_anchor(st, ts)


def _arm_reply_anchor(st: _ParseState, ts: datetime | None) -> None:
    """Open a reply-latency window at `ts` (None disarms it)."""
    st.pending_turn_begin_ts = ts
    st.first_assistant_event_ts = None


def _end_turn(st: _ParseState, line_num: int, ts: datetime | None) -> None:
    """Close an open turn at an explicit end marker, disarming the
    reply-latency anchor: a turn that produced no billing record never
    gets one attributed to the next turn's first request."""
    if st.current_turn is not None:
        _close_turn(st, line_num, ts)
        _arm_reply_anchor(st, None)


def _mark_assistant_event(st: _ParseState, ts: datetime | None) -> None:
    """Note model output at `ts`: the first stamped one while the anchor
    is armed is where the turn's reply latency ends."""
    if (st.pending_turn_begin_ts is not None
            and st.first_assistant_event_ts is None):
        st.first_assistant_event_ts = ts


def _consume_reply_latency(st: _ParseState, ts: datetime | None) -> float | None:
    """Gap from the turn's anchor to its first assistant output, if the
    anchor is open.

    The window ends at the first assistant event recorded since the anchor
    was armed -- the same end the Claude path uses, its first assistant
    line -- and at this record's own ts only when the turn produced no
    assistant event before it. A turn's first billing record can land far
    later than its first output (a cumulative counter that stays flat
    while the model works), so ending there measured the whole response.

    The anchor is consumed either way: one reply latency per turn,
    attributed to its first billing record. A negative gap is dropped.
    """
    end_ts = st.first_assistant_event_ts or ts
    latency = None
    if st.pending_turn_begin_ts is not None and end_ts is not None:
        delta_s = (end_ts - st.pending_turn_begin_ts).total_seconds()
        if delta_s >= 0:
            latency = delta_s
    _arm_reply_anchor(st, None)
    return latency


def _line_count(text: object) -> int:
    """Lines in an edit-payload string. A trailing newline terminates the
    last line rather than starting another, so "a\n" is 1 line; a final
    partial line still counts, so "a\nb" is 2."""
    if not isinstance(text, str) or not text:
        return 0
    n = text.count("\n")
    return n if text.endswith("\n") else n + 1


def _append_tool_use(st: _ParseState, line_num: int, ts: datetime | None,
                     tool_name: str, tool_call_id: str,
                     churn: tuple[int, int] = (0, 0),
                     model: str = "unknown",
                     dispatch: tuple | None = None) -> None:
    """Append one tool call row.

    `dispatch` is the (agent_type, agent_model, prompt_chars, brief_ref)
    a subagent-dispatching call asked for; None for any other call, whose
    row then carries no such keys and parse_lanes.to_claudit NULLs them.
    """
    added, deleted = churn
    row = {
        "file_key": st.file_key,
        "line_num": line_num,
        "idx": len(st.tool_uses),
        "ts": ts,
        "tool_name": tool_name,
        "model": model,
        "tool_call_id": tool_call_id,
        "is_error": None,
        "lines_added": added,
        "lines_deleted": deleted,
    }
    if dispatch is not None:
        (row["agent_type"], row["agent_model"],
         row["dispatch_prompt_chars"], row["dispatch_brief_ref"]) = dispatch
    st.tool_uses.append(row)


def _nonempty_str(value: object) -> str | None:
    """`value` when it is a non-empty string, else None."""
    return value if isinstance(value, str) and value else None


def _note_agent_role(st: _ParseState, role: object) -> None:
    """Record the role a transcript declares, if it names one.

    The FIRST non-empty value wins: a file can declare its role on more
    than one line, and a line naming none must not shadow one that does.
    """
    if st.agent_role is None:
        st.agent_role = _nonempty_str(role)


# How far into a dispatch prompt to look for a brief reference. A prompt
# that delegates to a written brief says so in its opening directive
# ("Read <path> IN FULL and execute it exactly"); one that mentions a
# path incidentally does so further down, after the instructions it
# actually carries.
BRIEF_REF_SCAN = 400

# An absolute POSIX or Windows path to a Markdown file. Markdown because
# that is what a brief is written as; a path to a source file being
# edited is not a brief and must not count as one.
BRIEF_REF_RE = re.compile(r"(?:/|[A-Za-z]:\\)[^\s`'\"]+\.md\b")


def _dispatch_prompt_shape(args: dict) -> tuple:
    """(prompt_chars, brief_ref) for a dispatching call's prompt.

    Two questions about HOW a dispatch was briefed, neither of which
    needs the prompt text itself kept:

    `prompt_chars` -- how much brief was written into this call.
    `brief_ref`    -- whether the opening directive points at a written
                      brief file instead of carrying the brief inline.

    Together they separate a dispatch that reuses a brief someone
    committed from one that re-authors the same instructions from
    scratch, which is the difference between a brief that survives the
    session and one that dies with its scratch directory.

    The prompt text is deliberately NOT stored: it is unbounded, it is
    the most sensitive thing in a transcript, and neither question
    needs it.

    Lives here rather than in parse.py so the lane parsers, which
    parse.py imports, can share it without an import cycle.
    """
    prompt = args.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        return None, None
    head = prompt[:BRIEF_REF_SCAN]
    return len(prompt), BRIEF_REF_RE.search(head) is not None


def _append_usage_record(st: _ParseState, line_num: int,
                         ts: datetime | None, uuid: str | None,
                         model: str, toks: tuple[int, int, int, int],
                         *, reasoning: int = 0,
                         long_context: bool = False) -> None:
    """Build one billing record from its token counts and append it.

    `toks` is the BILLED PARTITION of the request — (fresh, create, read,
    output) sum to everything charged, exactly once each. Every provider
    lands here, whatever its own field names.

    `reasoning` is different in kind: a SUBSET of `output`, reported by
    formats that break the response down and 0 for those that do not. It is
    carried rather than dropped because token types are not required to
    match across providers — a format that counts something gets it stored,
    and a format that does not gets a truthful zero instead of an invented
    value. It is never added to the cost: `output` already includes it.

    `model` is an ALREADY-RESOLVED canonical pricing label, because how a
    model is attributed is the one part of a billing row that is entirely
    format-specific: the Kimi formats run a wire-id-then-date ladder, Codex
    reads the surrounding turn_context. Resolving it here would drag one
    format's rules into every other format's records.

    `long_context` bills the whole record on the long-context meter. Only
    Codex has such a tier; the Kimi formats never pass it.
    """
    fresh, create, read, output = toks
    latency = _consume_reply_latency(st, ts)
    # claudit's compute_cost prices a cache write through its TTL split;
    # every lane bills create at ONE rate, which claudit stores as both
    # create_5m and create_1h (Kimi writes at 0, Codex at 1.25x input), so
    # the whole write rides unsplit_create and the split columns stay 0.
    cost = pricing.compute_cost(
        model,
        fresh=fresh, output=output,
        eph5=0, eph1h=0, unsplit_create=create,
        read=read, long_context=long_context,
        # The record's OWN timestamp, so a rate that changed mid-corpus
        # bills each request at what it cost when it ran. An unstamped
        # record falls back to current list price, which is the only
        # answer available for one.
        ts=ts,
    )
    st.records.append({
        "file_key": st.file_key,
        "line_num": line_num,
        "uuid": uuid,
        "ts": ts,
        "model": model,
        "fresh_tokens": fresh,
        "cache_creation_tokens": create,
        "cache_read_tokens": read,
        "output_tokens": output,
        "reasoning_output_tokens": reasoning,
        # Persisted on records so a re-derived cost breakdown can apply
        # the meter exactly as compute_cost did (SV-DATED-RATES).
        "long_context": long_context,
        "cost_usd": round(cost, 6),
        "text_chars": st.text_chars_since_turn,
        "reply_latency_s": latency,
        "ctx_input": fresh + create + read,
    })
    if st.current_turn is not None:
        st.current_turn["status_lines"].append(line_num)


def _settle_lane_tool_result(st: _ParseState, tc_id: object,
                             is_error: bool, text: str) -> None:
    """Record one lane tool_result's outcome against its call id.

    `text` is the readable failure text (empty for a successful or
    textless result); for an errored result it is classified into the
    harness-generic error_kind and its leading ERROR_TEXT_MAX characters
    kept for drill-down, exactly as the Claude path stores them.
    """
    st.tool_result_is_error[str(tc_id)] = bool(is_error)
    if not is_error:
        return
    st.tool_result_error_info[str(tc_id)] = (
        classify_lane_error(text),
        # A textless failure carries a kind but no text: NULL keeps the
        # column honest for the GROUP BY drill-down.
        _pg_text(text)[:ERROR_TEXT_MAX] or None,
    )


def _resolve_tool_errors(tool_uses: list[dict],
                         tool_result_is_error: dict[str, bool],
                         error_info: dict[str, tuple[str, str | None]] | None
                         = None) -> None:
    """Resolve tool_result.is_error onto each tool_uses entry, and zero the
    line churn of calls that failed — a rejected edit changed no lines.

    A settled failure also carries its (error_kind, error_text) pair from
    `error_info`, so lane failures count in the error-kind breakdowns and
    not only in the is_error rate."""
    for tu in tool_uses:
        # The id stays on the row: parse_lanes.to_claudit renames it to
        # claudit's tool_use_id, and ingest keys is_canonical on it.
        tc_id = str(tu.get("tool_call_id") or "")
        if tc_id and tc_id in tool_result_is_error:
            tu["is_error"] = tool_result_is_error[tc_id]
            if tu["is_error"]:
                tu["lines_added"] = 0
                tu["lines_deleted"] = 0
                if error_info and tc_id in error_info:
                    tu["error_kind"], tu["error_text"] = error_info[tc_id]


def _ctx_turns_from_turns(turns: list[dict], records: list[dict]) -> list[dict]:
    """Build ctx_turns from turns + records.

    The last StatusUpdate/usage.record in each turn is the canonical one.
    """
    rec_by_line = {r["line_num"]: r for r in records}
    ctx_turns: list[dict] = []
    prev_input = 0
    turn_idx = 0
    for turn in turns:
        if not turn["status_lines"]:
            continue
        last_line = turn["status_lines"][-1]
        rec = rec_by_line.get(last_line)
        if not rec or rec["ctx_input"] <= 0:
            continue
        turn_idx += 1
        ctx_input = rec["ctx_input"]
        ctx_turns.append({
            "idx": turn_idx,
            "ts": rec["ts"].isoformat() if rec["ts"] else "",
            "line": last_line,
            "input": ctx_input,
            "output": rec["output_tokens"],
            "delta": ctx_input - prev_input,
        })
        prev_input = ctx_input
    return ctx_turns


def _finish_parse(st: _ParseState, end_line: int | None = None) -> dict:
    """Shared tail for both formats: close any dangling turn, settle the
    tool-call is_error flags, and build ctx_turns from the turns."""
    if end_line is not None:
        _close_turn(st, end_line, None)
    elif st.current_turn is not None:
        st.turns.append(st.current_turn)
    _resolve_tool_errors(st.tool_uses, st.tool_result_is_error,
                         st.tool_result_error_info)
    ctx_turns = _ctx_turns_from_turns(st.turns, st.records)
    return {
        "records": st.records,
        "ctx_turns": ctx_turns,
        "turn_count": len(ctx_turns),
        # User turns recorded, whether or not a usage-bearing reply
        # followed — claudit's files.prompt_count for a lane transcript.
        "prompt_count": len(st.turns),
        # Each one's own begin timestamp, index-aligned with prompt_count
        # — stored on files so /api/dashboard counts a range's prompts
        # per own timestamp (issue #214). Ingest-side only: the browser
        # parser has no range concept and needs no counterpart.
        "prompt_ts": [
            t["begin_ts"].isoformat() if t["begin_ts"] else None
            for t in st.turns
        ],
        "rate_limit_hits": st.rate_limit_hits,
        "tool_uses": st.tool_uses,
        "agent_role": st.agent_role,
    }
