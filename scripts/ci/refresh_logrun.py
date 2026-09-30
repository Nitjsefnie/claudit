#!/usr/bin/env python3
"""Classify log-backed rows and sample generated histories that cannot load."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

import refresh_pricelog


@dataclass
class LogRows:
    """One model's log-backed moves and sampled rows."""
    moves: list[Any]
    sampled_rows: dict[str, Any]
    sampled: dict[str, str]
    notices: list[str]


def _unavailable_log_rows(model: str, rows: dict[str, Any], reason: str) -> LogRows:
    notice = f"listed-pricing log unavailable for {model}: {reason}; its hosts were sampled"
    return LogRows([], rows, {host: reason for host in rows}, [notice])


def _append_joined_rows(
        model: str, rows: dict[str, Any], hosts: dict,
        joined: dict[str, refresh_pricelog.HostLog],
        append_logged: Callable[[str, dict, str, Any, list[dict]], Any],
        same_rates: Callable[[dict, dict], bool]) -> LogRows:
    result = LogRows([], {}, {}, [])
    for host, listing in rows.items():
        match = joined.get(host)
        reason = match.reason if match else "no series joined to the listed host"
        if match is not None and match.entries is not None:
            if not same_rates(match.entries[-1], listing.rates):
                reason = "latest log state disagrees with the listed price"
            elif (hosts.get(host) and
                  hosts[host][-1].get("schedule") != listing.schedule):
                reason = "stored schedule changed outside the price log"
            else:
                move = append_logged(model, hosts, host, listing, match.entries)
                if move:
                    result.moves.append(move)
                continue
        if reason and "disagrees" in reason:
            result.notices.append(
                f"listed-pricing log disagrees with the listing for "
                f"{model} via {host}; the host was sampled")
        if reason and reason.startswith("series has unusable pricing entries:"):
            result.notices.append(
                f"listed-pricing entries are unusable for {model} via {host}; "
                f"the host was sampled")
        result.sampled[host] = reason or "log does not identify one endpoint"
        result.sampled_rows[host] = listing
    return result


def classify_log_rows(
        model: str, payload: object, rows: dict[str, Any], hosts: dict,
        read: refresh_pricelog.LogRead, region: str | None, resolutions: dict,
        append_logged: Callable[[str, dict, str, Any, list[dict]], Any],
        same_rates: Callable[[dict, dict], bool], at: datetime) -> LogRows:
    """Split listed hosts into log-backed appends and sampled rows; `at` is
    the fetch instant, at which each log series is read."""
    if read.series is None:
        return _unavailable_log_rows(
            model, rows, read.reason or "listed-pricing log is unavailable")
    try:
        joined = refresh_pricelog.join_listed_pricing(
            payload, read.series, region, resolutions, at)
    except refresh_pricelog.PriceLogError as exc:
        return _unavailable_log_rows(model, rows, str(exc))
    return _append_joined_rows(
        model, rows, hosts, joined, append_logged, same_rates)
