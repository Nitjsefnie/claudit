#!/usr/bin/env python3
"""Rewrite log-backed OpenRouter provider histories through a reviewed instant.

The backfill shares canonical-slug lookup, log validation and endpoint joining
with the hourly refresh. Rows without a unique log history stay untouched.
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
    return {field: entry[field] for field in _RATE_FIELDS}


def _same_rates(left: dict, right: dict) -> bool:
    return all(round(float(left[field]), 10) == round(float(right[field]), 10)
               for field in _RATE_FIELDS)


def _earlier_start(old_start: str | None, log_start: str) -> str | None:
    if old_start is None:
        return None
    old_at = datetime.fromisoformat(old_start.replace("Z", "+00:00"))
    log_at = datetime.strptime(log_start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return old_start if old_at < log_at else log_start


def _rewrite(history: list[dict], entries: list[dict], as_of: str) -> list[dict] | None:
    kept = [copy.deepcopy(entry) for entry in entries if entry["from"] <= as_of]
    if not kept:
        return None
    kept[0]["from"] = _earlier_start(history[0]["from"], kept[0]["from"])
    return kept


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
                    now: datetime) -> tuple[dict, str]:
    if log.series is None:
        reason = log.reason or "listed-pricing log unavailable"
        return {}, f"listed-pricing log unavailable: {reason}"
    try:
        return (refresh_pricelog.join_listed_pricing(
            payload, log.series, region, resolutions, now), "")
    except refresh_pricelog.PriceLogError as exc:
        return {}, f"listed-pricing log unavailable: {exc}"


def _listed_model(model: str, source: dict, old_hosts: dict, region: str | None,
                  fetch: FetchEndpoints, log: refresh_pricelog.LogRead,
                  now: datetime) -> tuple[ModelListing | None, str | None]:
    payload = hourly._fetch(fetch, model, source)  # pylint: disable=protected-access
    rows, refused, _ = hourly.listed_rows(
        model, payload, region, source.get("resolve", {}),
        {host: _entry_rates(history[-1]) for host, history in old_hosts.items()}, now)
    matched, unavailable = _join_model_log(
        payload, log, region, source.get("resolve", {}), now)
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
                if not _same_rates(rewritten[-1], history[-1]):
                    lines.append(f"  newest log state through {as_of} differs from "
                                 f"the old row for {host}; the log replaces it")
                providers[host] = rewritten
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


def backfill(doc: dict, fetch: FetchEndpoints, as_of: str,
             fetch_models: FetchModels, fetch_log: FetchLog,
             now: datetime | None = None) -> tuple[dict, dict[str, list[str]], bool]:
    """Build the reviewed historical file and per-host report lines.

    `now` is the instant the listing and log describe; it defaults to the
    wall clock and bounds how far a series is read, exactly as in the
    hourly refresh."""
    tracked, region = hourly._sources(doc)  # pylint: disable=protected-access
    result = copy.deepcopy(doc)
    logs = refresh_pricelog.read_logs(tracked, fetch_models, fetch_log)
    context = BackfillContext(doc, result, region, fetch, logs, as_of,
                              now or datetime.now(timezone.utc))
    reports, rewrote = _backfill_models(context, tracked)
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
        pricing_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n",
                                encoding="utf-8")
        constants_path.write_text(constants, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
