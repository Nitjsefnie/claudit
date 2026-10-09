"""Shared compact and effective shapes for the pricing document."""
from __future__ import annotations

import copy
import json

RATE_FIELDS = ("fresh", "create_5m", "create_1h", "read", "output")
CACHE_WRITE_FIELDS = ("create_5m", "create_1h")


def effective_rates(rates: dict) -> dict:
    """Return the complete five-rate vector, defaulting cache writes to fresh."""
    fresh = rates["fresh"]
    return {field: rates.get(field, fresh) if field in CACHE_WRITE_FIELDS
            else rates[field] for field in RATE_FIELDS}


def omit_default_rates(rates: dict) -> dict:
    """Copy rates while omitting cache-write values equal to fresh."""
    out = dict(rates)
    if "fresh" not in out:
        return out
    for field in CACHE_WRITE_FIELDS:
        if out.get(field) == out["fresh"]:
            out.pop(field, None)
    return out


def effective_schedule(schedule: list | None) -> list | None:
    """Return schedule windows with every cache-write rate made explicit."""
    if schedule is None:
        return None
    return [{**window, "rates": effective_rates(window["rates"])}
            for window in schedule]


def _walk_histories(doc: dict):
    for history in doc.get("models", {}).values():
        yield history
    for hosts in doc.get("providers", {}).values():
        for history in hosts.values():
            yield history


def expand_pricing_doc(doc: dict) -> dict:
    """Copy a document into its complete in-memory rate and timestamp shape."""
    out = copy.deepcopy(doc)
    for history in _walk_histories(out):
        for index, entry in enumerate(history):
            if index == 0:
                entry.setdefault("from", None)
            entry.update(effective_rates(entry))
            if "schedule" in entry:
                entry["schedule"] = effective_schedule(entry["schedule"])
            band = entry.get("band")
            if isinstance(band, dict) and "fresh" in band:
                for field in CACHE_WRITE_FIELDS:
                    band.setdefault(field, list(band["fresh"]))
    return out


def compact_pricing_doc(doc: dict) -> dict:
    """Copy a document, omitting cache tiers and leading null timestamps."""
    out = copy.deepcopy(doc)
    for history in _walk_histories(out):
        for index, entry in enumerate(history):
            if index == 0 and entry.get("from") is None:
                entry.pop("from", None)
            compacted = omit_default_rates(entry)
            entry.clear()
            entry.update(compacted)
            for window in entry.get("schedule", []):
                compacted_rates = omit_default_rates(window["rates"])
                window["rates"].clear()
                window["rates"].update(compacted_rates)
            band = entry.get("band")
            if isinstance(band, dict) and "fresh" in band:
                for field in CACHE_WRITE_FIELDS:
                    if band.get(field) == band["fresh"]:
                        band.pop(field, None)
    return out


def serialize_pricing_doc(doc: dict) -> str:
    """Serialize the canonical compact document used by every writer."""
    return json.dumps(compact_pricing_doc(doc), indent=2, sort_keys=True,
                      allow_nan=False) + "\n"
