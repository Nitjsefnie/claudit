"""The per-call context-window size a usage envelope put into context.

Mirrored by src/parser.js's usageCtxInput (SV-PARSER-SPEC lockstep).
"""
from __future__ import annotations

from backend.json_shape import dict_list


def usage_ctx_input(u: dict) -> int:
    """Per-call context-window size (SV-PARSER-SPEC): when the harness
    fans out multiple sub-calls (advisor()/retries), they get rolled into
    one `usage` envelope as `iterations`. The top-level fresh+create+read
    is the BILLING sum across iterations, not the peak single-call
    window. For context-growth panels we want the peak, so take
    max-of-iteration-totals when >1 iters. Single-iter (or absent) →
    fall back to top-level sum.
    """
    iters = dict_list(u.get("iterations") or [])
    if len(iters) > 1:
        return max(
            (int(it.get("input_tokens", 0) or 0)
             + int(it.get("cache_creation_input_tokens", 0) or 0)
             + int(it.get("cache_read_input_tokens", 0) or 0))
            for it in iters
        )
    return (
        int(u.get("input_tokens", 0) or 0)
        + int(u.get("cache_creation_input_tokens", 0) or 0)
        + int(u.get("cache_read_input_tokens", 0) or 0)
    )
