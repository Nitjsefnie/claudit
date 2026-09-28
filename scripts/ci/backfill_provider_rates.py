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


def backfill(doc: dict, fetch: FetchEndpoints, as_of: str,
             fetch_models: FetchModels, fetch_log: FetchLog) -> tuple[dict, dict[str, list[str]], bool]:
    """Build the reviewed historical file and per-host report lines."""
    tracked, region = hourly._sources(doc)  # pylint: disable=protected-access
    result = copy.deepcopy(doc)
    logs = refresh_pricelog.read_logs(tracked, fetch_models, fetch_log)
    reports: dict[str, list[str]] = {}
    rewrote = False
    for model, source in tracked.items():
        providers = result["providers"].setdefault(model, {})
        old_hosts = doc["providers"].get(model, {})
        model_report = []
        try:
            payload = hourly._fetch(fetch, model, source)  # pylint: disable=protected-access
            rows, refused, _ = hourly.listed_rows(
                model, payload, region, source.get("resolve", {}),
                {host: _entry_rates(history[-1]) for host, history in old_hosts.items()},
                datetime.now(timezone.utc))
        except hourly.RefreshError as exc:
            reason = f"endpoint listing unavailable: {exc}"
            for host, history in old_hosts.items():
                model_report.append(f"  untouched {host}: {len(history)} → {len(history)} entries "
                                    f"({reason})")
            reports[model] = model_report
            continue
        log = logs[model]
        if log.series is None:
            why_unavailable = log.reason or "listed-pricing log unavailable"
            matched = {}
            unavailable_reason = f"listed-pricing log unavailable: {why_unavailable}"
        else:
            try:
                matched = refresh_pricelog.join_listed_pricing(
                    payload, log.series, region, source.get("resolve", {}))
                unavailable_reason = ""
            except refresh_pricelog.PriceLogError as exc:
                matched = {}
                unavailable_reason = f"listed-pricing log unavailable: {exc}"
        hosts = set(old_hosts) | _hosts_from_listing(payload) | set(refused)
        for host in sorted(hosts):
            history = old_hosts.get(host)
            if unavailable_reason:
                reason = unavailable_reason
            elif host in refused:
                reason = refused[host]
            elif host not in rows:
                match = matched.get(host)
                reason = match.reason if match and match.reason else "no endpoint selected in the data region"
            else:
                match = matched.get(host)
                reason = match.reason if match else "no log series joined to the listed host"
                if match is not None and match.entries is not None:
                    if not hourly._same_rates(match.entries[-1], rows[host].rates):  # pylint: disable=protected-access
                        reason = "latest log state disagrees with the listed price"
                    elif history is None:
                        model_report.append(f"  untouched {host}: no row (log-backed host)")
                        continue
                    else:
                        rewritten = _rewrite(history, match.entries, as_of)
                        if rewritten is None:
                            reason = "no log entries at or before --as-of"
                        else:
                            if not _same_rates(rewritten[-1], history[-1]):
                                model_report.append(
                                    f"  newest log state through {as_of} differs from "
                                    f"the old row for {host}; the log replaces it")
                            providers[host] = rewritten
                            rewrote = True
                            model_report.append(
                                f"  rewritten {host}: {len(history)} → {len(rewritten)} entries")
                            continue
            if history is None:
                model_report.append(f"  untouched {host}: no row ({reason})")
            else:
                model_report.append(f"  untouched {host}: {len(history)} → {len(history)} entries "
                                    f"({reason})")
        reports[model] = model_report
    if rewrote:
        result["provider_rates_fetched"] = as_of
    pricing.load_tables(result)
    return result, reports, rewrote


def main(argv: list[str] | None = None, *, fetch: FetchEndpoints = refresh_pricelog.fetch_endpoints,
         fetch_models: FetchModels = refresh_pricelog.fetch_models,
         fetch_log: FetchLog = refresh_pricelog.fetch_listed_pricing,
         pricing_path: Path = PRICING_JSON, constants_path: Path = CONSTANTS_PY) -> int:
    args = _arguments(argv)
    try:
        doc = json.loads(pricing_path.read_text(encoding="utf-8"))
        result, reports, rewrote = backfill(doc, fetch, args.as_of, fetch_models, fetch_log)
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
