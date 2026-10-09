"""Project one parsed Claude event into its persisted records row."""
from __future__ import annotations

from backend import pricing
from backend.ctx_input import usage_ctx_input
from backend.json_shape import as_dict
from backend.parse_common import _to_dt


def _project_record(file_key: str, ev: dict) -> dict:  # pylint: disable=too-many-locals
    """One walked event → its records-table row (token columns + cost)."""
    u = ev["usage"]
    fresh = int(u.get("input_tokens", 0) or 0)
    create = int(u.get("cache_creation_input_tokens", 0) or 0)
    read = int(u.get("cache_read_input_tokens", 0) or 0)
    output = int(u.get("output_tokens", 0) or 0)
    server_tool_use = as_dict(u.get("server_tool_use"))
    search_count = server_tool_use.get("web_search_requests")
    web_search_requests = (
        search_count if isinstance(search_count, int)
        and not isinstance(search_count, bool) and search_count >= 0 else None)
    eph = as_dict(u.get("cache_creation") or {})
    eph5 = int(eph.get("ephemeral_5m_input_tokens", 0) or 0)
    eph1h = int(eph.get("ephemeral_1h_input_tokens", 0) or 0)
    ts = _to_dt(ev["ts"])
    res = pricing.resolve(ev["model"], ts, ev.get("provider"))
    # The meter decision (issue #765): a meter member above its own
    # threshold bills the band; every other Claude-format row keeps the
    # NULL marker. The window is the billed input side, the same columns
    # the reprice pass re-derives from.
    long_context = pricing.meter_flag(ev["model"], fresh + create + read)
    cost = pricing.compute_cost(
        ev["model"],
        fresh=fresh, output=output,
        eph5=eph5, eph1h=eph1h,
        unsplit_create=max(0, create - eph5 - eph1h), read=read,
        # Dated rates apply to when the tokens were spent, not to
        # when this file happens to be parsed.
        long_context=bool(long_context),
        web_search_requests=web_search_requests,
        res=res,
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
        "thinking_tokens": int(details.get("thinking_tokens", 0) or 0)
        if isinstance(details := u.get("output_tokens_details") or {}, dict) else 0,
        "cli_version": ev.get("cli_version"),
        "turn_flags": ev.get("turn_flags") or [],
        "turn_tool_results": int(ev.get("turn_tool_results") or 0),
        "eph5_tokens": eph5,
        "eph1h_tokens": eph1h,
        "cost_usd": round(cost, 6),
        "long_context": long_context,
        "web_search_requests": web_search_requests,
        "ctx_input": usage_ctx_input(u),
    }
