"""Project one parsed Claude event into its persisted records row."""
from __future__ import annotations

from backend.parse_common import _to_dt
from backend.parse_projection_usage import project_usage


def _project_record(file_key: str, ev: dict) -> dict:
    """One walked event → its records-table row (token columns + cost)."""
    u = ev["usage"]
    ts = _to_dt(ev["ts"])
    projected = project_usage(u, ev["model"], ev.get("provider"), ts)
    return {
        "file_key": file_key,
        "line_num": ev["line_num"],
        "uuid": ev["uuid"],
        "request_id": ev["request_id"],
        "ts": ts,
        "model": ev["model"],
        "provider": ev.get("provider"),
        "fresh_tokens": projected["fresh_tokens"],
        "cache_creation_tokens": projected["cache_creation_tokens"],
        "cache_read_tokens": projected["cache_read_tokens"],
        "output_tokens": projected["output_tokens"],
        "text_chars": int(ev.get("text_chars", 0)),
        "reply_latency_s": ev.get("reply_latency_s"),
        "stop_reason": ev.get("stop_reason"),
        "effort": ev.get("effort"),
        "thinking_tokens": projected["thinking_tokens"],
        "cli_version": ev.get("cli_version"),
        "turn_flags": ev.get("turn_flags") or [],
        "turn_tool_results": int(ev.get("turn_tool_results") or 0),
        "eph5_tokens": projected["eph5_tokens"],
        "eph1h_tokens": projected["eph1h_tokens"],
        "cost_usd": projected["cost_usd"],
        "long_context": projected["long_context"],
        "web_search_requests": projected["web_search_requests"],
        "ctx_input": projected["ctx_input"],
    }
