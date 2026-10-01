"""Fixture builders for the refresh-provider-rates tests.

The endpoint, override, discount and per-token spellings the refresh
tests share: fixture payloads over the seeded view, never the network.
"""
from __future__ import annotations

import re
from decimal import Decimal


def _per_token(rate: float) -> str:
    """OpenRouter's spelling: USD per token, as a decimal string."""
    return format(Decimal(repr(rate)).scaleb(-6).normalize(), "f")


def _endpoint(host: str, rates: dict, discount: float = 0, tag: str = "") -> dict:
    """One endpoint as the API returns it, extra fields included. The two
    cache-write keys are listed only where the rates name a tier of their
    own, which is how OpenRouter splits a 1h write from a 5m one."""
    pricing = {
        "prompt": _per_token(rates["fresh"]),
        "completion": _per_token(rates["output"]),
        "input_cache_read": _per_token(rates["read"]),
        "discount": discount,
    }
    write = rates.get("create_5m", rates["fresh"])
    write_1h = rates.get("create_1h", write)
    if write != rates["fresh"]:
        pricing["input_cache_write"] = _per_token(write)
    if write_1h != write:
        pricing["input_cache_write_1h"] = _per_token(write_1h)
    return {
        "name": f"{host} | fixture", "provider_name": host,
        "tag": tag or host.lower(), "quantization": "fp8", "status": 0,
        "context_length": 131072, "uptime_last_30m": 100,
        "pricing": pricing,
    }


def _overrides(schedule: list) -> list:
    """An entry schedule as OpenRouter lists it (pricing.overrides)."""
    out = []
    for window in schedule:
        override = {"prompt": _per_token(window["rates"]["fresh"]),
                    "completion": _per_token(window["rates"]["output"]),
                    "input_cache_read": _per_token(window["rates"]["read"])}
        if "days" in window:
            override["utc_days"] = window["days"]
        if "start" in window:
            override["utc_start"], override["utc_end"] = window["start"], window["end"]
        out.append(override)
    return out


def _discount(entry: dict) -> float:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)% off", entry.get("note", ""))
    return float(m.group(1)) / 100 if m else 0
