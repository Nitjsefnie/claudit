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
    if "long_context_models" in doc:
        raise ValueError(
            "pricing.json: long_context_models is removed; use long_context_meters")
    if "long_context_meters" not in doc:
        raise ValueError("pricing.json: long_context_meters is missing")
    groups = doc["long_context_meters"]
    if not isinstance(groups, list):
        raise ValueError("long_context_meters: not a list of groups")

    models = doc["models"]
    tracked = (doc.get("openrouter") or {}).get("models") or {}
    folded: dict[str, dict] = {}
    thresholds: set[int] = set()
    for index, group in enumerate(groups):
        where = f"long_context_meters[{index}]"
        if not isinstance(group, dict):
            raise ValueError(f"{where}: group is not an object")
        unknown = set(group) - {"threshold", "models"}
        if unknown:
            raise ValueError(f"{where}: unknown field {sorted(unknown)[0]!r}")
        if "threshold" not in group:
            raise ValueError(f"{where}: no positive integer threshold")
        threshold = group["threshold"]
        try:
            integral = (not isinstance(threshold, bool)
                        and isinstance(threshold, (int, float))
                        and math.isfinite(float(threshold))
                        and float(threshold).is_integer()
                        and threshold > 0)
        except (OverflowError, ValueError):
            integral = False
        if not integral:
            raise ValueError(f"{where}: no positive integer threshold")
        threshold = int(threshold)
        if threshold in thresholds:
            raise ValueError(
                f"long_context_meters: duplicate threshold {threshold}")
        thresholds.add(threshold)

        entries = group.get("models")
        if not isinstance(entries, list) or not entries:
            raise ValueError(f"{where}: models is empty or not a list")
        for entry in entries:
            if isinstance(entry, str):
                key, factors = entry, {}
            elif isinstance(entry, dict) and len(entry) == 1:
                key, factors = next(iter(entry.items()))
                if not isinstance(key, str) or not key:
                    raise ValueError(
                        f"{where}: models entries need non-empty model keys")
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
            if key in folded:
                raise ValueError(
                    f"long_context_meters: {key!r} appears more than once")
            if key not in models and key not in tracked:
                raise ValueError(
                    f"long_context_meters: {key!r} names no models-table or tracked key")

            meter = {"threshold": threshold}
            for field in ("input_mult", "output_mult"):
                if field not in factors:
                    continue
                multiplier = factors[field]
                try:
                    finite = (isinstance(multiplier, (int, float))
                              and not isinstance(multiplier, bool)
                              and math.isfinite(float(multiplier)))
                except (OverflowError, ValueError):
                    finite = False
                if not finite or multiplier <= 0:
                    raise ValueError(
                        f"long_context_meters: {key!r} has invalid {field}; "
                        "expected a positive finite number")
                meter[field] = multiplier
            folded[key] = meter
    return folded
