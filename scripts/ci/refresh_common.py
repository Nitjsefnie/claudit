#!/usr/bin/env python3
"""Shared time, source-config, and version helpers for provider-rate tools."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from refresh_prices import REGION_RE, RefreshError

_PRICING_VERSION = re.compile(r'^PRICING_VERSION = "(\d+)"$', re.MULTILINE)


def detection_stamp(now: datetime) -> str:
    """Whole seconds in UTC: the one spelling both loaders accept."""
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def bump_pricing_version(text: str) -> str:
    """Move PRICING_VERSION one past the value already in the constants file."""
    found = _PRICING_VERSION.findall(text)
    if len(found) != 1:
        raise RefreshError("backend/constants.py: expected exactly one PRICING_VERSION line")
    version = int(found[0]) + 1
    return _PRICING_VERSION.sub(f'PRICING_VERSION = "{version}"', text)


def data_region(config: object) -> str | None:
    """Resolve openrouter.data_region to the endpoint tag's region spelling."""
    region = config.get("data_region") if isinstance(config, dict) else None
    if region == "global":
        return None
    if isinstance(region, str) and region == region.lower() and REGION_RE.fullmatch(region):
        return region
    raise RefreshError(f"openrouter.data_region {region!r} is neither 'global' "
                       "nor a region code")


def sources(doc: dict) -> tuple[dict, str | None]:
    """Return the tracked models and their endpoint data region."""
    config = doc.get("openrouter")
    tracked = config.get("models") if isinstance(config, dict) else None
    if not isinstance(tracked, dict) or not set(doc["providers"]) <= set(tracked):
        raise RefreshError("every provider-table model needs an openrouter.models id")
    return tracked, data_region(config)
