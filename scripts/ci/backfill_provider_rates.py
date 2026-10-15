#!/usr/bin/env python3
"""Rewrite log-backed OpenRouter provider histories through a reviewed instant.

The backfill shares canonical-slug lookup, log validation and endpoint joining
with the hourly refresh. Repeated states retain every instant in from-lists;
residual bands are corrected from a unique historical series or removed.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
# pylint: disable=wrong-import-position
import refresh_pricelog  # noqa: E402
import refresh_provider_rates as hourly  # noqa: E402
from refresh_pricelog import FetchEndpoints, FetchLog, FetchModels  # noqa: E402
from backend import pricing  # noqa: E402
from backend.pricing_document import (  # noqa: E402
    effective_rates, expand_pricing_doc, serialize_pricing_doc)
from backend.pricing_rows import flatten_provider_history  # noqa: E402

PRICING_JSON = REPO_ROOT / "src" / "pricing.json"
CONSTANTS_PY = REPO_ROOT / "backend" / "constants.py"
_AS_OF = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_RATE_FIELDS = pricing.RATE_FIELDS


def _as_of(value: str) -> str:
    if not _AS_OF.fullmatch(value):
        raise argparse.ArgumentTypeError("must be YYYY-MM-DDTHH:MM:SSZ")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a valid UTC instant") from exc
    return value


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill log-backed provider histories through a reviewed instant.")
    parser.add_argument("--as-of", required=True, type=_as_of,
                        help="inclusive UTC cut-off in YYYY-MM-DDTHH:MM:SSZ form")
    parser.add_argument("--dry-run", action="store_true",
                        help="report the rewrite without writing either file")
    return parser.parse_args(argv)


def _entry_rates(entry: dict) -> dict:
    return effective_rates(entry)


def _same_rates(left: dict, right: dict) -> bool:
    left_rates, right_rates = effective_rates(left), effective_rates(right)
    return all(round(float(left_rates[field]), 10) == round(float(right_rates[field]), 10)
               for field in _RATE_FIELDS)


def _earlier_start(old_start: str | None, log_start: str) -> str | None:
    if old_start is None:
        return None
    old_at = datetime.fromisoformat(old_start.replace("Z", "+00:00"))
    log_at = datetime.strptime(log_start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return old_start if old_at < log_at else log_start


def _flatten(history: list[dict]) -> list[dict]:
    entries, _ = flatten_provider_history(
        history, "backfill provider history",
        lambda stamp, _: datetime.fromisoformat(stamp.replace("Z", "+00:00")))
    return entries


def _merge_history(history: list[dict]) -> list[dict]:
    """Group identical dated states without dropping any of their instants.

    Notes and other priced fields are part of a state. A null start stays
    scalar because the storage format cannot mix null with dated instants.
    """
    merged: list[dict] = []
    states: dict[str, dict] = {}
    for entry in _flatten(history):
        state = {k: v for k, v in entry.items() if k != "from"}
        state.update(effective_rates(entry))
        stamp = entry.get("from")
        key = json.dumps(state, sort_keys=True, allow_nan=False)
        if stamp is not None and key in states:
            previous = states[key]
            instants = previous["from"]
            previous["from"] = ([*instants, stamp] if isinstance(instants, list)
                                else [instants, stamp])
        else:
            current = {"from": stamp, **copy.deepcopy(state)}
            merged.append(current)
            if stamp is not None:
                states[key] = current
    return merged


def _rewrite(history: list[dict], entries: list[dict], as_of: str) -> list[dict] | None:
    kept = [copy.deepcopy(entry) for entry in entries if entry["from"] <= as_of]
    if not kept:
        return None
    kept[0]["from"] = _earlier_start(_flatten(history)[0].get("from"), kept[0]["from"])
    return _merge_history(kept)


def _hosts_from_listing(payload: object) -> set[str]:
    data = payload.get("data") if isinstance(payload, dict) else None
    endpoints = data.get("endpoints") if isinstance(data, dict) else None
    if not isinstance(endpoints, list):
        return set()
    return {endpoint["provider_name"] for endpoint in endpoints
            if isinstance(endpoint, dict) and isinstance(endpoint.get("provider_name"), str)}


def _render(as_of: str, reports: dict[str, list[str]]) -> str:
    lines = [f"OpenRouter provider-rate backfill through {as_of}"]
    for model, entries in reports.items():
        lines += ["", model, *entries]
    return "\n".join(lines)


@dataclass
class ModelListing:
    """Endpoint rows and log matches for one model's backfill pass."""
    payload: object
    rows: dict[str, hourly.Listing]
    refused: dict[str, str]
    matched: dict
    unavailable: str


@dataclass(frozen=True)
class BackfillContext:
    """Shared inputs for rewriting all tracked provider rows."""
    source_doc: dict
    result: dict
    region: str | None
    fetch: FetchEndpoints
    logs: dict[str, refresh_pricelog.LogRead]
    as_of: str
    now: datetime


def _join_model_log(payload: object, log: refresh_pricelog.LogRead,
                    region: str | None, resolutions: dict,
                    now: datetime, stored: dict[str, dict]) -> tuple[dict, str]:
    if log.series is None:
        reason = log.reason or "listed-pricing log unavailable"
        return {}, f"listed-pricing log unavailable: {reason}"
    try:
        return (refresh_pricelog.join_listed_pricing(
            payload, log.series, region, resolutions, now, stored), "")
    except refresh_pricelog.PriceLogError as exc:
        return {}, f"listed-pricing log unavailable: {exc}"


def _listed_model(model: str, source: dict, old_hosts: dict, region: str | None,
                  fetch: FetchEndpoints, log: refresh_pricelog.LogRead,
                  now: datetime) -> tuple[ModelListing | None, str | None]:
    payload = hourly._fetch(fetch, model, source)  # pylint: disable=protected-access
    # The fourth element is the untracked-host notices; a host they name has
    # no row here either — `_host_reason` reports it through the log join's
    # own sampled reason, and the row stands untouched.
    rows, refused, _, _ = hourly.listed_rows(
        model, payload, region, source.get("resolve", {}),
        {host: _entry_rates(_flatten(history)[-1])
         for host, history in old_hosts.items()}, now)
    matched, unavailable = _join_model_log(
        payload, log, region, source.get("resolve", {}), now,
        {host: _entry_rates(history[-1]) for host, history in old_hosts.items()})
    return ModelListing(payload, rows, refused, matched, unavailable), None


def _host_reason(
        host: str, listing: ModelListing
) -> tuple[refresh_pricelog.HostLog | None, str | None]:
    if listing.unavailable:
        return None, listing.unavailable
    if host in listing.refused:
        return None, listing.refused[host]
    if host not in listing.rows:
        match = listing.matched.get(host)
        return match, (match.reason if match and match.reason else
                       "no endpoint selected in the data region")
    match = listing.matched.get(host)
    return match, match.reason if match else "no log series joined to the listed host"


def _rewrite_host(host: str, history: list[dict] | None, listing: ModelListing,
                  providers: dict, as_of: str) -> tuple[bool, list[str]]:
    match, reason = _host_reason(host, listing)
    if reason is None and host in listing.rows and match is not None and match.entries:
        if not hourly._same_rates(match.entries[-1], listing.rows[host].rates):  # pylint: disable=protected-access
            reason = "latest log state disagrees with the listed price"
        elif history is None:
            return False, [f"  untouched {host}: no row (log-backed host)"]
        else:
            rewritten = _rewrite(history, match.entries, as_of)
            if rewritten is None:
                reason = "no log entries at or before --as-of"
            else:
                lines = []
                if not _same_rates(_flatten(rewritten)[-1], _flatten(history)[-1]):
                    lines.append(f"  newest log state through {as_of} differs from "
                                 f"the old row for {host}; the log replaces it")
                providers[host] = rewritten
                if any("band" in entry for entry in history):
                    lines.append(f"  replaced band for {host}: complete log history "
                                 "(human-reviewed correction)")
                lines.append(f"  rewritten {host}: {len(history)} → {len(rewritten)} entries")
                return True, lines
    if history is None:
        return False, [f"  untouched {host}: no row ({reason})"]
    return False, [f"  untouched {host}: {len(history)} → {len(history)} entries ({reason})"]


def _backfill_model(context: BackfillContext, model: str, source: dict,
                    old_hosts: dict) -> tuple[bool, list[str]]:
    providers = context.result["providers"].setdefault(model, {})
    try:
        listing, error = _listed_model(
            model, source, old_hosts, context.region, context.fetch, context.logs[model],
            context.now)
    except hourly.RefreshError as exc:
        listing, error = None, f"endpoint listing unavailable: {exc}"
    if listing is None:
        lines = [f"  untouched {host}: {len(history)} → {len(history)} entries ({error})"
                 for host, history in old_hosts.items()]
        return False, lines

    rewritten_any = False
    lines = []
    hosts = set(old_hosts) | _hosts_from_listing(listing.payload) | set(listing.refused)
    for host in sorted(hosts):
        rewritten, host_lines = _rewrite_host(
            host, old_hosts.get(host), listing, providers, context.as_of)
        rewritten_any = rewritten_any or rewritten
        lines.extend(host_lines)
    return rewritten_any, lines


def _backfill_models(context: BackfillContext,
                     tracked: dict) -> tuple[dict[str, list[str]], bool]:
    reports: dict[str, list[str]] = {}
    rewrote = False
    for model, source in tracked.items():
        old_hosts = context.source_doc["providers"].get(model, {})
        did_rewrite, model_report = _backfill_model(context, model, source, old_hosts)
        rewrote = rewrote or did_rewrite
        reports[model] = model_report
    return reports, rewrote


def _historical_entries(log: refresh_pricelog.LogRead | None, host: str,
                        now: datetime) -> tuple[list[dict] | None, str]:
    """A residual band needs one historical endpoint, not a current price join."""
    if log is None or log.series is None:
        return None, log.reason if log and log.reason else "no model log series"
    series = [item for item in log.series if item.provider_name == host]
    if len(series) != 1:
        return None, f"no unique host endpoint series ({len(series)} found)"
    chosen = series[0]
    if chosen.scheduled:
        return None, "historical endpoint series has a schedule"
    chosen = refresh_pricelog._series_in_force(chosen, now)  # pylint: disable=protected-access
    entries = refresh_pricelog.entries_for_series(chosen)
    if not entries:
        return None, "historical endpoint series has no complete rate states"
    issue = refresh_pricelog._generated_history_issue(chosen, entries, host)  # pylint: disable=protected-access
    return (None, issue) if issue else (entries, "")


def _band_span(entry: dict, end: str | None, entries: list[dict],
               as_of: str) -> list[dict]:
    start = entry.get("from")
    through = [point for point in entries if point["from"] <= as_of]
    if not through:
        return []
    before = [point for point in through if start is not None and point["from"] <= start]
    first = before[-1] if before else through[0]
    if end is not None and first["from"] >= end:
        return []
    points = [{**first, "from": start},
              *[copy.deepcopy(point) for point in through
                if point["from"] > first["from"]
                and (end is None or point["from"] < end)]]
    if "web_search" in entry:
        for point in points:
            point["web_search"] = entry["web_search"]
    return points


def _replace_bands(history: list[dict], entries: list[dict] | None,
                   as_of: str, reason: str) -> tuple[list[dict], list[tuple[str, str]]]:
    flat = _flatten(history)
    corrected: list[dict] = []
    outcomes: list[tuple[str, str]] = []
    for i, entry in enumerate(flat):
        if "band" not in entry:
            candidates = [entry]
        else:
            end = flat[i + 1].get("from") if i + 1 < len(flat) else None
            candidates = _band_span(entry, end, entries, as_of) if entries else []
            if candidates:
                outcomes.append(("replaced band", f"span {entry.get('from')} to {end}; "
                                 "unique historical endpoint series (human-reviewed correction)"))
            else:
                outcomes.append(("removed band", reason or "no log state covers the band span"))
        for candidate in candidates:
            candidate = {**candidate, **effective_rates(candidate)}
            # Removing a band can join two equal surrounding states.
            if corrected and ({k: v for k, v in corrected[-1].items() if k != "from"}
                              == {k: v for k, v in candidate.items() if k != "from"}):
                continue
            corrected.append(candidate)
    return _merge_history(corrected), outcomes


def _residual_bands(context: BackfillContext, reports: dict[str, list[str]]) -> bool:
    changed = False
    for model, hosts in context.result["providers"].items():
        for host, history in list(hosts.items()):
            if not any("band" in entry for entry in history):
                continue
            cutoff = min(context.now, datetime.fromisoformat(context.as_of.replace("Z", "+00:00")))
            entries, reason = _historical_entries(context.logs.get(model), host, cutoff)
            rewritten, outcomes = _replace_bands(history, entries, context.as_of, reason)
            if rewritten:
                hosts[host] = rewritten
            else:
                del hosts[host]
            for action, detail in outcomes:
                reports.setdefault(model, []).append(
                    f"  {action} for {host}: {len(history)} → {len(rewritten)} entries ({detail})")
            changed = True
    return changed


def backfill(doc: dict, fetch: FetchEndpoints, as_of: str,
             fetch_models: FetchModels, fetch_log: FetchLog,
             now: datetime | None = None) -> tuple[dict, dict[str, list[str]], bool]:
    """Build the reviewed historical file and per-host report lines.

    `now` is the instant the listing and log describe; it defaults to the
    wall clock and bounds how far a series is read, exactly as in the
    hourly refresh."""
    pricing.load_tables(doc)
    source_doc = expand_pricing_doc(doc)
    tracked, region = hourly._sources(source_doc)  # pylint: disable=protected-access
    result = copy.deepcopy(source_doc)
    logs, _ = refresh_pricelog.read_logs(tracked, fetch_models, fetch_log)
    context = BackfillContext(source_doc, result, region, fetch, logs, as_of,
                              now or datetime.now(timezone.utc))
    reports, rewrote = _backfill_models(context, tracked)
    rewrote = _residual_bands(context, reports) or rewrote
    if rewrote:
        result["provider_rates_fetched"] = as_of
    pricing.load_tables(result)
    return result, reports, rewrote


def main(argv: list[str] | None = None, *, fetch: FetchEndpoints = refresh_pricelog.fetch_endpoints,
         fetch_models: FetchModels = refresh_pricelog.fetch_models,
         fetch_log: FetchLog = refresh_pricelog.fetch_listed_pricing,
         pricing_path: Path = PRICING_JSON, constants_path: Path = CONSTANTS_PY,
         now: datetime | None = None) -> int:
    args = _arguments(argv)
    try:
        doc = json.loads(pricing_path.read_text(encoding="utf-8"))
        result, reports, rewrote = backfill(doc, fetch, args.as_of, fetch_models,
                                            fetch_log, now)
        constants = constants_path.read_text(encoding="utf-8")
        if rewrote:
            constants = hourly.bump_pricing_version(constants)
    except (hourly.RefreshError, ValueError, OSError) as exc:
        print(f"backfill_provider_rates: {exc}", file=sys.stderr)
        return 1
    print(_render(args.as_of, reports))
    if rewrote and not args.dry_run:
        pricing_path.write_text(serialize_pricing_doc(result),
                                encoding="utf-8", newline="\n")
        constants_path.write_text(constants, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
