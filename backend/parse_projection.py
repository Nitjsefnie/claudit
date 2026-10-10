"""Project parsed Claude events into their persisted records rows."""
from __future__ import annotations

from backend import pricing
from backend.parse_common import _to_dt
from backend.parse_projection_usage import _usage_tokens

_NO_ADJUSTMENTS = pricing.CostAdjustments()


def project_records(file_key: str, events: list[dict]) -> list[dict]:
    """Project one file's Claude events without a per-event row helper."""
    resolve = pricing.resolve
    meter_flag = pricing.meter_flag
    compute_cost = pricing.compute_cost
    cost_adjustments = pricing.CostAdjustments
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
                cost_adjustments(
                    long_context=bool(long_context),
                    web_search_requests=tokens.web_search_requests,
                )
                if long_context or tokens.web_search_requests
                else _NO_ADJUSTMENTS
            ),
            res=res,
        )
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
            "stop_reason": ev.get("stop_reason"),
            "effort": ev.get("effort"),
            "thinking_tokens": tokens.thinking,
            "cli_version": ev.get("cli_version"),
            "turn_flags": ev.get("turn_flags") or [],
            "turn_tool_results": int(ev.get("turn_tool_results") or 0),
            "eph5_tokens": tokens.eph5,
            "eph1h_tokens": tokens.eph1h,
            "cost_usd": round(cost, 6),
            "long_context": long_context,
            "web_search_requests": tokens.web_search_requests,
            "ctx_input": tokens.ctx_input,
        })
    return records
