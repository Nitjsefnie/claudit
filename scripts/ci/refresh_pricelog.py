#!/usr/bin/env python3
"""Read OpenRouter endpoint price histories for provider-rate refreshes.

The log is used only when its endpoint can be joined unambiguously to one
listed host endpoint. Invalid, ambiguous, or pre-Unix-epoch histories are
left for the hourly refresh's detection-time sampler.
"""
from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

from refresh_logshape import PriceLogError, PriceSeries, read_log_payload
from refresh_prices import (RefreshError, lists_a_fee, rates_of, tag_region)
from backend import pricing as rate_pricing

MODELS_URL = "https://openrouter.ai/api/v1/models"
LOG_URL = "https://openrouter.ai/api/frontend/v1/stats/listed-pricing"
ENDPOINTS_URL = "https://openrouter.ai/api/v1/models/{}/endpoints"
_RATE_FIELDS = ("fresh", "create_5m", "create_1h", "read", "output")
_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# Bounded retries for the fetches below (issue #760): one transient timeout
# on the catalog read must not leave it unreadable, which would turn a
# delisted model's empty endpoints into a refusal. The retry changes only
# how many tries "unreadable" takes; a fetch failing every attempt still
# raises into the callers' existing unreadable handling.
FETCH_ATTEMPTS = 3
FETCH_BACKOFF_S = 2.0
# Transport failures a retry can plausibly clear. HTTPError is one of these
# (it subclasses URLError) and is refined by _transient: only a server-side
# "try again" — 5xx or 429 — is transient; a 4xx is the answer to the
# request as sent and never gets a second try. An empty or unparseable body
# is a broken transfer, never a valid listing (the same reasoning
# _catalog_slugs applies to an empty models list).
_TRANSIENT = (urllib.error.URLError, http.client.HTTPException,
              TimeoutError, ConnectionError, json.JSONDecodeError)

FetchModels = Callable[[], object]
FetchLog = Callable[[str], object]
FetchEndpoints = Callable[[str], object]


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


@dataclass(frozen=True)
class HostSelection:
    """Endpoint subset and histories selected for one listed host."""
    prefix: str
    endpoint_index: int
    series: list[PriceSeries]


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


def _transient(exc: BaseException) -> bool:
    """Whether a retry could plausibly clear this fetch failure."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500 or exc.code == 429
    return isinstance(exc, _TRANSIENT)


def _open(url: str):
    """Issue one GET for `_fetch_json`; the seam tests fake in place of the
    network."""
    request = urllib.request.Request(
        url, headers={"User-Agent": "claudit-refresh-provider-rates"})
    return urllib.request.urlopen(request, timeout=30)


def _sleep(seconds: float) -> None:
    """The backoff clock; tests replace this so no test ever really sleeps."""
    time.sleep(seconds)


def _fetch_json(url: str) -> object:
    """GET the URL as JSON, retrying bounded transient transport failures."""
    for attempt in range(FETCH_ATTEMPTS):
        try:
            with _open(url) as response:
                return json.load(response)
        except _TRANSIENT as exc:
            if not _transient(exc) or attempt + 1 == FETCH_ATTEMPTS:
                raise
            _sleep(FETCH_BACKOFF_S * 2 ** attempt)
    raise AssertionError("unreachable: every attempt returns or raises")


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
            stamp = f"{second.year:04d}{second.strftime('-%m-%dT%H:%M:%SZ')}"
            entry = {"from": stamp, **current}
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
    if not data:
        # An empty listing is a broken fetch, never a catalog of zero
        # models: read as unreadable, so no tracked model is delisted by it.
        return {}, "models response lists no models"
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
              fetch_log: FetchLog | None) -> tuple[dict[str, LogRead], set[str] | None]:
    """Fetch and validate each tracked model's log using one model catalog.

    Also returns the catalog's model-id set, or None whenever the catalog
    could not be read (no fetcher, a failed fetch, an unrecognised or empty
    listing): the caller cannot then tell a delisted model from a broken
    catalog fetch, and keeps refusing a still-tracked model's empty
    endpoints answer."""
    if fetch_catalog is None or fetch_log is None:
        return _unavailable(tracked, "listed-pricing fetch is not configured"), None
    try:
        slugs, catalog_error = _catalog_slugs(fetch_catalog())
    except Exception as exc:  # the log is a fallback input; it never refuses a host
        return (_unavailable(tracked,
                             f"models fetch failed: {str(exc) or type(exc).__name__}"),
                None)
    if catalog_error:
        return _unavailable(tracked, catalog_error), None
    logs = {}
    for model, source in tracked.items():
        model_id = source.get("id") if isinstance(source, dict) else None
        slug = slugs.get(model_id) if isinstance(model_id, str) else None
        if slug is None:
            logs[model] = LogRead(None, f"canonical_slug missing from /api/v1/models for {model_id!r}")
            continue
        try:
            logs[model] = LogRead(read_log_payload(fetch_log(slug)), None)
        except Exception as exc:  # HTTP, decoding and shape failures all sample
            reason = str(exc) or type(exc).__name__
            logs[model] = LogRead(None, reason)
    return logs, set(slugs)


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


def _series_entries(series: PriceSeries) -> list[dict] | None:
    entries = entries_for_series(series)
    if not entries:
        return None
    return entries


def _series_rates(series: PriceSeries) -> tuple[float, ...] | None:
    entries = _series_entries(series)
    if not isinstance(entries, list) or not entries:
        return None
    return _rate_vector(entries.pop())


def _earlier_state_vectors(series: PriceSeries) -> set[tuple[float, ...]]:
    """The series' in-force rate states before its newest one."""
    entries = entries_for_series(series)
    if not isinstance(entries, list) or not entries:
        return set()
    entries.pop()  # the newest state; everything before it is "earlier"
    # Pylint cannot follow entries_for_series's optional-list return; the
    # isinstance guard above narrows it (as in the tests' module header).
    # pylint: disable=not-an-iterable
    return {_rate_vector(entry) for entry in entries}


def _divergence_class(series: PriceSeries,
                      endpoint_rates: list[dict[str, float]]) -> str:
    """Name which side of a log/listing divergence the listing sits on.

    A disagreement between OpenRouter's listing and its own in-force log
    points is either the listing still at an earlier logged state (lagging
    — transient while the listing catches up) or at a state the log's
    in-force points never held (ahead of the truncation, or the log is
    stale for this host). Either way the host samples for the cycle;
    naming the class tells a human which side moved.
    """
    listed = {_rate_vector(rates) for rates in endpoint_rates}
    if listed & _earlier_state_vectors(series):
        return "the listing lags the price log"
    return "no in-force log state matches the listing"


def _match_series_to_endpoints(
        host_series: list[PriceSeries],
        endpoint_rates: list[dict[str, float]]) -> tuple[dict[int, PriceSeries] | None, str | None]:
    matched: dict[int, PriceSeries] = {}
    for item in host_series:
        latest = _series_rates(item)
        if latest is None:
            return None, "series has null input or output, or no complete state"
        candidates = [i for i, rates in enumerate(endpoint_rates)
                      if _rate_vector(rates) == latest]
        if not candidates:
            return None, _divergence_class(item, endpoint_rates)
        if len(candidates) != 1:
            return None, f"{len(candidates)} endpoints at one current price"
        endpoint_index = candidates[0]
        if endpoint_index in matched:
            return None, "more than one series matches the same endpoint"
        matched[endpoint_index] = item
    if len(matched) != len(endpoint_rates):
        return None, "one or more endpoints have no matching series"
    return matched, None


def _endpoint_overrides_kind(endpoint: dict) -> str | None:
    """What one endpoint's pricing.overrides classify as: "schedule" when a
    utc window is present, "band" when a min_prompt_tokens override is, and
    "schedule" (the historical wording) for any other overrides presence.
    The log's five fields carry neither, so any of them samples the host;
    the sampled reason names which classified it."""
    pricing = endpoint.get("pricing")
    if not isinstance(pricing, dict):
        return None
    overrides = pricing.get("overrides")
    if not isinstance(overrides, list) or not overrides:
        return None
    for override in overrides:
        if not isinstance(override, dict):
            continue
        if set(override) & {"utc_days", "utc_start", "utc_end"}:
            return "schedule"
        if "min_prompt_tokens" in override:
            return "band"
    return "schedule"


def _prepare_host(host: str, endpoints: list[dict], prefix_owners: dict[str, set[str]],
                  series_by_prefix: dict[str, list[PriceSeries]], region: str | None,
                  resolutions: dict) -> tuple[HostSelection | None, str | None]:
    prefixes = {endpoint["tag"].split("/", 1)[0] for endpoint in endpoints}
    reason = None
    prefix = next(iter(prefixes)) if len(prefixes) == 1 else ""
    selected: list[int] = []
    host_series = []
    if len(prefixes) != 1:
        reason = "host endpoints use more than one tag prefix"
    elif not prefix or prefix_owners.get(prefix) != {host}:
        reason = "tag prefix is shared by another host"
    elif any(lists_a_fee(endpoint.get("pricing")) for endpoint in endpoints):
        reason = "endpoint lists a per-request fee the price log cannot carry"
    elif any(kinds := [_endpoint_overrides_kind(endpoint)
                       for endpoint in endpoints]):
        reason = ("endpoint has a pricing schedule" if "schedule" in kinds
                  else "endpoint lists a long-context band")
    else:
        selected, reason = _selection_indices(host, endpoints, region, resolutions)
    if reason is None:
        host_series = series_by_prefix.get(prefix, [])
        if any(item.scheduled for item in host_series):
            reason = "series has a schedule"
        elif len(host_series) != len(endpoints):
            reason = "series count does not match endpoint count"
    selection = None if reason else HostSelection(prefix, selected[0], host_series)
    return selection, reason


def _generated_history_issue(series: PriceSeries, entries: list[dict], host: str) -> str | None:
    """Return why generated log entries cannot safely back a pricing row."""
    try:
        rate_pricing._history(  # pylint: disable=protected-access
            entries, f"{host} listed-pricing log", may_begin=True)
    except ValueError as exc:
        return f"series has unusable pricing entries: {exc}"
    if any(point.at < _UNIX_EPOCH
           for points in series.fields.values() for point in points):
        return "series has unusable pricing entries: log instant predates 1970"
    return None


def _join_host(host: str, endpoints: list[dict], prefix_owners: dict[str, set[str]],
               series_by_prefix: dict[str, list[PriceSeries]], region: str | None,
               resolutions: dict) -> HostLog:
    selection, reason = _prepare_host(
        host, endpoints, prefix_owners, series_by_prefix, region, resolutions)
    if reason or selection is None:
        return HostLog(None, reason or "listed host did not resolve to one endpoint")
    try:
        endpoint_rates = [_endpoint_rates(endpoint, f"{host} endpoint {i}")
                          for i, endpoint in enumerate(endpoints)]
    except PriceLogError as exc:
        return HostLog(None, str(exc))
    matches, reason = _match_series_to_endpoints(selection.series, endpoint_rates)
    if reason or matches is None:
        return HostLog(None, reason or "series did not identify one endpoint per listing")
    chosen = matches.get(selection.endpoint_index)
    if chosen is None:
        return HostLog(None, "selected endpoint has no matching series")
    entries = _series_entries(chosen)
    if not isinstance(entries, list) or not entries:
        return HostLog(None, "selected endpoint has no usable history")
    issue = _generated_history_issue(chosen, entries, host)
    return HostLog(None, issue) if issue else HostLog(entries, None)


def _endpoint_groups(
        raw_endpoints: list,
) -> tuple[dict[str, list[dict]], dict[str, str], dict[str, set[str]]]:
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
    for host, endpoints in by_host.items():
        prefixes = {endpoint["tag"].split("/", 1)[0]
                    for endpoint in endpoints if isinstance(endpoint.get("tag"), str)}
        for prefix in prefixes:
            owners.setdefault(prefix, set()).add(host)
    return by_host, invalid, owners


def _series_in_force(series: PriceSeries, at: datetime) -> PriceSeries:
    """The series without points dated after the fetch instant `at`.

    A point after `at` is not yet in force: the listed price the series must
    match is the one at `at`, and a not-yet-in-force change never enters a
    row's history. The log is refetched in full every run, so the change
    lands when a later fetch instant passes it.
    """
    return replace(
        series,
        fields={field: tuple(point for point in points if point.at <= at)
                for field, points in series.fields.items()})


def join_listed_pricing(endpoint_payload: object, series: list[PriceSeries],
                        region: str | None, resolutions: dict,
                        at: datetime) -> dict[str, HostLog]:
    """Join log histories to only the hosts with one provable endpoint.

    `at` is the fetch instant: each series is read truncated to it, so a
    point dated after the fetch (an announced change not yet in force)
    never backs a row or enters its history."""
    data = endpoint_payload.get("data") if isinstance(endpoint_payload, dict) else None
    raw_endpoints = data.get("endpoints") if isinstance(data, dict) else None
    if not isinstance(raw_endpoints, list):
        raise PriceLogError("endpoint response has no data.endpoints list")
    by_host, invalid, owners = _endpoint_groups(raw_endpoints)
    series_by_prefix = _series_prefixes([_series_in_force(item, at) for item in series])
    matched: dict[str, HostLog] = {}
    for host, endpoints in by_host.items():
        if host in invalid:
            matched[host] = HostLog(None, invalid[host])
            continue
        matched[host] = _join_host(host, endpoints, owners, series_by_prefix,
                                   region, resolutions)
    return matched
