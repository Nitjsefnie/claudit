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


def _members(groups: list, changes: dict[str, str]) -> list[list]:
    destinations: dict[str, str] = {}
    members = []
    for index, group in enumerate(groups):
        where = f"long_context_meters[{index}]"
        if not isinstance(group, dict) or not isinstance(group.get("models"), list):
            raise RefreshError(f"{where} must be a mapping with a models list")
        entries = []
        for entry in group["models"]:
            if not (isinstance(entry, str)
                    or isinstance(entry, dict) and len(entry) == 1
                    and all(isinstance(key, str) for key in entry)):
                raise RefreshError(f"{where}.models entries must be strings "
                                   "or one-key mappings")
            key = entry if isinstance(entry, str) else next(iter(entry))
            new = changes.get(key, key)
            if new in destinations:
                raise RefreshError(f"long_context_meters: {destinations[new]!r} "
                                   f"and {key!r} both rekey to {new!r}")
            destinations[new] = key
            entries.append(entry if new == key else
                           new if isinstance(entry, str) else {new: entry[key]})
        members.append(entries)
    return members


def validate(doc: dict) -> None:
    """Validate table and member shapes before either decoding or rekeying."""
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
    for name, table in (("openrouter.models", router.get("models")),
                        ("providers", doc.get("providers")),
                        ("openrouter.vendor.resolve", vendor.get("resolve", {}))):
        if not isinstance(table, dict):
            raise RefreshError(f"{name} is missing or not a mapping")
    groups = doc.get("long_context_meters")
    if not isinstance(groups, list):
        raise RefreshError("long_context_meters must be a list of mappings")
    _members(groups, {})


def rekey(doc: dict) -> dict[str, str]:
    """Check all destinations before changing any table; return old→new keys."""
    validate(doc)
    router = doc["openrouter"]
    vendor = router["vendor"]
    prefixes = vendor["prefixes"]
    groups = doc["long_context_meters"]
    tables = [("openrouter.models", router["models"]),
              ("providers", doc["providers"]),
              ("openrouter.vendor.resolve", vendor.get("resolve", {}))]
    changes: dict[str, str] = {}
    mappings = [(table, _mapping(name, table, prefixes, changes))
                for name, table in tables]
    members = _members(groups, changes)
    for table, mapping in mappings:
        table.clear()
        table.update(mapping)
    for group, entries in zip(groups, members):
        group["models"] = entries
    return changes
