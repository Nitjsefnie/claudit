"""Response-cache warming after an ingest or a restart.

`warm_common` pre-populates the response cache for the views a fresh
visitor hits. Split out of ingest for the same reason as ingest_persist
and ingest_rollups: ingest.py sits at pylint's module-size limit, so any
growth means a move first. `ingest` re-exports both names, so
`ingest.warm_common()` and `ingest.WARM_RANGES` keep resolving for
callers and tests.
"""
from __future__ import annotations

import logging

from backend.env_config import getenv
from backend import api, cache, flags
from backend.api_flag_gate import endpoint_enabled
from backend.api_ctx_growth import context_growth_traces
from backend.api_dashboard import dashboard
from backend.api_web_metrics import web_metrics_readout

log = logging.getLogger("claudit.ingest")

# Ranges warm_common pre-populates. Mirrors RangePicker's presets in
# src/app.jsx; anything the UI can request but this omits stays cold.
WARM_RANGES = ("all", "365d", "90d", "30d", "7d", "1d")


def warm_common() -> None:
    """Pre-populate the response cache for the views a fresh visitor hits.

    After an ingest the buffer cache is cold (the recompute and rollup
    rebuild just rewrote the tables) and after a RESTART the response
    cache is empty too, so the first load pays both — measured 6.0s vs
    1.4s warm. Stale-while-revalidate cannot cover the restart case
    because there is nothing stale to serve.

    Only the unfiltered default views are warmed. The full keyspace is
    (endpoint x range x project x model), which is far too large to
    precompute and would mostly evict itself; a project the user actually
    opens still costs one cold query, but the landing view never does.

    Runs on the cache's background pool, so ingest returns immediately.
    Disabled by CLAUDIT_WARM_CACHE=0 — the tests set that, because a warm
    outlives the fixture that created its database and its queries then
    race the teardown that drops it.
    """
    if getenv("CLAUDIT_WARM_CACHE", "1").lower() in ("0", "false", "no"):
        return

    # Every range the picker offers, so no button lands on a cold query.
    # Must mirror RangePicker's preset values in src/app.jsx — a range the
    # UI can request but this does not list is a permanently cold key.
    # (This was ("all","30d","7d") back when these queries cost seconds
    # each; with the rollups they are ~0.1-1s, so covering all six is
    # cheap. "1d" is arguably the most valuable: its 5-minute buckets are
    # below the rollups' 1h gate, so it is the one range still served by
    # live queries.)
    # /api/models takes no range — one key, warmed once per ingest. It is
    # fetched on every page open and its query is a full GROUP BY over
    # records (issue #644).
    if endpoint_enabled("/api/models", flags.CONFIG):
        cache.warm(api.list_models)
    targets = (
        ("/api/dashboard", dashboard),
        ("/api/context-growth/traces", context_growth_traces),
        ("/api/activity-heatmap", api.activity_heatmap),
        ("/api/tool-usage", api.tool_usage),
        ("/api/tool-error-rate", api.tool_error_rate),
        ("/api/reply-latency", api.reply_latency),
        ("/api/web-metrics", web_metrics_readout),
        ("/api/projects", api.list_projects),
    )
    for rng in WARM_RANGES:
        for path, target in targets:
            if endpoint_enabled(path, flags.CONFIG):
                cache.warm(target, rng=rng)
    log.info("warm_common: queued %d range(s)", len(WARM_RANGES))
