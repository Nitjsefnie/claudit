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

# Context-size buckets for `ctx_cost_rollup` / the Cost by Context panel.
# The per-call window is fresh + cache_creation + cache_read, bucketed to
# CTX_BUCKET_WIDTH; everything at or above CTX_BUCKET_MAX folds into one
# open-ended overflow bucket keyed by CTX_BUCKET_MAX itself.
#
# Fixed width rather than logarithmic, and that is a measurement not a
# taste: over the live corpus, cost is near-flat across the window --
# 7.8% of all spend in 100-150k, still 5.2% in 700-750k, ~60% above
# 300k. Log buckets would compress precisely the region carrying the
# money into a handful of fat bars.
#
# These edges are BAKED INTO STORED ROWS, exactly like LATENCY_BUCKETS:
# a read cannot re-bucket to a different width, so changing either
# constant requires rebuilding the rollup (bump nothing else -- the
# rollup derives from stored `records` columns, so no reparse).
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
    tags a release when it changes, and `speed.yml` compares against the
    release it names. Read once at import — it cannot change under a
    running process without a redeploy.

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


# Parse-output schema/semantics version. Ingest reparses every file whose
# stored `parser_version` differs from this, so it is the ONLY switch that
# forces a full reparse.
#
# It lives HERE, in code, and not in the environment: a parser change and the
# reparse it requires must travel in the same commit. When this was read from
# .env, shipping a parser change without an operator also editing that file
# left every stored row at the old semantics with nothing to detect it, and a
# deploy that never set the variable at all sat forever on the "1" default.
#
# BUMP THIS in the same commit as any change to parse.py semantics or to
# the set of columns parse_file() emits. A rate change in src/pricing.json
# reprices stored records instead of reparsing them: bump PRICING_VERSION
# below for that.
PARSER_VERSION = "89"

# The rate-data semantics version, stored per record: the ONLY switch
# that forces a REPRICE, and a reprice never refetches R2 — the reprice
# pass (issue #193) recomputes each stale row's cost_usd AND long_context
# (issue #194) from its own stored columns, so a stored-flag rule change
# is a trigger with no rate change at all. PARSER_VERSION bumps only for
# parser-semantics changes from now on.
PRICING_VERSION = "105"

# Stored rollup semantics version. Bump whenever any rollup SQL semantics
# change; it forces one complete rebuild without reparsing files.
DERIVED_STATE_VERSION = "1"

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

# The text Claude Code writes when the user cuts a reply off.
INTERRUPT_MARKER = "[Request interrupted by user"

# The service's stop budget, in seconds (issue #410). uvicorn spends
# SHUTDOWN_GRACEFUL_S draining connections BEFORE the lifespan teardown
# runs, and systemd SIGKILLs the process at TimeoutStopSec — so the
# teardown's bounded wait on an in-flight ingest, plus the row close
# behind it, has to fit in what is left of that. A wait sized past it
# loses the race every time: the process is killed with the ingest_runs
# row still open, which is the finished_at NULL the teardown's fallback
# exists to prevent.
#
# Derived from the SHIPPED unit (examples/claudit.service) rather than
# restated per call site, and tests/test_shutdown_budget.py reads that
# file to pin both ends: the numbers here and the unit must agree, and
# the wait the teardown actually uses must leave SHUTDOWN_MARGIN_S spare
# for the fallback's single-row UPDATE and the tail of the teardown.
#
# What this budget does NOT cover: the teardown steps that run BEFORE the
# wait spend the window it leaves behind — the export reap above all,
# which bounds itself per live render child rather than out of these
# numbers. A stop arriving with several live renders is the reap's own
# arithmetic, not this one.
SHUTDOWN_GRACEFUL_S = 5.0
SHUTDOWN_STOP_BUDGET_S = 10.0
# Spare for the fallback close itself and the tail of the teardown after
# it, so a stop landing while the teardown is already part-way through
# still lands the row. Generous on purpose: the fallback is one UPDATE,
# and being early costs nothing — the run's own close wins either way.
SHUTDOWN_MARGIN_S = 2.0
# What the teardown may spend waiting for the in-flight run. A run's
# abort unwind is one fetch chunk plus one final transaction, normally
# sub-second, so this is a bound and not a target.
SHUTDOWN_RUN_WAIT_S = (SHUTDOWN_STOP_BUDGET_S - SHUTDOWN_GRACEFUL_S
                       - SHUTDOWN_MARGIN_S)
