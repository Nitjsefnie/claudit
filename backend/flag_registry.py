"""Declarative startup collection and feature switch metadata."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CollectionSpec:
    key: str
    env: str
    record_fields: tuple[str, ...] = ()
    tool_fields: tuple[str, ...] = ()
    file_fields: tuple[str, ...] = ()
    rollup_tables: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("record_fields", "tool_fields", "file_fields", "rollup_tables"):
            object.__setattr__(self, name, tuple(getattr(self, name)))


@dataclass(frozen=True)
class FeatureSpec:
    key: str
    env: str
    collection_dependencies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "collection_dependencies", tuple(self.collection_dependencies)
        )


COLLECTION_SPECS = (
    CollectionSpec(
        "turn_context", "CLAUDIT_COLLECT_TURN_CONTEXT",
        record_fields=("turn_flags", "turn_tool_results", "cli_version"),
    ),
    CollectionSpec(
        "request_metadata", "CLAUDIT_COLLECT_REQUEST_METADATA",
        record_fields=("stop_reason", "effort"),
    ),
    CollectionSpec(
        "cache_write_tokens", "CLAUDIT_COLLECT_CACHE_WRITE_TOKENS",
        record_fields=("cache_write_tokens",),
    ),
    CollectionSpec(
        "context_intake", "CLAUDIT_COLLECT_CONTEXT_INTAKE",
        tool_fields=(
            "result_chars", "read_kind", "read_targets", "write_targets",
            "is_reread",
        ),
    ),
    CollectionSpec(
        "error_text", "CLAUDIT_COLLECT_ERROR_TEXT",
        tool_fields=("error_text",),
    ),
    CollectionSpec(
        "file_extras", "CLAUDIT_COLLECT_FILE_EXTRAS",
        file_fields=("models", "prompt_count", "turn_count"),
    ),
    CollectionSpec(
        "tool_error_kinds", "CLAUDIT_COLLECT_TOOL_ERROR_KINDS",
        tool_fields=("error_kind",),
        rollup_tables=("tool_error_rollup",),
    ),
    CollectionSpec(
        "dispatch_analytics", "CLAUDIT_COLLECT_DISPATCH_ANALYTICS",
        tool_fields=("agent_model", "dispatch_prompt_chars", "dispatch_brief_ref"),
        rollup_tables=("dispatch_rollup", "dispatch_brief_rollup"),
    ),
    CollectionSpec(
        "generation_speed", "CLAUDIT_COLLECT_GENERATION_SPEED",
        record_fields=("generation_s", "output_tokens_per_s"),
    ),
)


FEATURE_SPECS = (
    FeatureSpec("usage_timeseries", "CLAUDIT_FEATURE_USAGE_TIMESERIES"),
    FeatureSpec("thinking_output", "CLAUDIT_FEATURE_THINKING_OUTPUT"),
    FeatureSpec("line_churn", "CLAUDIT_FEATURE_LINE_CHURN"),
    FeatureSpec("model_comparison", "CLAUDIT_FEATURE_MODEL_COMPARISON"),
    FeatureSpec("token_breakdown", "CLAUDIT_FEATURE_TOKEN_BREAKDOWN"),
    FeatureSpec("project_comparison", "CLAUDIT_FEATURE_PROJECT_COMPARISON"),
    FeatureSpec("cache_ttl", "CLAUDIT_FEATURE_CACHE_TTL"),
    FeatureSpec("response_sizes", "CLAUDIT_FEATURE_RESPONSE_SIZES"),
    FeatureSpec("context_growth", "CLAUDIT_FEATURE_CONTEXT_GROWTH"),
    FeatureSpec("burn_rate", "CLAUDIT_FEATURE_BURN_RATE"),
    FeatureSpec("context_comparison", "CLAUDIT_FEATURE_CONTEXT_COMPARISON"),
    FeatureSpec("tool_usage", "CLAUDIT_FEATURE_TOOL_USAGE"),
    FeatureSpec("reply_latency", "CLAUDIT_FEATURE_REPLY_LATENCY"),
    FeatureSpec("page_performance", "CLAUDIT_FEATURE_PAGE_PERFORMANCE"),
    FeatureSpec("tool_error_rate", "CLAUDIT_FEATURE_TOOL_ERROR_RATE"),
    FeatureSpec("agent_comparison", "CLAUDIT_FEATURE_AGENT_COMPARISON"),
    FeatureSpec("activity_heatmap", "CLAUDIT_FEATURE_ACTIVITY_HEATMAP"),
    FeatureSpec("summary", "CLAUDIT_FEATURE_SUMMARY"),
    FeatureSpec("rate_limits", "CLAUDIT_FEATURE_RATE_LIMITS"),
    FeatureSpec("sessions", "CLAUDIT_FEATURE_SESSIONS"),
    FeatureSpec("cache", "CLAUDIT_FEATURE_CACHE"),
    FeatureSpec("inspector", "CLAUDIT_FEATURE_INSPECTOR"),
    FeatureSpec("project_picker", "CLAUDIT_FEATURE_PROJECT_PICKER"),
    FeatureSpec("export_png", "CLAUDIT_FEATURE_EXPORT_PNG"),
    FeatureSpec("live_updates", "CLAUDIT_FEATURE_LIVE_UPDATES"),
)
