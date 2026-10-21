"""Shared constants that would otherwise create import cycles.

Kept dependency-free: any module may import this one, and it imports no
other backend module.
"""
from __future__ import annotations

from pathlib import Path

# Display bucket widths /api/reply-latency can ask for, from
# api._bucket_seconds. 300 (the 24h view) is deliberately absent: a row
# per 5 minutes of all history to serve one day is not worth it, and that
# range stays on the live path.
LATENCY_BUCKETS = (3600, 21600, 43200, 86400)

# Fixed context-width buckets preserve detail across high-input spend.
# Use fresh + cache_creation + cache_read; inputs at or above CTX_BUCKET_MAX
# share its open-ended overflow bucket. Logarithmic buckets would compress
# the high-input region into a few broad bars.
#
# Edges are baked into stored rows, like LATENCY_BUCKETS: changing either
# constant requires a rollup rebuild, not a reparse, since records already
# carry the inputs.
CTX_BUCKET_WIDTH = 50_000
CTX_BUCKET_MAX = 1_000_000

# Above this, a record's context is not a request -- it is a cumulative
# counter. Set at twice the largest published window (the [1m] variants),
# so a real request can never reach it while a whole-session total always
# will. Used only to keep such a row out of the ctx_turns TRACE, whose
# y-axis is scaled off the maximum: one 115.8M row in the `zai` bucket,
# written by another harness under the all-zeros sentinel session id,
# flattened every real trace on glmmeter to a hairline. The record itself
# is still parsed, stored and priced -- this is a plausibility bound on
# one derived series, not a data-layer filter.
MAX_PLAUSIBLE_CTX = 2 * CTX_BUCKET_MAX


def ctx_bucket(ctx_tokens: int) -> int:
    """Lower edge of the bucket `ctx_tokens` falls in.

    Mirrored by the SQL in ingest.rebuild_ctx_cost_rollup(); the two must
    agree, and test_ctx_cost_rollup.py pins this side of it.
    """
    if ctx_tokens >= CTX_BUCKET_MAX:
        return CTX_BUCKET_MAX
    if ctx_tokens <= 0:
        return 0
    return (ctx_tokens // CTX_BUCKET_WIDTH) * CTX_BUCKET_WIDTH


def _read_version() -> str:
    """Repo version from the root VERSION file, or "unknown".

    The file is the single source of truth: `.github/workflows/release.yml`
    tags a release when it changes, and `/health` reports it to a
    deploy. Read once at import — it cannot change under a running
    process without a redeploy.

    A deploy that omits the file (a tarball, a partial checkout) gets
    "unknown" rather than a crash: /health reporting an unknown version is
    strictly better than /health not answering at all.
    """
    try:
        text = (Path(__file__).resolve().parent.parent / "VERSION").read_text(
            encoding="utf-8"
        )
    except OSError:
        return "unknown"
    return text.strip() or "unknown"


VERSION = _read_version()


# Rate-data semantics version: forces repricing from stored columns,
# never R2 refetch. Recompute cost, long_context, meter factors and
# web-search cost at the row's rate; clear request_fee_usd to NULL.
# Stored-flag changes also trigger repricing. PARSER_VERSION is only for
# parser-semantics changes.
PRICING_VERSION = "553"

# Stored rollup semantics version. Bump whenever any rollup SQL semantics
# change; it forces one complete rebuild without reparsing files.
DERIVED_STATE_VERSION = "6"

#: How ingest._fetch_marker turns a project.json body into a path. Each
#: lane_markers row records the version it was read under, and a row from
#: another version is re-fetched, so bump this whenever that reading
#: changes.
MARKER_READER_VERSION = "1"

#: What a file is attributed to when the transcript records no role at
#: all. It is the roster's own fallback dispatch type, and it is also
#: where every unattributable file lands — parse.resolve_agent_type for
#: Claude transcripts, parse_lanes.lane_agent_type for lane ones.
DEFAULT_AGENT_TYPE = "general-purpose"

INTERRUPT_MARKER = "[Request interrupted by user"

# The service's stop budget, in seconds. uvicorn spends
# SHUTDOWN_GRACEFUL_S draining connections BEFORE the lifespan teardown
# runs, and systemd SIGKILLs the process at TimeoutStopSec. EVERY term the
# teardown can spend draws from what is left, so no term can spend the
# window the term after it needs:
#
#     graceful  ->  render reap  ->  bounded ingest wait  ->  row close + tail
#
# A term sized past its share loses the race every time: the process is
# killed with the ingest_runs row still open, which is the finished_at NULL
# the teardown's fallback exists to prevent.
#
# Derived from the SHIPPED unit (examples/claudit.service) rather than
# restated per call site, and tests/test_shutdown_budget.py reads that file
# to pin both ends: the numbers here and the unit must agree, and the
# budgets the teardown actually PASSES must sum to TimeoutStopSec with
# SHUTDOWN_MARGIN_S spare for the fallback's single-row UPDATE.
SHUTDOWN_GRACEFUL_S = 5.0
SHUTDOWN_STOP_BUDGET_S = 10.0
# What the teardown may spend once uvicorn's graceful window is done.
SHUTDOWN_TEARDOWN_S = SHUTDOWN_STOP_BUDGET_S - SHUTDOWN_GRACEFUL_S
# Spare for the fallback's single-row UPDATE and teardown tail, even
# when stop lands part-way through. Being early costs nothing: the
# run's own close wins either way.
SHUTDOWN_MARGIN_S = 1.5
# One shared reap budget for ALL render children, ahead of ingest wait.
# Per-child bounds multiply teardown time and can prevent row close.
# SIGKILL normally makes reaping instant; the bound covers wedged children
# that the service stop will kill regardless.
SHUTDOWN_RENDER_REAP_S = 1.5
# Remaining stop budget for ingest unwind: a fetch chunk and final
# transaction. A run exceeding this bound loses its own close to the
# fallback, which closes the same row as aborted.
SHUTDOWN_RUN_WAIT_S = (SHUTDOWN_TEARDOWN_S - SHUTDOWN_MARGIN_S
                       - SHUTDOWN_RENDER_REAP_S)
