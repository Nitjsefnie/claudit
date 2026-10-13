"""Dashboard row folds and shared-payload projection."""
from __future__ import annotations

from backend import flags
from backend.api_common import _iso
from backend.feature_payloads import project_dashboard


# The hourly SELECT's column order, minus `hour`, `model`, `long_context`
# and `provider`, which the entry treats specially. Named here so the
# unpack does not spend one local per column.
_HOURLY_TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_5m_tokens",
    "cache_1h_tokens",
    "cache_read_tokens",
    # A SUBSET of output_tokens (the API reports it under
    # usage.output_tokens_details), so it is declared as a token type for
    # its own panel but never summed into a total or priced.
    "thinking_tokens",
)


def _hourly_entry(row, seen_hours: set) -> dict:
    """One hourly panel entry. `session_count` is attributed to the first
    row of each hour only — the rows are per (hour, model, long_context,
    provider), so summing the column across models would double-count. A
    long-context row is part of the same hour's data (its flag rides the
    entry for the browser breakdown to price by), and so is a provider
    row: `provider` is None for a record that named no serving host."""
    hour, model, long_context, provider = row[:4]
    tokens = row[4:4 + len(_HOURLY_TOKEN_KEYS)]
    cost, reqs, sc = row[4 + len(_HOURLY_TOKEN_KEYS):]
    hour_iso = _iso(hour)
    is_first_for_hour = hour_iso not in seen_hours
    seen_hours.add(hour_iso)
    entry = {"hour": hour_iso, "model": model,
             "long_context": bool(long_context), "provider": provider or None}
    entry.update(dict(zip(_HOURLY_TOKEN_KEYS, (int(v or 0) for v in tokens))))
    entry["cost_usd"] = float(cost or 0)
    entry["requests"] = int(reqs or 0)
    entry["session_count"] = int(sc or 0) if is_first_for_hour else 0
    return entry


# Token-type fields the hourly panel can carry, in render order.
# `thinking_tokens` is the one entry that is NOT part of the billed
# partition: it is a subset of output_tokens, so it is declared here for
# zero-suppression and panel order and must never be summed with the
# rest. It follows output_tokens because it is a breakdown of it.
TOKEN_TYPE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "thinking_tokens",
    "cache_5m_tokens",
    "cache_1h_tokens",
    "cache_read_tokens",
)


def drop_zero_token_types(entries: list[dict]) -> list[dict]:
    """Hide token types whose total is zero across the data in view.

    ZERO-SUPPRESSION AT THE PRESENTATION LAYER, NOT OMISSION AT THE DATA
    LAYER. The column exists, the value is stored, the rate is wired.
    What this spares a viewer is a panel that reads zero in every bucket
    on screen.

    Not hypothetical: this same codebase is deployed as glmmeter over the
    `zai` bucket, where cache_creation, eph5 and eph1h are 0 across all
    90,316 canonical records, so Cache Create and the whole Prompt-Cache
    TTL Split render permanently flat there.

    The decision is per RESPONSE, over the summed totals, not per entry:
    suppressing per entry would make a series flicker in and out between
    buckets. The day a request lands with a real value for one of these
    it reappears on its own -- no migration, no backfill, no change here.

    FRONTEND MIRROR: src/app.jsx must treat every TOKEN_TYPE_FIELDS key
    as optional and render whichever arrive, rather than naming them one
    by one. A consumer that adds two suppressed keys together gets NaN,
    not 0.
    """
    totals = {
        f: sum(int(e.get(f) or 0) for e in entries)
        for f in TOKEN_TYPE_FIELDS
    }
    empty = [f for f, total in totals.items() if total == 0]
    if not empty:
        return entries
    for entry in entries:
        for field in empty:
            entry.pop(field, None)
    return entries


def surviving_token_types(entries: list[dict]) -> list[str]:
    """The token fields still present after suppression, in render order.

    Field NAMES only, deliberately: claudit's panel set is fixed and one
    of its panels (Cache Create) plots two of these summed, so a label or
    an is-additive flag would be sent and never read. The frontend needs
    exactly one thing from this -- which panels to draw.
    """
    present = {k for entry in entries for k in entry}
    return [f for f in TOKEN_TYPE_FIELDS if f in present]


# The billed partition (SV-SUBSET-TOKENS: thinking_tokens is inside
# output_tokens and never added).
_BILLED_TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_5m_tokens",
                      "cache_1h_tokens", "cache_read_tokens")


def _fold_hourly(hourly_rows) -> tuple[list, list, list]:
    """The hourly panel, cost_by_model and cost_by_model_provider, folded
    from the same rows rather than costing their own passes over the
    records.

    cost_by_model_provider is cost AND tokens per (model, provider). It
    keeps zero-cost rows that carry tokens, because a free host (Stealth)
    still has tokens to show."""
    hourly = []
    seen_hours: set[str | None] = set()
    cost_by_model_acc: dict[str, float] = {}
    split_acc: dict[tuple, list] = {}
    for row in hourly_rows:
        entry = _hourly_entry(row, seen_hours)
        hourly.append(entry)
        model, cost = entry["model"], entry["cost_usd"]
        cost_by_model_acc[model] = cost_by_model_acc.get(model, 0.0) + cost
        acc = split_acc.setdefault((model, entry["provider"]), [0.0, 0])
        acc[0] += cost
        acc[1] += sum(entry[k] for k in _BILLED_TOKEN_KEYS)
    cost_by_model = sorted(
        ({"model": m, "cost_usd": v} for m, v in cost_by_model_acc.items() if v > 0),
        key=lambda r: r["cost_usd"],
        reverse=True,
    )
    cost_by_model_provider = sorted(
        ({"model": m, "provider": p, "cost_usd": c, "total_tokens": t}
         for (m, p), (c, t) in split_acc.items() if c > 0 or t > 0),
        key=lambda r: (r["cost_usd"], r["total_tokens"]),
        reverse=True,
    )
    return hourly, cost_by_model, cost_by_model_provider


def _attach_churn(hourly: list, churn_rows) -> None:
    """Fold per-bucket line churn onto the hourly entries (issue #10).

    Churn is bucketed per hour, NOT per model, so — like session_count —
    it is attributed to the first model row of each hour only: the
    TimeSeriesPanel bins sum the key across every entry in the bin, and
    attaching it to each (hour, model) row would multiply it by the
    model count."""
    churn_by_hour = {
        _iso(hour): (int(added or 0), int(deleted or 0))
        for (hour, added, deleted) in churn_rows
    }
    seen_hours: set[str | None] = set()
    for entry in hourly:
        hour_iso = entry["hour"]
        added, deleted = 0, 0
        if hour_iso not in seen_hours:
            seen_hours.add(hour_iso)
            added, deleted = churn_by_hour.get(hour_iso, (0, 0))
        entry["lines_added"] = added
        entry["lines_deleted"] = deleted


def _fold_cost_by_project(rows) -> list:
    """Zero-cost rows are noise (the project picker drops all-time-zero
    projects too), so they're excluded. Top 10 by range cost; the tail
    collapses into ONE "Other (N projects)" row — a bar per project is
    unreadable at ~70 projects."""
    positive = [
        {"project": p or "unknown", "cost_usd": float(cost or 0)}
        for (p, cost) in rows
        if float(cost or 0) > 0
    ]
    cost_by_project = positive[:10]
    if len(positive) > 10:
        rest = positive[10:]
        cost_by_project.append({
            "project": f"Other ({len(rest)} projects)",
            "cost_usd": sum(r["cost_usd"] for r in rest),
        })
    return cost_by_project


def _fold_tokens_by_project(rows) -> list:
    """Tokens by Project's fold — the same shape as _fold_cost_by_project:
    zero-token rows are noise, top 10 by range tokens, the tail as ONE
    "Other (N projects)" row. The measure is the billed partition
    (fresh + cache_creation + cache_read + output); thinking_tokens is a
    SUBSET of output and never enters the sum (SV-SUBSET-TOKENS)."""
    positive = [
        {"project": p or "unknown", "total_tokens": int(t or 0)}
        for (p, t) in rows
        if int(t or 0) > 0
    ]
    tokens_by_project = positive[:10]
    if len(positive) > 10:
        rest = positive[10:]
        tokens_by_project.append({
            "project": f"Other ({len(rest)} projects)",
            "total_tokens": sum(r["total_tokens"] for r in rest),
        })
    return tokens_by_project


def _session_entry(row, ctx_end: dict) -> dict:
    """One sessions-panel entry, with ctx_at_end folded in from the
    per-session ctx-end map."""
    (sid, st, et, reqs, inp, out, cc, cr, cost, dom, models_used) = row
    # null (not 0) when ctx_turns is empty so the UI can flag the dot
    # as "ctx unknown" instead of silently falling back to a synthetic
    # duration-based size encoding.
    ctx_at_end = ctx_end.get(sid)
    return {
        "session_id": sid,
        "start_ts": float(st or 0),
        "end_ts": float(et or 0),
        "requests": int(reqs or 0),
        "input_tokens": int(inp or 0),
        "output_tokens": int(out or 0),
        "cache_create_tokens": int(cc or 0),
        "cache_read_tokens": int(cr or 0),
        "cost_usd": float(cost or 0),
        "model": dom or "",
        "models_used": list(models_used or []),
        "ctx_at_end": ctx_at_end,
    }


def _fold_sessions(sessions_rows, ctx_end: dict) -> list:
    """Per-session aggregates, with ctx_at_end folded in from the
    per-session ctx-end map."""
    return [
        _session_entry(row, ctx_end) for row in sessions_rows
    ]


def _fold_rate_limits(rl_rows) -> list:
    rate_limit_hits = []
    for sid, hit in rl_rows:
        ts_str = (hit or {}).get("ts") or ""
        if not ts_str:
            continue
        rate_limit_hits.append({
            "session_id": sid,
            "ts": ts_str,
            "content": (hit or {}).get("content", ""),
        })
    return rate_limit_hits


def _dashboard_build(rows: dict, rng: str, project: str | None,
                     bucket_s: int) -> dict:
    """Fold the raw panel rows into the response payload."""
    hourly, cost_by_model, cost_by_model_provider = _fold_hourly(rows["hourly"])
    _attach_churn(hourly, rows["churn"])
    drop_zero_token_types(hourly)
    file_counts_row = rows["file_counts_row"] or (0, 0, 0, 0, 0, 0)
    total_sessions_row = rows["total_sessions_row"]
    return project_dashboard({
        "range": rng,
        "project": project,
        "bucket_s": bucket_s,
        "hourly": hourly,
        "token_types": surviving_token_types(hourly),
        "cost_by_model": cost_by_model,
        "cost_by_model_provider": cost_by_model_provider,
        "cost_by_project": _fold_cost_by_project(rows["cost_by_project"]),
        "tokens_by_project": _fold_tokens_by_project(rows["tokens_by_project"]),
        "rate_limit_hits": (_fold_rate_limits(rows["rate_limits"])
                            if flags.CONFIG.features["rate_limits"] else []),
        "hide_rate_limits": not flags.CONFIG.features["rate_limits"],
        "sessions": _fold_sessions(rows["sessions"], rows["ctx_end"]),
        "total_sessions": int(total_sessions_row[0] or 0) if total_sessions_row else 0,
        "main_w_usage": int(file_counts_row[0] or 0),
        "main_empty": int(file_counts_row[1] or 0),
        "subagent_files": int(file_counts_row[2] or 0),
        "subagent_only_sessions": int(file_counts_row[3] or 0),
        "total_prompts": int(file_counts_row[4] or 0),
        "total_turns": int(file_counts_row[5] or 0),
        # `turns` is a FLAT array of ctx values, positionally indexed.
        # It used to be [{"t": i, "ctx": n}, ...] where `t` was just the
        # array index the consumer re-derived anyway — ~20 bytes per turn
        # instead of ~6, repeated across 54k turns. session_id/is_main are
        # dropped too: they are only used server-side above (to fold in
        # ctx_turns) and no consumer reads them off the wire.
        # The per-file ctx traces moved to /api/context-growth/traces
        # (issue #644): they were 82% of this payload's gzip (1.83 MB of
        # a 2.36 MB body at range=all) while drawing one panel of ~30.
        # The session fold above reads only each main file's LAST turn,
        # through api_ctx_growth.ctx_end_rows — the full trace query is
        # never run here (issue #838).
        "response_sizes": [
            {
                "ts": _iso(bucket),
                "model": m,
                "n": int(n or 0),
                "p50": float(p50 or 0),
                "p90": float(p90 or 0),
            }
            for (bucket, m, n, p50, p90) in rows["response_sizes"]
        ],
    }, flags.CONFIG)
