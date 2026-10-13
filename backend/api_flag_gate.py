"""Pure route eligibility and the assembled API's feature dependency."""
from __future__ import annotations

from fastapi import HTTPException, Request

from backend import flags
from backend.flags import Config

DASHBOARD_CONSUMERS = (
    "usage_timeseries", "thinking_output", "line_churn", "model_comparison",
    "token_breakdown", "project_comparison", "cache_ttl", "response_sizes",
    "burn_rate", "summary", "sessions",
)
MODEL_CONSUMERS = (
    "model_comparison", "token_breakdown", "context_growth", "context_comparison",
    "tool_usage", "reply_latency", "page_performance", "tool_error_rate",
    "agent_comparison", "activity_heatmap", "inspector",
)
# Route templates are stable across session IDs and router inclusion.
ENDPOINT_FEATURES = {
    "/api/tool-usage": "tool_usage",
    "/api/tool-error-rate": "tool_error_rate",
    "/api/reply-latency": "reply_latency",
    "/api/activity-heatmap": "activity_heatmap",
    "/api/cost-by-context": "context_comparison",
    "/api/cost-by-agent": "agent_comparison",
    "/api/web-metrics": "page_performance",
    "/api/context-growth/traces": "context_growth",
    "/api/context-growth/agg": "cache",
    "/api/context-growth/session/{session_id}": "inspector",
    "/api/cache": "cache",
    "/api/sessions": "sessions",
    "/api/sessions/{session_id}": "inspector",
    "/api/sessions/{session_id}/transcript": "inspector",
    "/api/sessions/{session_id}/sidecar": "inspector",
    "/api/projects": "project_picker",
    "/api/export": "export_png",
    "/api/events": "live_updates",
}


def any_enabled(config: Config, consumers: tuple[str, ...]) -> bool:
    """Whether a shared resource still has a consumer."""
    return any(config.features[key] for key in consumers)


def endpoint_enabled(path: str, config: Config, method: str = "GET") -> bool:
    """Eligibility for route templates and warm targets; telemetry stays on."""
    if method != "GET":
        return True
    if path == "/api/dashboard":
        return any_enabled(config, DASHBOARD_CONSUMERS)
    if path == "/api/models":
        return any_enabled(config, MODEL_CONSUMERS)
    feature = ENDPOINT_FEATURES.get(path)
    return feature is None or config.features[feature]


def require_enabled(request: Request) -> None:
    """Reject disabled reads before the handler enters caches or IO."""
    config = flags.CONFIG
    if "project" in request.query_params and not config.features["project_picker"]:
        raise HTTPException(404, "feature disabled")
    # Newer FastAPI lazy inclusion keeps the original sub-router template
    # in scope; the URL path supplies the assembled /api prefix.
    path = request.url.path
    if path.startswith("/api/sessions/"):
        tail = path.rsplit("/", 1)[-1]
        path = "/api/sessions/{session_id}"
        if tail in ("transcript", "sidecar"):
            path += "/" + tail
    elif path.startswith("/api/context-growth/session/"):
        path = "/api/context-growth/session/{session_id}"
    if not endpoint_enabled(path, config, request.method):
        raise HTTPException(404, "feature disabled")
