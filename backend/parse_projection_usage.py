"""Project Claude usage values and pricing fields for one record."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from backend import pricing
from backend.ctx_input import usage_ctx_input
from backend.json_shape import as_dict


def _web_search_requests(usage: dict) -> int | None:
    """Return a non-negative server-side search count, or no count."""
    server_tool_use = as_dict(usage.get("server_tool_use"))
    count = server_tool_use.get("web_search_requests")
    return (count if isinstance(count, int) and not isinstance(count, bool)
            and count >= 0 else None)


@dataclass(frozen=True)
class _UsageTokens:
    """Token counts used by the row projection and cost calculation."""
    fresh: int
    create: int
    read: int
    output: int
    eph5: int
    eph1h: int
    web_search_requests: int | None


def _usage_tokens(usage: dict) -> _UsageTokens:
    """Read token counts from an event's decoded usage object."""
    fresh = int(usage.get("input_tokens", 0) or 0)
    create = int(usage.get("cache_creation_input_tokens", 0) or 0)
    read = int(usage.get("cache_read_input_tokens", 0) or 0)
    output = int(usage.get("output_tokens", 0) or 0)
    web_search_requests = _web_search_requests(usage)
    ephemeral = as_dict(usage.get("cache_creation") or {})
    eph5 = int(ephemeral.get("ephemeral_5m_input_tokens", 0) or 0)
    eph1h = int(ephemeral.get("ephemeral_1h_input_tokens", 0) or 0)
    return _UsageTokens(fresh, create, read, output, eph5, eph1h,
                        web_search_requests)


def _usage_cost(model: str, provider: str | None, ts: datetime | None,
                tokens: _UsageTokens) -> tuple[float, bool | None]:
    """Price token values at their event timestamp."""
    res = pricing.resolve(model, ts, provider)
    # A meter member above its own threshold bills the band; every other
    # Claude-format row keeps the NULL marker. The window is the billed
    # input side, the same columns the reprice pass re-derives from.
    long_context = pricing.meter_flag(
        model, tokens.fresh + tokens.create + tokens.read)
    cost = pricing.compute_cost(
        model,
        fresh=tokens.fresh, output=tokens.output,
        eph5=tokens.eph5, eph1h=tokens.eph1h,
        unsplit_create=max(0, tokens.create - tokens.eph5 - tokens.eph1h),
        read=tokens.read,
        # Dated rates apply to when the tokens were spent, not to
        # when this file happens to be parsed.
        adjustments=pricing.CostAdjustments(
            long_context=bool(long_context),
            web_search_requests=tokens.web_search_requests,
        ),
        res=res,
    )
    return cost, long_context


def project_usage(usage: dict, model: str, provider: str | None,
                  ts: datetime | None) -> dict:
    """Convert event usage into the records table's usage and cost fields."""
    tokens = _usage_tokens(usage)
    cost, long_context = _usage_cost(model, provider, ts, tokens)
    details = usage.get("output_tokens_details") or {}
    thinking_tokens = (int(details.get("thinking_tokens", 0) or 0)
                       if isinstance(details, dict) else 0)
    return {
        "fresh_tokens": tokens.fresh,
        "cache_creation_tokens": tokens.create,
        "cache_read_tokens": tokens.read,
        "output_tokens": tokens.output,
        "thinking_tokens": thinking_tokens,
        "eph5_tokens": tokens.eph5,
        "eph1h_tokens": tokens.eph1h,
        "cost_usd": round(cost, 6),
        "long_context": long_context,
        "web_search_requests": tokens.web_search_requests,
        "ctx_input": usage_ctx_input(usage),
    }
