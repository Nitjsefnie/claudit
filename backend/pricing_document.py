"""Shared compact and effective shapes for the pricing document."""
from __future__ import annotations

import copy
import json

from backend.pricing_rows import decode_pricing_doc, encode_pricing_doc

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
    yield from doc.get("models", {}).values()
    for hosts in doc.get("providers", {}).values():
        yield from hosts.values()


def expand_pricing_doc(doc: dict) -> dict:
    """Copy a document into its complete in-memory rate and timestamp shape."""
    out = (decode_pricing_doc(doc) if "_format" in doc
           else copy.deepcopy(doc))
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
    out = (decode_pricing_doc(doc) if "_format" in doc
           else copy.deepcopy(doc))
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


def _serialize_provider_map(providers: dict, indent: int = 2) -> str:
    """Indent model/host maps, keeping each complete storage row on one line."""
    fields = []
    for key, value in sorted(providers.items()):
        if isinstance(value, dict):
            rendered = _serialize_provider_map(value, indent + 2)
        else:
            rows = [" " * (indent + 4) + json.dumps(
                row, separators=(",", ":"), ensure_ascii=False,
                sort_keys=True, allow_nan=False) for row in value]
            rendered = "[\n" + ",\n".join(rows) + "\n" + " " * (indent + 2) + "]"
        fields.append(" " * (indent + 2) + json.dumps(key) + ": " + rendered)
    return ("{\n" + ",\n".join(fields) + "\n" + " " * indent + "}"
            if fields else "{}")


def serialize_pricing_doc(doc: dict) -> str:
    """Serialize storage rows compactly within the sorted, indented document."""
    stored = encode_pricing_doc(compact_pricing_doc(doc))
    fields = []
    for key, value in sorted(stored.items()):
        rendered = (_serialize_provider_map(value) if key == "providers"
                    else json.dumps(value, indent=2, sort_keys=True,
                                    allow_nan=False).replace("\n", "\n  "))
        fields.append("  " + json.dumps(key) + ": " + rendered)
    return "{\n" + ",\n".join(fields) + "\n}\n"


def check_compact_shape(doc: dict, decoded: dict | None = None) -> None:
    """Refuse a STORED document whose cache-write rates repeat ``fresh``.

    ``compact_pricing_doc`` strips an equal ``create_5m``/``create_1h``
    pair at every write, so a present equal pair in the stored file is a
    hand edit (SV-RATE-DATA: present only when it differs). This runs at
    the raw-file boundaries only — pricing_load's module load and the
    browser loader's fetch, and when ``load_tables`` receives stored rows.
    Decoded inputs still accept the expanded intermediate materialised by
    ``expand_pricing_doc``. Pass the already-decoded document at boundaries
    to avoid decoding twice. Mirrored by src/pricing-loader.js's
    ``checkCompactShape``.
    """
    if decoded is None:
        decoded = decode_pricing_doc(doc)
    models = decoded.get("models")
    providers = decoded.get("providers")
    rows = list(models.items()) if isinstance(models, dict) else []
    if isinstance(providers, dict):
        rows += [(f"{model} via {host}", entries)
                 for model, hosts in providers.items()
                 if isinstance(hosts, dict)
                 for host, entries in hosts.items()]
    for where, entries in rows:
        if not isinstance(entries, list):
            continue
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            at = f"{where}[{i}]"
            spots = [(at, entry)]
            schedule = entry.get("schedule")
            if isinstance(schedule, list):
                spots += [(f"{at}.schedule[{j}]", window.get("rates"))
                          for j, window in enumerate(schedule)
                          if isinstance(window, dict)]
            for spot, rates in spots:
                if not isinstance(rates, dict):
                    continue
                for field in CACHE_WRITE_FIELDS:
                    if (field in rates and "fresh" in rates
                            and rates[field] == rates["fresh"]):
                        raise ValueError(
                            f"{spot}: {field} is present and equals fresh; "
                            "it is present only when it differs")


def check_dual_pin_tags(openrouter: dict) -> None:
    """Refuse a key pinned in BOTH resolve tables whose tag sets differ
    (SV-RATE-DATA, issue #931): the vendor pin and the tracked entry's
    own pins name one endpoint, so when both sides carry tags they must
    agree. A pin without a tag (cheapest/ignore shapes) is not compared,
    nor is `why`, which legitimately differs between the two tables.
    Called from pricing_load._vendor_tables; mirrored by
    src/vendor-tables.js's buildVendorTables."""
    vendor = openrouter.get("vendor")
    tracked = openrouter.get("models")
    vendor_resolve = vendor.get("resolve") if isinstance(vendor, dict) else None
    if not isinstance(tracked, dict):
        return
    for key, entry in tracked.items():
        if not isinstance(entry, dict):
            continue
        where = f"openrouter.models[{key!r}]"
        vendor_pin = (vendor_resolve.get(key)
                      if isinstance(vendor_resolve, dict) else None)
        model_pins = entry.get("resolve")
        vendor_tags = ({vendor_pin["tag"]} if isinstance(vendor_pin, dict)
                       and "tag" in vendor_pin else set())
        model_tags = ({pin["tag"] for pin in model_pins.values()
                       if isinstance(pin, dict) and "tag" in pin}
                      if isinstance(model_pins, dict) else set())
        if vendor_tags and model_tags and vendor_tags != model_tags:
            raise ValueError(
                f"pricing.json: {where}: vendor resolve tags "
                f"{sorted(vendor_tags)} differ from the model's resolve "
                f"tags {sorted(model_tags)}")
