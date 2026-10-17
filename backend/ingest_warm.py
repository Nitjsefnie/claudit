"""Warm default response-cache views after ingest or restart.

Keep warming outside ingest's module-size ceiling; ingest re-exports
warm_common and WARM_RANGES for callers and test patch seams."""
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
    """Warm default response-cache views after ingest or restart.

    Ingest rewrites leave database buffers cold; restart also empties the
    response cache. Warming keeps the first visitor from paying both;
    stale-while-revalidate cannot cover an empty cache.

    Warm only unfiltered defaults: endpoint × range × project × model is
    too large to precompute and would evict itself. Use the background
    pool so ingest returns immediately. Tests disable CLAUDIT_WARM_CACHE
    to prevent queries outliving the database fixture and racing teardown."""
    if getenv("CLAUDIT_WARM_CACHE", "1").lower() in ("0", "false", "no"):
        return

    # Mirror every RangePicker preset in src/app.jsx so no button lands
    # on an unwarmed default key. The 1d view uses live five-minute queries
    # below the hourly-rollup gate. /api/models has no range: warm its one
    # key per ingest to avoid a GROUP BY on every page open.
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
