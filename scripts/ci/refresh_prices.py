#!/usr/bin/env python3
"""Normalise an OpenRouter endpoint's listed price to the refresh's shapes.

The five rates an entry carries (fresh, create_5m, create_1h, read,
output) and the entry schedule pricing.overrides becomes, plus the run's
error type. A price or override kind not modelled here refuses its host:
a pricing key outside PRICED listed at a nonzero price (a per-request
fee, an image price), or an override kind outside _OVERRIDE_KEYS, is a
RefreshError, which appends nothing (SV-RATE-REFRESH). The provider
refresh also uses this module for endpoint tag/region normalisation and
weekly schedule coverage checks for first-seen hosts.

Cache writes take the listed write price when it is nonzero, the input
rate otherwise; no listed cache-read price is 0. OpenRouter lists USD per
token as a decimal string and the table is USD per million tokens, so
Decimal keeps "0.0000001275" exactly 0.1275.
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
# pylint: disable=wrong-import-position
from backend import pricing  # noqa: E402

# The prices this script models. Any other pricing key listed at a nonzero
# price (a per-request fee, an image price) refuses its host, unless it is
# one of the RECORDED_FEES.
PRICED = ("prompt", "completion", "input_cache_read", "input_cache_write",
          "input_cache_write_1h")
# Per-request fees the token-rate table cannot express: the call costs
# something no token count can price. They do not refuse, because a fee
# that is RECORDED is not silently dropped — it is written into the row's
# note beside the rates it never enters. Every other unmodelled key at a
# nonzero price still refuses.
RECORDED_FEES = ("web_search",)
_OVERRIDE_KEYS = frozenset({"utc_days", "utc_start", "utc_end", *PRICED})

# An endpoint tag is `host` or `host/<suffix>[/<suffix>...]`: quantizations
# and data regions. A region is one of these codes, alone or qualified by an
# area and a number (us, us-east, us-east-1), in any case.
REGIONS = ("us", "eu", "europe", "uk", "ca", "au", "ap", "jp", "sg", "in", "br",
           "de", "fr", "nl", "kr", "cn", "hk", "tw", "me", "sa", "za", "asia",
           "apac", "emea", "latam")
REGION_RE = re.compile(rf"(?:{'|'.join(REGIONS)})(?:-[a-z]+(?:-[0-9]+)?)?",
                       re.IGNORECASE)
QUANTIZATIONS = frozenset({"fp4", "fp6", "fp8", "fp16", "fp32", "bf16", "nvfp4",
                           "mxfp4", "int4", "int8", "awq", "gptq"})


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


def fee_notes(price: dict, where: str) -> list[str]:
    """One note per RECORDED_FEE the listing prices: the fee, its unit and
    the reason the table cannot carry it. A fee is recorded, never refused,
    so an unmodelled cost is visible in the row instead of dropped. A fee at
    zero, or one the listing does not price, notes nothing; one that is not a
    nonnegative decimal string refuses the host, as any unmodelled key does."""
    notes = []
    for key in RECORDED_FEES:
        value = price.get(key)
        if value is None or is_zero(value):
            continue
        try:
            amount = Decimal(value) if isinstance(value, str) else None
        except InvalidOperation:
            amount = None
        if amount is None or not amount.is_finite() or amount < 0:
            raise RefreshError(
                f"{where}: fee {key} {value!r} is not a nonnegative decimal string")
        notes.append(f"{key} ${format(amount.normalize(), 'f')}/request not modelled: "
                     "per-request, unpriceable from token counts")
    return notes


def rates_of(price: dict, where: str) -> dict:
    """The five rates of one price. Cache writes take the listed write
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
    return {"fresh": fresh, "create_5m": create, "create_1h": write_1h or create,
            "read": read, "output": output}


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
    overrides; a price it does not name is the endpoint's own."""
    overrides = price.get("overrides")
    if overrides is None or overrides == []:
        return None
    if not isinstance(overrides, list) or not all(isinstance(o, dict) for o in overrides):
        raise RefreshError(f"{where}: pricing.overrides is not a list of windows")
    schedule = []
    for override in overrides:
        unknown = set(override) - _OVERRIDE_KEYS
        if unknown:
            raise RefreshError(f"{where}: override kind not modelled: {sorted(unknown)}")
        window: dict = {"rates": rates_of({**{k: price[k] for k in PRICED if k in price},
                                           **{k: override[k] for k in PRICED if k in override}},
                                          where)}
        if override.get("utc_days") is not None:
            window["days"] = override["utc_days"]
        if (override.get("utc_start") is not None
                or override.get("utc_end") is not None):
            window["start"] = override.get("utc_start")
            window["end"] = override.get("utc_end")
        schedule.append(window)
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
