"""The grouped long-context meter table, checked.

``long_context_meters`` groups model keys by threshold and carries optional
per-model factors. The loader folds it to the complete per-model entries
used by pricing and repricing; the JSON does not keep a second membership
list.
"""
from __future__ import annotations

import math


def _long_context_meters(doc: dict) -> dict[str, dict]:
    """Validate grouped meter data and fold it to key -> whole meter entry."""
    groups = _meter_groups(doc)
    models = doc["models"]
    tracked = (doc.get("openrouter") or {}).get("models") or {}
    folded: dict[str, dict] = {}
    thresholds: set[int] = set()
    for index, group in enumerate(groups):
        where = f"long_context_meters[{index}]"
        threshold, entries = _meter_group(group, where, thresholds)
        for entry in entries:
            key, factors = _meter_entry(entry, where)
            _validate_meter_key(key, folded, models, tracked)
            folded[key] = _meter(key, threshold, factors)
    return folded


def _meter_groups(doc: dict) -> list:
    """Read the grouped meter field and reject the retired membership shape."""
    if "long_context_models" in doc:
        raise ValueError(
            "pricing.json: long_context_models is removed; use long_context_meters")
    if "long_context_meters" not in doc:
        raise ValueError("pricing.json: long_context_meters is missing")
    groups = doc["long_context_meters"]
    if not isinstance(groups, list):
        raise ValueError("long_context_meters: not a list of groups")
    return groups


def _meter_group(group: object, where: str,
                 thresholds: set[int]) -> tuple[int, list]:
    """Validate one threshold group and return its threshold and entries."""
    if not isinstance(group, dict):
        raise ValueError(f"{where}: group is not an object")
    unknown = set(group) - {"threshold", "models"}
    if unknown:
        raise ValueError(f"{where}: unknown field {sorted(unknown)[0]!r}")
    if "threshold" not in group:
        raise ValueError(f"{where}: no positive integer threshold")
    raw_threshold = group["threshold"]
    try:
        integral = (not isinstance(raw_threshold, bool)
                    and isinstance(raw_threshold, (int, float))
                    and math.isfinite(float(raw_threshold))
                    and float(raw_threshold).is_integer()
                    and raw_threshold > 0)
    except (OverflowError, ValueError):
        integral = False
    if not integral:
        raise ValueError(f"{where}: no positive integer threshold")
    threshold = int(raw_threshold)
    if threshold in thresholds:
        raise ValueError(f"long_context_meters: duplicate threshold {threshold}")
    thresholds.add(threshold)

    entries = group.get("models")
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{where}: models is empty or not a list")
    return threshold, entries


def _meter_entry(entry: object, where: str) -> tuple[str, dict]:
    """Read a bare model key or its one-key multiplier object."""
    if isinstance(entry, str):
        key, factors = entry, {}
    elif isinstance(entry, dict) and len(entry) == 1:
        key, factors = next(iter(entry.items()))
        if not isinstance(key, str) or not key:
            raise ValueError(f"{where}: models entries need non-empty model keys")
        if not isinstance(factors, dict):
            raise ValueError(
                f"long_context_meters: {key!r} multipliers are not an object")
        unknown = set(factors) - {"input_mult", "output_mult"}
        if unknown:
            raise ValueError(
                f"long_context_meters: {key!r} unknown field "
                f"{sorted(unknown)[0]!r}")
    else:
        raise ValueError(
            f"{where}: each model is a key or a one-key multiplier object")
    if not key:
        raise ValueError(f"{where}: models entries need non-empty model keys")
    return key, factors


def _validate_meter_key(key: str, folded: dict, models: dict,
                        tracked: dict) -> None:
    """Reject duplicate meter entries and keys absent from either rate table."""
    if key in folded:
        raise ValueError(f"long_context_meters: {key!r} appears more than once")
    if key not in models and key not in tracked:
        raise ValueError(
            f"long_context_meters: {key!r} names no models-table or tracked key")


def _meter(key: str, threshold: int, factors: dict) -> dict:
    """Build a model's meter after checking any non-default multipliers."""
    meter: dict[str, int | float] = {"threshold": threshold}
    for field in ("input_mult", "output_mult"):
        if field not in factors:
            continue
        multiplier = factors[field]
        if (not isinstance(multiplier, (int, float))
                or isinstance(multiplier, bool)):
            raise ValueError(
                f"long_context_meters: {key!r} has invalid {field}; "
                "expected a positive finite number")
        try:
            finite = math.isfinite(float(multiplier))
        except (OverflowError, ValueError):
            finite = False
        if not finite or multiplier <= 0:
            raise ValueError(
                f"long_context_meters: {key!r} has invalid {field}; "
                "expected a positive finite number")
        meter[field] = multiplier
    return meter
