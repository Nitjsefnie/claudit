"""Validate and rebuild vendor namespace keys from preserved table values."""
from __future__ import annotations

from refresh_prices import RefreshError

from backend.pricing_load import bare_form, is_vendor_namespace


def _mapping(name: str, table: dict, prefixes: list[str], changes: dict) -> dict:
    destinations: dict[str, str] = {}
    for key in table:
        bare = bare_form(key, prefixes)
        if bare_form(bare, prefixes) != bare:
            raise RefreshError(f"{name}: {key!r} cannot rekey to {bare!r}: "
                               "destination still carries a tracked prefix")
        bare = bare or key
        if bare in destinations:
            raise RefreshError(f"{name}: {destinations[bare]!r} and {key!r} "
                               f"both rekey to {bare!r}")
        destinations[bare] = key
        if key != bare:
            changes[key] = bare
    return {bare_form(key, prefixes) or key: value for key, value in table.items()}


def rekey(doc: dict) -> dict[str, str]:
    """Check all destinations before changing any table; return old→new keys."""
    router = doc.get("openrouter")
    if not isinstance(router, dict):
        raise RefreshError("openrouter is missing or not a mapping")
    vendor = router.get("vendor")
    if not isinstance(vendor, dict):
        raise RefreshError("openrouter.vendor is missing or not a mapping")
    prefixes = vendor.get("prefixes")
    if (not isinstance(prefixes, list) or not prefixes
            or not all(is_vendor_namespace(prefix) for prefix in prefixes)
            or len(set(prefixes)) != len(prefixes)):
        raise RefreshError("openrouter.vendor.prefixes must be a non-empty list "
                           "of distinct lowercase [a-z0-9-] namespace strings")
    groups = doc.get("long_context_meters", [])
    tables = [("openrouter.models", router["models"]),
              ("providers", doc["providers"]),
              ("openrouter.vendor.resolve", vendor.get("resolve", {})),
              ("long_context_meters", {
                  entry if isinstance(entry, str) else next(iter(entry)): None
                  for group in groups for entry in group["models"]})]
    changes: dict[str, str] = {}
    mappings = [(table, _mapping(name, table, prefixes, changes))
                for name, table in tables]
    members = [[(changes.get(entry, entry) if isinstance(entry, str) else
                 {changes.get(key, key): value for key, value in entry.items()})
                for entry in group["models"]] for group in groups]
    for table, mapping in mappings[:3]:
        table.clear()
        table.update(mapping)
    for group, entries in zip(groups, members):
        group["models"] = entries
    return changes
