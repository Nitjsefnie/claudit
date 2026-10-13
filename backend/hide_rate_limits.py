"""Compatibility access to the inverse rate-limit display switch.

The legacy CLAUDIT_HIDE_RATE_LIMITS alias is resolved with the same
startup configuration as positive feature switches. It affects display
visibility only; rate-limit events remain collected and stored.
"""
from __future__ import annotations

from importlib import import_module


def is_truthy(value: str) -> bool:
    """Apply claudit's established env-switch grammar without trimming."""
    return value.lower() not in ("", "0", "false", "no")


def __getattr__(name: str) -> bool:
    """Expose the old module constant from the resolved startup config."""
    if name != "HIDE_RATE_LIMITS":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    config_module = import_module("backend.flags")
    return config_module.CONFIG.legacy_hide_rate_limits


def __dir__() -> list[str]:
    """Include the compatibility constant in module introspection."""
    return sorted((*globals(), "HIDE_RATE_LIMITS"))
