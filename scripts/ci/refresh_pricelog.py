#!/usr/bin/env python3
"""Read OpenRouter endpoint price histories for provider-rate refreshes.

The log is used only when its endpoint can be joined unambiguously to one
listed host endpoint. Invalid or ambiguous histories are left for the hourly
refresh's detection-time sampler.
"""
from __future__ import annotations

import json
import math
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

from refresh_prices import RefreshError, rates_of, tag_region

MODELS_URL = "https://openrouter.ai/api/v1/models"
LOG_URL = "https://openrouter.ai/api/frontend/v1/stats/listed-pricing"
ENDPOINTS_URL = "https://openrouter.ai/api/v1/models/{}/endpoints"
_RATE_FIELDS = ("fresh", "create_5m", "create_1h", "read", "output")
_POINT_FIELDS = ("input", "output", "cacheRead", "cacheWrite", "discount")
_REQUIRED_POINTS = frozenset({"input", "output"})

FetchModels = Callable[[], object]
FetchLog = Callable[[str], object]
FetchEndpoints = Callable[[str], object]


class PriceLogError(ValueError):
    """A listed-pricing response that cannot safely back a provider row."""


@dataclass(frozen=True)
class Point:
    """One field's value at an OpenRouter log instant."""

    at: datetime
    value: float | None


@dataclass(frozen=True)
class PriceSeries:
    """One endpoint's independently timestamped rates."""

    endpoint_id: str
    provider_name: str
    provider_slug: str
    fields: dict[str, tuple[Point, ...]]
    scheduled: bool


@dataclass(frozen=True)
class LogRead:
    """A model's validated series, or the reason its log is unavailable."""

    series: list[PriceSeries] | None
    reason: str | None


@dataclass(frozen=True)
class HostLog:
    """A host's matched history, or the reason it must be sampled."""

    entries: list[dict] | None
    reason: str | None


def fetch_models() -> object:
    """Fetch OpenRouter's canonical model slugs."""
    return _fetch_json(MODELS_URL)


def fetch_endpoints(model_id: str) -> object:
    """Fetch one model's provider endpoints."""
    return _fetch_json(ENDPOINTS_URL.format(urllib.parse.quote(model_id, safe="/")))


def fetch_listed_pricing(canonical_slug: str) -> object:
    """Fetch one model's complete listed-pricing change log."""
    query = urllib.parse.urlencode({
        "permaslug": canonical_slug, "variant": "standard", "shape": "v4",
        "range": "all",
    })
    return _fetch_json(f"{LOG_URL}?{query}")


def _fetch_json(url: str) -> object:
    request = urllib.request.Request(
        url, headers={"User-Agent": "claudit-refresh-provider-rates"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


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
        series_list.append(PriceSeries(*identity, fields, bool(schedule)))
    return series_list


def _rounded(value: float) -> float:
    return round(value, 10)


def _rates(state: dict[str, float | None]) -> dict[str, float] | None:
    fresh, output = state.get("input"), state.get("output")
    if fresh is None or output is None:
        return None
    cache_read = state.get("cacheRead")
    cache_write = state.get("cacheWrite")
    create = cache_write if cache_write not in (None, 0) else fresh
    return {
        "fresh": _rounded(fresh), "create_5m": _rounded(create),
        "create_1h": _rounded(create),
        "read": _rounded(0 if cache_read is None else cache_read),
        "output": _rounded(output),
    }


def _discount_note(value: float | None) -> str | None:
    if value in (None, 0):
        return None
    return f"{format((Decimal(str(value)) * 100).normalize(), 'f')}% off"


def entries_for_series(series: PriceSeries) -> list[dict] | None:
    """Collapse each UTC second to its last state and emit rate changes."""
    if any(point.value is None
           for field in ("input", "output") for point in series.fields[field]):
        return None
    events = sorted((point.at, field, point.value)
                    for field, points in series.fields.items() for point in points)
    state: dict[str, float | None] = {}
    entries: list[dict] = []
    previous_rates: dict[str, float] | None = None
    cursor = 0
    while cursor < len(events):
        second = events[cursor][0].replace(microsecond=0)
        end = cursor
        while end < len(events) and events[end][0].replace(microsecond=0) == second:
            _, field, value = events[end]
            state[field] = value
            end += 1
        current = _rates(state)
        if current is not None and current != previous_rates:
            entry = {"from": second.strftime("%Y-%m-%dT%H:%M:%SZ"), **current}
            note = _discount_note(state.get("discount"))
            if note:
                entry["note"] = note
            entries.append(entry)
            previous_rates = current
        cursor = end
    return entries or None


def _catalog_slugs(payload: object) -> tuple[dict[str, str | None], str | None]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return {}, "models response has no data list"
    slugs: dict[str, str | None] = {}
    for index, item in enumerate(data):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            return {}, f"models response data[{index}] has no string id"
        model_id = item["id"]
        slug = item.get("canonical_slug")
        if slug is not None and not isinstance(slug, str):
            slugs[model_id] = None
        elif model_id in slugs:
            slugs[model_id] = None
        else:
            slugs[model_id] = slug or None
    return slugs, None


def _unavailable(tracked: dict, reason: str) -> dict[str, LogRead]:
    return {model: LogRead(None, reason) for model in tracked}


def read_logs(tracked: dict, fetch_catalog: FetchModels | None,
              fetch_log: FetchLog | None) -> dict[str, LogRead]:
    """Fetch and validate each tracked model's log using one model catalog."""
    if fetch_catalog is None or fetch_log is None:
        return _unavailable(tracked, "listed-pricing fetch is not configured")
    try:
        slugs, catalog_error = _catalog_slugs(fetch_catalog())
    except Exception as exc:  # the log is a fallback input; it never refuses a host
        return _unavailable(tracked, f"models fetch failed: {str(exc) or type(exc).__name__}")
    logs = {}
    for model, source in tracked.items():
        model_id = source.get("id") if isinstance(source, dict) else None
        if catalog_error:
            logs[model] = LogRead(None, catalog_error)
            continue
        slug = slugs.get(model_id) if isinstance(model_id, str) else None
        if slug is None:
            logs[model] = LogRead(None, f"canonical_slug missing from /api/v1/models for {model_id!r}")
            continue
        try:
            logs[model] = LogRead(read_log_payload(fetch_log(slug)), None)
        except Exception as exc:  # HTTP, decoding and shape failures all sample
            reason = str(exc) or type(exc).__name__
            logs[model] = LogRead(None, reason)
    return logs


def _rate_vector(rates: dict) -> tuple[float, ...]:
    return tuple(_rounded(float(rates[field])) for field in _RATE_FIELDS)


def _endpoint_rates(endpoint: dict, where: str) -> dict[str, float]:
    pricing = endpoint.get("pricing")
    if not isinstance(pricing, dict):
        raise PriceLogError(f"{where}: endpoint has no pricing object")
    try:
        return {field: _rounded(float(value))
                for field, value in rates_of(pricing, where).items()}
    except (RefreshError, TypeError, ValueError, OverflowError) as exc:
        raise PriceLogError(f"{where}: {exc}") from exc


def _selection_indices(host: str, endpoints: list[dict], region: str | None,
                       resolutions: dict) -> tuple[list[int], str | None]:
    pin = resolutions.get(host)
    if pin is not None and not isinstance(pin, dict):
        return [], "host resolution is not an object"
    if isinstance(pin, dict) and pin.get("select") == "cheapest":
        return [], "cheapest resolution has no stable endpoint identity"
    if isinstance(pin, dict) and "tag" in pin:
        tag = pin.get("tag")
        if not isinstance(tag, str):
            return [], "tag resolution is not a string"
        selected = [i for i, endpoint in enumerate(endpoints) if endpoint.get("tag") == tag]
    else:
        selected = [i for i, endpoint in enumerate(endpoints)
                    if isinstance(endpoint.get("tag"), str)
                    and tag_region(endpoint["tag"]) == region]
    if len(selected) != 1:
        return [], f"endpoint selection found {len(selected)} endpoints"
    return selected, None


def _series_prefixes(series: list[PriceSeries]) -> dict[str, list[PriceSeries]]:
    by_prefix: dict[str, list[PriceSeries]] = {}
    for item in series:
        by_prefix.setdefault(item.provider_slug, []).append(item)
    return by_prefix


def join_listed_pricing(endpoint_payload: object, series: list[PriceSeries],
                        region: str | None, resolutions: dict) -> dict[str, HostLog]:
    """Join log histories to only the hosts with one provable endpoint."""
    data = endpoint_payload.get("data") if isinstance(endpoint_payload, dict) else None
    raw_endpoints = data.get("endpoints") if isinstance(data, dict) else None
    if not isinstance(raw_endpoints, list):
        raise PriceLogError("endpoint response has no data.endpoints list")
    by_host: dict[str, list[dict]] = {}
    invalid: dict[str, str] = {}
    for index, endpoint in enumerate(raw_endpoints):
        host = endpoint.get("provider_name") if isinstance(endpoint, dict) else None
        if not isinstance(host, str) or not host:
            continue
        if not isinstance(endpoint.get("tag"), str):
            invalid[host] = f"endpoint {index} has no tag"
        by_host.setdefault(host, []).append(endpoint)
    owners: dict[str, set[str]] = {}
    prefixes: dict[str, set[str]] = {}
    for host, endpoints in by_host.items():
        host_prefixes = {endpoint["tag"].split("/", 1)[0]
                         for endpoint in endpoints if isinstance(endpoint.get("tag"), str)}
        prefixes[host] = host_prefixes
        for prefix in host_prefixes:
            owners.setdefault(prefix, set()).add(host)
    series_by_prefix = _series_prefixes(series)
    matched: dict[str, HostLog] = {}
    for host, endpoints in by_host.items():
        if host in invalid:
            matched[host] = HostLog(None, invalid[host])
            continue
        host_prefixes = prefixes[host]
        if len(host_prefixes) != 1:
            matched[host] = HostLog(None, "host endpoints use more than one tag prefix")
            continue
        prefix = next(iter(host_prefixes))
        if not prefix or owners.get(prefix) != {host}:
            matched[host] = HostLog(None, "tag prefix is shared by another host")
            continue
        if any(isinstance(endpoint.get("pricing"), dict)
               and endpoint["pricing"].get("overrides") for endpoint in endpoints):
            matched[host] = HostLog(None, "endpoint has a pricing schedule")
            continue
        selected, selection_error = _selection_indices(host, endpoints, region, resolutions)
        if selection_error:
            matched[host] = HostLog(None, selection_error)
            continue
        host_series = series_by_prefix.get(prefix, [])
        if any(item.scheduled for item in host_series):
            matched[host] = HostLog(None, "series has a schedule")
            continue
        if len(host_series) != len(endpoints):
            matched[host] = HostLog(None, "series count does not match endpoint count")
            continue
        endpoint_rates: list[dict[str, float]] = []
        try:
            endpoint_rates = [_endpoint_rates(endpoint, f"{host} endpoint {i}")
                              for i, endpoint in enumerate(endpoints)]
        except PriceLogError as exc:
            matched[host] = HostLog(None, str(exc))
            continue
        series_for_endpoint: dict[int, PriceSeries] = {}
        ambiguous = None
        for item in host_series:
            entries = entries_for_series(item)
            if entries is None:
                ambiguous = "series has null input or output, or no complete state"
                break
            latest = _rate_vector(entries[-1])
            candidates = [i for i, rates in enumerate(endpoint_rates)
                          if _rate_vector(rates) == latest]
            if not candidates:
                ambiguous = "latest log state disagrees with the listed price"
                break
            if len(candidates) != 1:
                ambiguous = "series current rates do not identify exactly one endpoint"
                break
            endpoint_index = candidates[0]
            if endpoint_index in series_for_endpoint:
                ambiguous = "more than one series matches the same endpoint"
                break
            series_for_endpoint[endpoint_index] = item
        if ambiguous:
            matched[host] = HostLog(None, ambiguous)
            continue
        if len(series_for_endpoint) != len(endpoints):
            matched[host] = HostLog(None, "one or more endpoints have no matching series")
            continue
        chosen = series_for_endpoint.get(selected[0])
        entries = entries_for_series(chosen) if chosen else None
        if entries is None:
            matched[host] = HostLog(None, "selected endpoint has no usable history")
        elif _rate_vector(entries[-1]) != _rate_vector(endpoint_rates[selected[0]]):
            matched[host] = HostLog(None, "latest log state disagrees with the listed price")
        else:
            matched[host] = HostLog(entries, None)
    return matched
