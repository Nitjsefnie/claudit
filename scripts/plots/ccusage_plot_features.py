"""Effective PNG policy: authoritative server keys or standalone startup env."""
from __future__ import annotations

import json
import os
from collections.abc import Collection

from backend.flag_registry import FEATURE_SPECS
from backend.flags import resolve_config

DRAWABLE_FEATURES = (
    'usage_timeseries', 'model_comparison', 'token_breakdown', 'burn_rate',
)
_KNOWN_FEATURES = frozenset(spec.key for spec in FEATURE_SPECS)


def parse_disabled_features(value: str) -> frozenset[str]:
    """Validate the closed vocabulary carried by one JSON argv element."""
    try:
        keys = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError('disabled features must be a JSON array') from exc
    if not isinstance(keys, list) or any(not isinstance(key, str) for key in keys):
        raise ValueError('disabled features must be an array of string keys')
    unknown = set(keys) - _KNOWN_FEATURES
    if unknown:
        raise ValueError(f'unknown disabled features: {", ".join(sorted(unknown))}')
    return frozenset(keys)


def resolve_features(explicit: frozenset[str] | None) -> frozenset[str]:
    """An explicit list wins, including an empty list; no child-env merging."""
    if explicit is not None:
        return explicit
    return frozenset(resolve_config(os.environ).disabled_features())


def has_drawable_features(disabled: Collection[str]) -> bool:
    """Summary text and rate overlays alone cannot produce a PNG."""
    return any(key not in disabled for key in DRAWABLE_FEATURES)
