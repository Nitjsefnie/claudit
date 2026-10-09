#!/usr/bin/env python3
"""Normalise an OpenRouter endpoint's listed price to the refresh's shapes.

The five token rates an entry carries (fresh, create_5m, create_1h, read,
output), its optional USD-per-search rate, and the entry schedule from
pricing.overrides become provider history, plus the run's error types. A
price or override kind not modelled here refuses its host: a pricing key
outside PRICED (an image price), or an override kind outside
_OVERRIDE_KEYS, is a RefreshError, which appends nothing (SV-RATE-REFRESH). A coherent
`min_prompt_tokens` band yields its own per-model meter factors and
threshold; its rates never enter a provider row. The provider refresh
also uses this module for endpoint tag/region normalisation and weekly
schedule coverage checks for first-seen hosts.

Cache writes take the listed write price when it is nonzero, the input
rate otherwise; no listed cache-read price is 0. OpenRouter lists USD per
token as a decimal string and the table is USD per million tokens, so
Decimal keeps "0.0000001275" exactly 0.1275.
"""
from __future__ import annotations

import math
import re
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
# pylint: disable=wrong-import-position
from backend import pricing  # noqa: E402

# Token prices and the per-search price this script models. Any other
# pricing key at a nonzero price refuses its host.
TOKEN_PRICED = ("prompt", "completion", "input_cache_read",
                "input_cache_write", "input_cache_write_1h")
PRICED = (*TOKEN_PRICED, "web_search")
_OVERRIDE_KEYS = frozenset({"utc_days", "utc_start", "utc_end", *TOKEN_PRICED})
_UTC_KEYS = frozenset({"utc_days", "utc_start", "utc_end"})

# The long-context band OpenRouter lists (a `min_prompt_tokens` override).
# The band names its own threshold; its coherent price ratios yield the
# model's input/output factors. `min_prompt_tokens` names the band; the
# other three keys are the utc fields a weekly window carries.
MIN_PROMPT_TOKENS = "min_prompt_tokens"


def meter_shape_ok(base: dict, band_rates: dict,
                   where: str = "long-context band",
                   band: dict | None = None) -> tuple[float, float]:
    """Return the band's input/output price ratios, rounded to ten
    decimal places. The input ratio is derived from fresh input and then
    applies to fresh, both write TTLs, and cache reads alike. A band
    departing from global defaults is still valid; the vendor fold writes
    its derived factors.
    """
    if base["fresh"] == 0:
        if band_rates["fresh"] != 0:
            raise Untracked(
                f"{where}: input factor cannot be inferred from a zero base rate")
        input_mult = pricing.LONG_CONTEXT_INPUT_MULT
    else:
        input_mult = round(band_rates["fresh"] / base["fresh"], 10)
    if not math.isfinite(input_mult) or input_mult <= 0:
        raise Untracked(f"{where}: input multiplier is not positive and finite")

    # The meter applies one input factor to fresh tokens, cache writes, and
    # cache reads. Preserve every cache price explicitly listed by the band
    # only when that same factor represents it at the comparison precision.
    cache_fields = (
        ("input_cache_read", "read", "read"),
        ("input_cache_write", "create_5m", "5m write"),
        ("input_cache_write_1h", "create_1h", "1h write"),
    )
    if band is not None:
        for key, field, label in cache_fields:
            if key not in band:
                continue
            base_rate = base[field]
            band_rate = band_rates[field]
            expected = round(base_rate * input_mult, 10)
            if round(band_rate, 10) == expected:
                continue
            implied = (f"x{round(band_rate / base_rate, 10):g}"
                       if base_rate else "undefined (zero base rate)")
            raise Untracked(
                f"{where}: cache {label} is not representable by one input "
                f"factor: base {base_rate:g}, band {band_rate:g}, implied "
                f"factor {implied} (meter factor x{input_mult:g})")

    if base["output"] == 0:
        if band_rates["output"] != 0:
            raise Untracked(
                f"{where}: output factor cannot be inferred from a zero base rate")
        output_mult = pricing.LONG_CONTEXT_OUTPUT_MULT
    else:
        output_mult = round(band_rates["output"] / base["output"], 10)
    if not math.isfinite(output_mult) or output_mult <= 0:
        raise Untracked(f"{where}: output multiplier is not positive and finite")
    return input_mult, output_mult


def band_threshold(band: dict, where: str) -> int:
    """The band's own threshold, checked: a positive integer."""
    value = band[MIN_PROMPT_TOKENS]
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise Untracked(f"{where}: band threshold {value!r} is not a "
                        "positive integer")
    return value


def metered_band(price: dict, where: str,
                 bands: list[dict]) -> dict | None:
    """Fold one representable `min_prompt_tokens` band to its meter entry."""
    if not bands:
        return None
    if len(bands) > 1:
        raise Untracked(f"{where}: {len(bands)} long-context bands are not modelled")
    band = bands[0]
    if set(band) & _UTC_KEYS:
        raise Untracked(f"{where}: a band and utc fields on one override is not modelled")
    unknown = set(band) - {*TOKEN_PRICED, MIN_PROMPT_TOKENS}
    if unknown:
        raise Untracked(f"{where}: band kind not modelled: {sorted(unknown)}")
    if not (isinstance(band.get("prompt"), str)
            and isinstance(band.get("completion"), str)):
        raise Untracked(f"{where}: the band does not restate input and output: not modelled")
    threshold = band_threshold(band, where)
    input_mult, output_mult = meter_shape_ok(
        rates_of(price, where), rates_of(band, where), where, band)
    return {"threshold": threshold, "input_mult": input_mult,
            "output_mult": output_mult}


# An endpoint tag is `host` or `host/<suffix>[/<suffix>...]`: quantizations
# and data regions. A region is one of these codes, alone or qualified by an
# area and number (us, us-east, us-east5, us-east-1), in any case.
QUANTIZATIONS = frozenset({"fp4", "fp6", "fp8", "fp16", "fp32", "bf16", "nvfp4",
                           "mxfp4", "int4", "int8", "awq", "gptq"})
REGIONS = ("global", "us", "eu", "europe", "uk", "ca", "au", "ap", "jp", "sg",
           "in", "br", "de", "fr", "nl", "kr", "cn", "hk", "tw", "me", "sa",
           "za", "asia", "apac", "emea", "latam")
_QUANTIZATION_RE = "|".join(
    re.escape(tag) for tag in sorted(QUANTIZATIONS, key=len, reverse=True))
REGION_RE = re.compile(
    rf"(?:{'|'.join(REGIONS)})(?:-(?!(?:{_QUANTIZATION_RE})$)"
    r"[a-z]+(?:[0-9]+|-[0-9]+)?)?", re.IGNORECASE)


def tag_region(tag: str) -> str | None:
    """The data region an endpoint tag names, or None for a global one."""
    for suffix in tag.split("/")[1:]:
        if REGION_RE.fullmatch(suffix):
            return suffix.lower()
    return None


def unknown_suffixes(tag: str) -> list[str]:
    return [suffix for suffix in tag.split("/")[1:]
            if not REGION_RE.fullmatch(suffix) and suffix.lower() not in QUANTIZATIONS]


class RefreshError(Exception):
    """A host, a model or a run a human must look at: it writes nothing."""


class Untracked(RefreshError):
    """A listed shape a refresh caller cannot model as rate data.

    The provider pass reports a host-level notice; the vendor pass turns
    an actionable first-party shape into a run refusal.
    """


def _per_million(price: object, where: str) -> float:
    """OpenRouter lists USD per token as a decimal string; the table is
    USD per million tokens. Decimal keeps "0.0000001275" exactly 0.1275."""
    try:
        value = Decimal(price) if isinstance(price, str) else None
    except InvalidOperation:
        value = None
    if value is None or not value.is_finite() or value < 0:
        raise RefreshError(f"{where}: price {price!r} is not a non-negative decimal string")
    return float(value.scaleb(6))


def is_zero(value: object) -> bool:
    try:
        return not isinstance(value, bool) and Decimal(str(value)) == 0
    except InvalidOperation:
        return False


def _per_search(value: object, where: str) -> float:
    """OpenRouter lists web_search in USD per search, not per token."""
    if value is None:
        return 0.0
    try:
        if isinstance(value, str):
            amount = Decimal(value)
        elif (isinstance(value, (int, float))
              and not isinstance(value, bool)):
            amount = Decimal(str(value))
        else:
            amount = None
    except InvalidOperation:
        amount = None
    if amount is None or not amount.is_finite() or amount < 0:
        raise RefreshError(
            f"{where}: web_search {value!r} is not a nonnegative decimal string")
    return float(amount)


def rates_of(price: dict, where: str) -> dict:
    """The token rates and optional per-search rate of one listing.

    Cache writes take the listed write
    price when it is nonzero, the input rate otherwise; no listed cache-read
    price is 0. The 1h write takes the listed 1h price when there is one,
    else the 5m tier: a listing that splits them states both."""
    fresh = _per_million(price.get("prompt"), where)
    output = _per_million(price.get("completion"), where)
    read = _per_million(price["input_cache_read"], where) if "input_cache_read" in price else 0.0
    write = (_per_million(price["input_cache_write"], where)
             if "input_cache_write" in price else 0.0)
    create = write or fresh
    write_1h = (_per_million(price["input_cache_write_1h"], where)
                if "input_cache_write_1h" in price else 0.0)
    rates = {"fresh": fresh, "create_5m": create,
             "create_1h": write_1h or create,
             "read": read, "output": output}
    search = _per_search(price.get("web_search"), where)
    if search:
        rates["web_search"] = search
    return rates


def token_rates_of(price: dict, where: str) -> dict:
    """The five token rates carried by OpenRouter's price log."""
    rates = rates_of(price, where)
    return {field: rates[field] for field in (
        "fresh", "create_5m", "create_1h", "read", "output")}


def as_listed(rates: dict) -> dict:
    """The pricing OpenRouter lists for `rates`: rates_of's inverse."""
    price = {key: format(Decimal(repr(rates[f])).scaleb(-6).normalize(), "f")
             for key, f in (("prompt", "fresh"), ("completion", "output"),
                            ("input_cache_read", "read"))}
    if rates["create_5m"] != rates["fresh"]:
        price["input_cache_write"] = format(
            Decimal(repr(rates["create_5m"])).scaleb(-6).normalize(), "f")
    if rates["create_1h"] != rates["create_5m"]:
        price["input_cache_write_1h"] = format(
            Decimal(repr(rates["create_1h"])).scaleb(-6).normalize(), "f")
    return price


def entry_schedule(price: dict, where: str) -> list | None:
    """The entry schedule for OpenRouter's pricing.overrides: weekly UTC
    windows (utc_days, utc_start/utc_end as HHMM), each with the prices it
    overrides; a price it does not name is the endpoint's own.

    A `min_prompt_tokens` override is the long-context band: it contributes
    no window or rates to this provider row. The vendor fold independently
    learns its threshold and factors from the first-party listing."""
    overrides = price.get("overrides")
    if overrides is None or overrides == []:
        return None
    if not isinstance(overrides, list) or not all(isinstance(o, dict) for o in overrides):
        raise RefreshError(f"{where}: pricing.overrides is not a list of windows")
    schedule, bands = [], []
    for override in overrides:
        if MIN_PROMPT_TOKENS in override:
            bands.append(override)
            continue
        unknown = set(override) - _OVERRIDE_KEYS
        if unknown:
            raise RefreshError(f"{where}: override kind not modelled: {sorted(unknown)}")
        window_price = {**{k: price[k] for k in TOKEN_PRICED if k in price},
                        **{k: override[k] for k in TOKEN_PRICED if k in override}}
        window: dict = {"rates": token_rates_of(window_price, where)}
        if override.get("utc_days") is not None:
            window["days"] = override["utc_days"]
        if (override.get("utc_start") is not None
                or override.get("utc_end") is not None):
            window["start"] = override.get("utc_start")
            window["end"] = override.get("utc_end")
        schedule.append(window)
    metered_band(price, where, bands)
    if not schedule:
        return None
    try:
        pricing._schedule(schedule, where)  # pylint: disable=protected-access
    except ValueError as exc:
        raise RefreshError(f"{where}: pricing.overrides: {exc}") from exc
    return schedule


def in_a_window(schedule: list, at: datetime) -> bool:
    # pylint: disable-next=protected-access
    return pricing._scheduled(pricing._schedule(schedule, "schedule"), at) is not None


def covers_week(schedule: list) -> bool:
    """Whether a weekly schedule leaves no instant of the week outside
    every window, by the loaders' own matching: every (day, minute) of one
    anchor week falls in a window. 10080 loader calls, a few ms."""
    windows = pricing._schedule(schedule, "schedule")  # pylint: disable=protected-access
    anchor = datetime(2026, 1, 5, tzinfo=timezone.utc)
    for minute in range(7 * 24 * 60):
        ts = anchor + timedelta(minutes=minute)
        # pylint: disable-next=protected-access
        if pricing._scheduled(windows, ts) is None:
            return False
    return True
