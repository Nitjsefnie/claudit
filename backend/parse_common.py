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

from dataclasses import dataclass, field
from datetime import datetime

from backend import pricing


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
    turns: list[dict] = field(default_factory=list)
    current_turn: dict | None = None
    current_turn_id: str | None = None
    # For reply latency: track TurnBegin ts, then find first assistant event
    pending_turn_begin_ts: datetime | None = None
    turn_has_assistant_event: bool = False
    # For text_chars: accumulate ContentPart.text since last TurnBegin
    text_chars_since_turn: int = 0
    # First event timestamp drives the per-session model label.
    first_event_ts: datetime | None = None


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
    st.turn_has_assistant_event = False
    st.text_chars_since_turn = 0


def _turn_boundary(st: _ParseState, line_num: int, ts: datetime | None) -> None:
    """Close any open turn, open the next, and arm the reply-latency anchor."""
    _close_turn(st, line_num, ts)
    _start_turn(st, line_num, ts)
    st.pending_turn_begin_ts = ts


def _end_turn(st: _ParseState, line_num: int, ts: datetime | None) -> None:
    """Close an open turn at an explicit end marker, disarming the
    reply-latency anchor: a turn that produced no billing record never
    gets one attributed to the next turn's first request."""
    if st.current_turn is not None:
        _close_turn(st, line_num, ts)
        st.pending_turn_begin_ts = None


def _mark_assistant_event(st: _ParseState) -> None:
    if not st.turn_has_assistant_event and st.pending_turn_begin_ts is not None:
        st.turn_has_assistant_event = True


def _consume_reply_latency(st: _ParseState, ts: datetime | None) -> float | None:
    """Gap from the turn's anchor to this record, if the anchor is open.

    The anchor is consumed either way: one reply latency per turn, taken
    from its first billing record.
    """
    latency = None
    if st.pending_turn_begin_ts is not None and ts is not None:
        delta_s = (ts - st.pending_turn_begin_ts).total_seconds()
        if delta_s >= 0:
            latency = delta_s
    st.pending_turn_begin_ts = None
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
                     model: str = "unknown") -> None:
    added, deleted = churn
    st.tool_uses.append({
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
    })


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


def _resolve_tool_errors(tool_uses: list[dict],
                         tool_result_is_error: dict[str, bool]) -> None:
    """Resolve tool_result.is_error onto each tool_uses entry, and zero the
    line churn of calls that failed — a rejected edit changed no lines."""
    for tu in tool_uses:
        # The id stays on the row: parse_lanes.to_claudit renames it to
        # claudit's tool_use_id, and ingest keys is_canonical on it.
        tc_id = str(tu.get("tool_call_id") or "")
        if tc_id and tc_id in tool_result_is_error:
            tu["is_error"] = tool_result_is_error[tc_id]
            if tu["is_error"]:
                tu["lines_added"] = 0
                tu["lines_deleted"] = 0


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
    _resolve_tool_errors(st.tool_uses, st.tool_result_is_error)
    ctx_turns = _ctx_turns_from_turns(st.turns, st.records)
    return {
        "records": st.records,
        "ctx_turns": ctx_turns,
        "turn_count": len(ctx_turns),
        # User turns recorded, whether or not a usage-bearing reply
        # followed — claudit's files.prompt_count for a lane transcript.
        "prompt_count": len(st.turns),
        "rate_limit_hits": st.rate_limit_hits,
        "tool_uses": st.tool_uses,
    }
