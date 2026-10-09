"""The long-context meter's membership and per-model threshold tables
(issue #765), checked. Split from backend/pricing_load.py — whose module
size is a ratcheted ceiling — the same checked-table shape the rest of
that loader carries; pricing_load imports the two builders back and
pricing re-exports their values. Pure functions over the document; no
rate table is read here."""
from __future__ import annotations

import math


def _long_context_members(doc: dict) -> frozenset[str]:
    """The long-context meter's membership, checked: distinct non-empty
    strings, each naming a models-table key OR a tracked key (the meter
    rides whichever table's rate rows the id prices through, so a name
    with no row in either is a typo the loader refuses)."""
    if "long_context_models" not in doc:
        raise ValueError("pricing.json: long_context_models is missing")
    members = doc["long_context_models"]
    if (not isinstance(members, list)
            or not all(isinstance(k, str) and k for k in members)
            or len(set(members)) != len(members)):
        raise ValueError(
            "long_context_models: not a list of distinct non-empty keys")
    tracked = (doc.get("openrouter") or {}).get("models") or {}
    unknown = [k for k in members
               if k not in doc["models"] and k not in tracked]
    if unknown:
        raise ValueError(
            f"long_context_models: {unknown} name no models-table or tracked key")
    return frozenset(members)


def _long_context_meters(doc: dict) -> dict[str, dict]:
    """The meter's per-model entries, checked: a map of member keys to
    {threshold, input_mult?, output_mult?}. The threshold is a positive
    integer; an integral float spelling folds to an int so both loaders
    accept the same JSON number. Multipliers, when present, are finite
    positive numbers. A member absent here keeps the global defaults.
    The key must be a member — a meter for a model that does not meter is
    inert data the loaders refuse."""
    meters = doc.get("long_context_meters") or {}
    if not isinstance(meters, dict):
        raise ValueError("long_context_meters: not a map of member keys "
                         "to meter entries")
    members = _long_context_members(doc)
    folded: dict[str, dict] = {}
    for key, value in meters.items():
        if key not in members:
            raise ValueError(
                f"long_context_meters: {key!r} names no long_context_models "
                "member")
        if (not isinstance(value, dict) or "threshold" not in value
                or set(value) - {"threshold", "input_mult", "output_mult"}):
            raise ValueError(
                f"long_context_meters: {key!r} is not a meter entry with "
                "a positive integer threshold and optional positive "
                "finite multipliers")
        threshold = value["threshold"]
        try:
            integral = (not isinstance(threshold, bool)
                        and isinstance(threshold, (int, float))
                        and math.isfinite(float(threshold))
                        and float(threshold).is_integer()
                        and threshold > 0)
        except (OverflowError, ValueError):
            integral = False
        if not integral:
            raise ValueError(
                f"long_context_meters: {key!r} has no positive integer "
                "threshold")
        entry = {**value, "threshold": int(threshold)}
        for field in ("input_mult", "output_mult"):
            if field not in entry:
                continue
            multiplier = entry[field]
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
        folded[key] = entry
    return folded
