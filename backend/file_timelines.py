"""Convert parser context turns to typed columns and restore the wire shape."""
from __future__ import annotations

from datetime import datetime, timezone


def ctx_columns(turns: list[dict]) -> dict[str, list]:
    """Reject traces whose omitted idx/delta cannot be reconstructed exactly."""
    columns: dict[str, list] = {key: [] for key in
                                ("ctx_ts", "ctx_line", "ctx_input", "ctx_output")}
    previous = 0
    for idx, turn in enumerate(turns, 1):
        value = turn["input"]
        if turn["idx"] != idx or turn["delta"] != value - previous:
            raise ValueError(f"ctx turn {idx}: non-derived idx/delta")
        columns["ctx_ts"].append(turn["ts"] or None)
        columns["ctx_line"].append(turn["line"])
        columns["ctx_input"].append(value)
        columns["ctx_output"].append(turn["output"])
        previous = value
    return columns


def ctx_objects(timestamps: list[datetime | None], lines: list[int],
                inputs: list[int], outputs: list[int]) -> list[dict]:
    """Reconstruct JSONB's key order, UTC ISO timestamps and derived fields."""
    turns = []
    previous = 0
    for idx, (ts, line, value, output) in enumerate(
            zip(timestamps, lines, inputs, outputs, strict=True), 1):
        turns.append({
            "ts": ts.astimezone(timezone.utc).isoformat() if ts else "",
            "idx": idx,
            "line": line,
            "delta": value - previous,
            "input": value,
            "output": output,
        })
        previous = value
    return turns
