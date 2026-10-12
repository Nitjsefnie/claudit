"""Project parsed Claude events into their persisted records rows."""
from __future__ import annotations

from backend import pricing
from backend.parse_common import _to_dt
from backend.parse_projection_usage import _usage_tokens
from backend.reply_timing import generation_values

_NO_ADJUSTMENTS = pricing.CostAdjustments()


def project_records(file_key: str, events: list[dict]) -> list[dict]:
    """Project one file's Claude events without a per-event row helper."""
    resolve = pricing.resolve
    meter_flag = pricing.meter_flag
    compute_cost = pricing.compute_cost
    records = []
    for ev in events:
        tokens = _usage_tokens(ev["usage"])
        ts = _to_dt(ev["ts"])
        model = ev["model"]
        provider = ev.get("provider")
        res = resolve(model, ts, provider)
        long_context = meter_flag(
            model, tokens.fresh + tokens.create + tokens.read)
        cost = compute_cost(
            model,
            fresh=tokens.fresh, output=tokens.output,
            eph5=tokens.eph5, eph1h=tokens.eph1h,
            unsplit_create=max(0, tokens.create - tokens.eph5 - tokens.eph1h),
            read=tokens.read,
            # Dated rates apply to when the tokens were spent, not to
            # when this file happens to be parsed.
            adjustments=(
                pricing.CostAdjustments(
                    long_context=bool(long_context),
                    web_search_requests=tokens.web_search_requests,
                )
                if long_context or tokens.web_search_requests
                else _NO_ADJUSTMENTS
            ),
            res=res,
        )
        generation = generation_values(ev.get("generation"))
        records.append({
            "file_key": file_key,
            "line_num": ev["line_num"],
            "uuid": ev["uuid"],
            "request_id": ev["request_id"],
            "ts": ts,
            "model": model,
            "provider": provider,
            "fresh_tokens": tokens.fresh,
            "cache_creation_tokens": tokens.create,
            "cache_read_tokens": tokens.read,
            "output_tokens": tokens.output,
            "text_chars": int(ev.get("text_chars", 0)),
            "reply_latency_s": ev.get("reply_latency_s"),
            "generation_s": generation[0],
            "output_tokens_per_s": generation[1],
            "stop_reason": ev.get("stop_reason"),
            "effort": ev.get("effort"),
            "thinking_tokens": tokens.thinking,
            "cli_version": ev.get("cli_version"),
            "turn_flags": ev.get("turn_flags") or [],
            "turn_tool_results": int(ev.get("turn_tool_results") or 0),
            "eph5_tokens": tokens.eph5,
            "eph1h_tokens": tokens.eph1h,
            # Provider-neutral cache-write count (issue #842): 0 here,
            # filled by parse_file()'s derivation over the file's records.
            "cache_write_tokens": 0,
            "cost_usd": round(cost, 6),
            "long_context": long_context,
            "web_search_requests": tokens.web_search_requests,
            "ctx_input": tokens.ctx_input,
        })
    return records
