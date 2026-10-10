"""Project Claude usage values and pricing fields for one record."""
from __future__ import annotations

from typing import NamedTuple

from backend.ctx_input import usage_ctx_input


class _UsageTokens(NamedTuple):
    """Token counts used by the row projection and cost calculation."""
    fresh: int
    create: int
    read: int
    output: int
    thinking: int
    eph5: int
    eph1h: int
    web_search_requests: int | None
    ctx_input: int


def _usage_tokens(usage: dict) -> _UsageTokens:
    """Read token counts from an event's decoded usage object."""
    fresh = int(usage.get("input_tokens", 0) or 0)
    create = int(usage.get("cache_creation_input_tokens", 0) or 0)
    read = int(usage.get("cache_read_input_tokens", 0) or 0)
    output = int(usage.get("output_tokens", 0) or 0)
    search_count = usage.get("server_tool_use")
    search_count = (search_count.get("web_search_requests")
                    if isinstance(search_count, dict) else None)
    web_search_requests = (
        search_count if isinstance(search_count, int)
        and not isinstance(search_count, bool) and search_count >= 0 else None)
    ephemeral = usage.get("cache_creation") or {}
    if not isinstance(ephemeral, dict):
        ephemeral = {}
    eph5 = int(ephemeral.get("ephemeral_5m_input_tokens", 0) or 0)
    eph1h = int(ephemeral.get("ephemeral_1h_input_tokens", 0) or 0)
    details = usage.get("output_tokens_details") or {}
    thinking = (int(details.get("thinking_tokens", 0) or 0)
                if isinstance(details, dict) else 0)
    # Reuse already-read tokens for the common path. Multiple raw entries
    # need the shared policy to filter malformed calls and select their peak.
    ctx_input = fresh + create + read
    iterations = usage.get("iterations")
    if isinstance(iterations, list) and len(iterations) > 1:
        ctx_input = usage_ctx_input(usage)
    return _UsageTokens(fresh, create, read, output, thinking, eph5, eph1h,
                        web_search_requests, ctx_input)
