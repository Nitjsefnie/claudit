"""The long-context meter's membership and per-model threshold tables
(issue #765), checked. Split from backend/pricing_load.py — whose module
size is a ratcheted ceiling — the same checked-table shape the rest of
that loader carries; pricing_load imports the two builders back and
pricing re-exports their values. Pure functions over the document; no
rate table is read here."""
from __future__ import annotations


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


def _long_context_meters(doc: dict) -> dict[str, int]:
    """The meter's per-model thresholds (issue #765), checked: a map of
    member keys to exactly {"threshold": N} for a positive integral N —
    an integral float spelling (200000.0) folds to the integer, so both
    loaders accept the same bytes where JSON has already collapsed the
    spelling (the browser's Number sees one value); a fractional one is
    refused. A member absent here keeps the meter's global threshold.
    The key must be a member — a threshold for a model that does not
    meter is inert data the loaders refuse."""
    meters = doc.get("long_context_meters") or {}
    if not isinstance(meters, dict):
        raise ValueError("long_context_meters: not a map of member keys "
                         "to thresholds")
    members = _long_context_members(doc)
    for key, value in meters.items():
        if key not in members:
            raise ValueError(
                f"long_context_meters: {key!r} names no long_context_models "
                "member")
        if not isinstance(value, dict) or set(value) != {"threshold"}:
            raise ValueError(
                f"long_context_meters: {key!r} is not a {{threshold: "
                "positive integer}}")
        threshold = value["threshold"]
        if (isinstance(threshold, bool)
                or not isinstance(threshold, (int, float))
                or not float(threshold).is_integer() or threshold <= 0):
            raise ValueError(
                f"long_context_meters: {key!r} is not a {{threshold: "
                "positive integer}}")
    return {k: int(v["threshold"]) for k, v in meters.items()}
