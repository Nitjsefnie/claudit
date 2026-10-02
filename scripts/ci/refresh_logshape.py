#!/usr/bin/env python3
"""Validate OpenRouter's listed-pricing log response shape.

The listed-pricing log is used only when its response parses as complete,
strictly-ordered, non-negative per-field point series; anything else raises
PriceLogError, which the callers turn into a sampled host with a reason.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone


class PriceLogError(ValueError):
    """A listed-pricing response that cannot safely back a provider row."""


@dataclass(frozen=True)
class Point:
    """One field's value at an OpenRouter log instant."""

    at: datetime
    value: float | None


def _instant(value: object, where: str) -> datetime:
    """Parse the log's UTC ISO-8601 spelling, which must end in Z."""
    if not isinstance(value, str) or not value.endswith("Z"):
        raise PriceLogError(f"{where}: at is not an ISO-8601 UTC instant ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise PriceLogError(f"{where}: at {value!r} is not a valid UTC instant") from exc
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise PriceLogError(f"{where}: at {value!r} is not UTC")
    return parsed


def _field_points(value: object, where: str) -> tuple[Point, ...]:
    if not isinstance(value, list):
        raise PriceLogError(f"{where}: points are not a list")
    points: list[Point] = []
    for index, item in enumerate(value):
        point_where = f"{where}[{index}]"
        if not isinstance(item, dict) or "at" not in item or "value" not in item:
            raise PriceLogError(f"{point_where}: point needs 'at' and 'value'")
        at = _instant(item["at"], point_where)
        raw = item["value"]
        if raw is None:
            number = None
        elif isinstance(raw, (int, float)) and not isinstance(raw, bool):
            try:
                number = float(raw)
            except (OverflowError, ValueError):
                number = float("inf")
            if not math.isfinite(number) or number < 0:
                raise PriceLogError(
                    f"{point_where}: value is not a finite non-negative number or null")
        else:
            raise PriceLogError(f"{point_where}: value is not a finite non-negative number or null")
        if points and at <= points[-1].at:
            raise PriceLogError(f"{point_where}: points are not strictly ordered by at")
        points.append(Point(at, number))
    return tuple(points)


@dataclass(frozen=True)
class PriceSeries:
    """One endpoint's independently timestamped rates."""

    endpoint_id: str
    provider_name: str
    provider_slug: str
    fields: dict[str, tuple[Point, ...]]
    scheduled: bool


_POINT_FIELDS = ("input", "output", "cacheRead", "cacheWrite", "discount")
_REQUIRED_POINTS = frozenset({"input", "output"})


def read_log_payload(payload: object) -> list[PriceSeries]:
    """Validate a complete listed-pricing response before using any series."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise PriceLogError("response has no data object")
    raw_series = data.get("series")
    if not isinstance(raw_series, list):
        raise PriceLogError("data.series is not a list")
    series_list: list[PriceSeries] = []
    for index, raw in enumerate(raw_series):
        where = f"data.series[{index}]"
        if not isinstance(raw, dict):
            raise PriceLogError(f"{where} is not an object")
        identity = []
        for key in ("endpointId", "providerName", "providerSlug"):
            value = raw.get(key)
            if not isinstance(value, str) or not value:
                raise PriceLogError(f"{where}.{key} is not a non-empty string")
            identity.append(value)
        fields: dict[str, tuple[Point, ...]] = {}
        for field in _POINT_FIELDS:
            if field not in raw and field in _REQUIRED_POINTS:
                raise PriceLogError(f"{where}.{field} is missing")
            fields[field] = _field_points(raw.get(field, []), f"{where}.{field}")
        schedule = raw.get("schedule", [])
        if not isinstance(schedule, list):
            raise PriceLogError(f"{where}.schedule is not a list")
        series_list.append(PriceSeries(
            identity[0], identity[1], identity[2], fields, bool(schedule)))
    return series_list
