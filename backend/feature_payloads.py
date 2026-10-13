"""Default-preserving projections for shared dashboard data and bootstrap."""
from __future__ import annotations

from copy import deepcopy

from backend.branding import script_json
from backend.flags import Config

TOKEN_CONSUMERS = (
    "usage_timeseries", "model_comparison", "token_breakdown", "summary", "burn_rate",
)
HOURLY_CONSUMERS = TOKEN_CONSUMERS + ("cache_ttl", "thinking_output", "line_churn")
SUMMARY_FIELDS = (
    "total_sessions", "main_w_usage", "main_empty", "subagent_files",
    "subagent_only_sessions", "total_prompts", "total_turns",
)
EXCLUSIVE_FIELDS = {
    "model_comparison": ("cost_by_model", "cost_by_model_provider"),
    "project_comparison": ("cost_by_project", "tokens_by_project"),
    "response_sizes": ("response_sizes",),
    "rate_limits": ("rate_limit_hits",),
    "summary": SUMMARY_FIELDS,
}


def hourly_enabled(config: Config) -> bool:
    return any(config.features[key] for key in HOURLY_CONSUMERS)


def _hourly_fields(config: Config) -> set[str]:
    """Keep each leaf while at least one of its consumers needs it."""
    on = config.features
    fields = {"hour", "model"}
    if any(on[key] for key in TOKEN_CONSUMERS):
        fields.update(("input_tokens", "output_tokens", "cache_5m_tokens",
                       "cache_1h_tokens", "cache_read_tokens", "provider", "long_context"))
    elif on["cache_ttl"]:
        fields.update(("cache_5m_tokens", "cache_1h_tokens"))
    if any(on[key] for key in ("usage_timeseries", "model_comparison", "summary")):
        fields.add("cost_usd")
    if on["summary"]:
        fields.update(("requests", "session_count"))
    if on["thinking_output"]:
        fields.add("thinking_tokens")
    if on["line_churn"]:
        fields.update(("lines_added", "lines_deleted"))
    return fields


def project_dashboard(payload: dict, config: Config) -> dict:
    """Filter before cache insertion; keep all-ON bytes and object identity."""
    if not config.disabled_features():
        return payload
    out = deepcopy(payload)
    for feature, keys in EXCLUSIVE_FIELDS.items():
        if not config.features[feature]:
            for key in keys:
                out.pop(key, None)
    out["hide_rate_limits"] = not config.features["rate_limits"]
    if not (config.features["sessions"] or config.features["burn_rate"]):
        out.pop("sessions", None)
    else:
        for session in out.get("sessions", []):
            if not config.features["burn_rate"]:
                session.pop("ctx_at_end", None)
            if not (config.features["context_growth"] or config.features["burn_rate"]):
                session.pop("models_used", None)
    if not hourly_enabled(config):
        out.pop("hourly", None)
        out.pop("token_types", None)
    else:
        fields = _hourly_fields(config)
        out["hourly"] = [{key: value for key, value in row.items() if key in fields}
                         for row in out.get("hourly", [])]
        out["token_types"] = [key for key in out.get("token_types", []) if key in fields]
    return out


def features_script(config: Config) -> str:
    """Missing bootstrap means all ON; otherwise publish the effective map."""
    if not config.disabled_features():
        return ""
    return f"window.FEATURES = {script_json(dict(config.features))};"
