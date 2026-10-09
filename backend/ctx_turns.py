"""Build context-growth turns from parser records and message boundaries."""
from __future__ import annotations

from backend.constants import MAX_PLAUSIBLE_CTX


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


def _build_ctx_turns(records: list, user_text_lines: list) -> list:
    """Build line-ordered ctx_turns, keeping pre-prompt usage as a leading turn.

    Each turn uses its last usage; zero or implausible context is dropped.
    """
    boundary_lines = sorted(user_text_lines)
    sorted_recs = sorted(records, key=lambda r: r["line_num"])

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
