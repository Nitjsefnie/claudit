"""Fixture builders for the refresh-provider-rates tests.

The endpoint, override, discount and per-token spellings the refresh
tests share: fixture payloads over the seeded view, never the network.
"""
from __future__ import annotations

import copy
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


# The default estimate's seed row (issue #858): frozen synthetic rates at
# no cutover — the one claude-opus-4-7 literal in the repo. The loaders
# require it on every loadable document (SV-RATE-DATA); a site that
# mutates the row copies it (dict(DEFAULT_ROW)) so this source stays
# clean.
DEFAULT_ROW = {"from": None, "fresh": 5.0, "create_5m": 6.25,
               "create_1h": 10.0, "read": 0.5, "output": 25.0}

# Shared synthetic rate vectors; these stay five-field dictionaries without
# `from`, so callers can preserve each loader's exact row shape.
RATES_A = {"fresh": 1.0, "create_5m": 1.25, "create_1h": 2.0,
           "read": 0.1, "output": 5.0}
RATES_B = {"fresh": 2.0, "create_5m": 2.5, "create_1h": 4.0,
           "read": 0.2, "output": 10.0}
RATE_C = {"fresh": 0.40, "create_5m": 0.40, "create_1h": 0.40,
          "read": 0.030, "output": 0.900}


def seed_doc(*, members=None, meters=None, models=None, tracked=None,
             providers=None, resolve=None, prefixes=None,
             fetched="2030-01-01T00:00:00Z") -> dict:
    """The minimal loadable seed document (issue #858): every section the
    loaders demand plus the default estimate's row. Callers add their own
    rows through the keyword arguments — extra models-table rows, tracked
    entries, provider rows, vendor resolve pins, the prefix list, the
    per-model meter map, the fetch stamp — and every doc they build loads
    for the same reason."""
    return {
        "long_context_models": list(members or []),
        "long_context_meters": dict(meters or {}),
        "models": {"claude-opus-4-7": [dict(DEFAULT_ROW)],
                   **(copy.deepcopy(models) if models else {})},
        "openrouter": {"data_region": "global",
                       "models": copy.deepcopy(tracked) if tracked else {},
                       "vendor": {"resolve": resolve or {},
                                  "prefixes": list(prefixes) if prefixes
                                  else ["anthropic", "openai",
                                        "moonshotai", "z-ai"]}},
        "provider_rates_fetched": fetched,
        "providers": copy.deepcopy(providers) if providers else {},
    }
