#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
# pylint: disable=wrong-import-position
# The long-context band OpenRouter lists (a `min_prompt_tokens` override).
# The band names its own threshold; its coherent price ratios yield the
# model's input/output factors. `min_prompt_tokens` names the band; the
# other three keys are the utc fields a weekly window carries.
from refresh_prices_rates import (  # noqa: E402
    TOKEN_PRICED,
    PRICED,
    _OVERRIDE_KEYS,
    _UTC_KEYS,
    MIN_PROMPT_TOKENS,
    meter_shape_ok,
    band_threshold,
    metered_band,
    RefreshError,
    Untracked,
    _per_million,
    is_zero,
    _per_search,
    rates_of,
    token_rates_of,
    as_listed,
    entry_schedule,
)
from backend import pricing  # noqa: E402

__all__ = (
    'TOKEN_PRICED',
    'PRICED',
    '_OVERRIDE_KEYS',
    '_UTC_KEYS',
    'MIN_PROMPT_TOKENS',
    'meter_shape_ok',
    'band_threshold',
    'metered_band',
    'RefreshError',
    'Untracked',
    '_per_million',
    'is_zero',
    '_per_search',
    'rates_of',
    'token_rates_of',
    'as_listed',
    'entry_schedule',
    'QUANTIZATIONS',
    'REGIONS',
    'REGION_RE',
    'tag_region',
    'unknown_suffixes',
    'in_a_window',
    'covers_week',
)


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
    """The recognized region marker, including global, or None for a bare tag."""
    for suffix in tag.split("/")[1:]:
        if REGION_RE.fullmatch(suffix):
            return suffix.lower()
    return None


def unknown_suffixes(tag: str) -> list[str]:
    return [suffix for suffix in tag.split("/")[1:]
            if not REGION_RE.fullmatch(suffix) and suffix.lower() not in QUANTIZATIONS]


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
