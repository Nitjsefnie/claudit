#!/usr/bin/env python3
"""Refresh src/pricing.json's OpenRouter provider rates (SV-RATE-REFRESH).

Fetches every tracked model's endpoints and listed-pricing log. A host with
one provable endpoint history appends each logged rate change at OpenRouter's
change time; a first row imports its log from the history floor
(HISTORY_FLOOR) onward, since earlier states price no record the meters hold.
Hosts without an unambiguous
log join are sampled at detection time and use the existing alternation rule.
No existing entry is rewritten by the hourly refresh. A run that appends
bumps PRICING_VERSION (a reprice, never a reparse) by one from whatever
backend/constants.py holds and moves provider_rates_fetched; a quiet run
writes nothing.

Only endpoints in the account's data region count (tag_region). A host's
weekly time-of-day prices (pricing.overrides) become its entry's schedule;
its top-level price is the entry's default only when the fetch falls
outside every window, since inside one it is that window's price.
Endpoint selection — the resolution shapes (a tag, "cheapest", their
combination, a recorded ignore) and the four automatic rules over the
mechanical multi-price shapes — lives in refresh_selection.py and
refresh_selection_rules.py. Tag/region normalisation and whole-week
schedule coverage live in refresh_prices.py and are imported here.
These refuse the host or model they concern, which appends nothing:
- a host with two endpoints in that region at different prices and no
  resolution for it (a tag, "cheapest" of otherwise identical twins, or
  their combination with a recorded "ignore" of listing-artifact fields),
  where no automatic rule fits the shape — a bare namespace endpoint
  beside its service tiers, one quantization endpoint beside throughput
  tiers, the configured data region, and one quantization's price twins
  each resolve themselves and are reported as rule-resolved;
- a resolution that no longer applies;
- a price or override kind this script does not model, or a response
  shape it does not recognise. Web-search rates are sampled separately
  from the token price log and stored as a per-search rate. The one modelled override
  exception is the long-context band: a coherent `min_prompt_tokens`
  override infers representable input/output factors from its listed prices;
  those factors need not match the currently stored meter. The band contributes no
  window or membership here — the vendor pass owns the meter entry — and
  its rates never enter the row. A band never enters a provider entry's
  `band` field: that field is the oscillation band, a different shape;
- a host seen for the first time while the fetch falls inside one of its
  schedule's windows, unless its schedule covers the whole week: then no
  record is priced by the entry default and it starts as the listed top-level
  price; otherwise that price is a window price and the next fetch outside
  every window starts the row;
- a tracked model still in the catalog with no endpoints, or none in the
  region. A model gone from the catalog (/api/v1/models) is a delisting,
  not a refusal: the run skips it with a notice, its row keeps pricing
  stored records, and it rejoins the refresh on its own when the catalog
  relists it. An unreadable catalog proves nothing about delisting, so
  the empty-endpoints refusal stands while the catalog cannot be read.

Every other sampled move is still written, then the script exits nonzero
naming each refusal. A detection time not after a sampled row's newest
entry leaves that host untouched with a notice; appending after it would
refuse the file, and one host's damage never blocks the others. The
one-time, human-reviewed history rewrite
lives in backfill_provider_rates.py.

The same run tracks the first-party vendor table (refresh_vendor_rates,
SV-VENDOR-RATES): a catalog id under a configured
openrouter.vendor.prefixes entry whose derived key is untracked joins
openrouter.models as {"id", "vendor_host"} — no rates; the provider pass
carries its (key, vendor_host) row from the next hourly run. One report,
one PRICING_VERSION bump, one commit.

    python3 scripts/ci/refresh_provider_rates.py [--dry-run] [--commit-msg FILE]
"""
from __future__ import annotations

import http.client
import json
import sys
import urllib.error
from dataclasses import dataclass
from functools import partial
from itertools import combinations
from typing import Callable
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
# pylint: disable=wrong-import-position
sys.path.insert(0, str(Path(__file__).resolve().parent))
import refresh_logrun  # noqa: E402
import refresh_pricelog  # noqa: E402
from refresh_pricelog import (FetchEndpoints as Fetch, FetchLog, FetchModels)  # noqa: E402
from refresh_common import (bump_pricing_version,  # noqa: E402
                            detection_stamp, sources as _sources)
from refresh_report import (arguments as _arguments,  # noqa: E402
                            commit_message, report, vendor_report)
from refresh_prices import RefreshError  # noqa: E402
from refresh_selection import Listing, listed_rows  # noqa: E402
import refresh_vendor_rates  # noqa: E402
from refresh_provider_history import Move, _append  # noqa: E402
from refresh_provider_history_logged import _append_logged  # noqa: E402
from backend import pricing  # noqa: E402
from backend.pricing_document import (  # noqa: E402
    effective_rates, expand_pricing_doc, serialize_pricing_doc)

PRICING_JSON = REPO_ROOT / "src" / "pricing.json"
CONSTANTS_PY = REPO_ROOT / "backend" / "constants.py"
RATE_FIELDS = pricing.RATE_FIELDS


def _scales_alike(listing: Listing) -> bool:
    """Whether every window is the default times one factor per window,
    which keeps the read-time fold's Token Breakdown split exact
    (SV-RATE-DATA)."""
    for window in listing.schedule or []:
        pairs = [(Decimal(repr(listing.rates[f])), Decimal(repr(window["rates"][f])))
                 for f in RATE_FIELDS]
        if all(base == 0 for base, _ in pairs):
            if any(rate != 0 for _, rate in pairs):
                return False
        elif any(b1 * r2 != b2 * r1 for (b1, r1), (b2, r2) in combinations(pairs, 2)):
            return False
    return True


def _fetch(fetch: Fetch, model: str, source: object) -> object:
    if not isinstance(source, dict) or not isinstance(source.get("id"), str):
        raise RefreshError(f"{model}: openrouter entry needs an 'id'")
    try:
        return fetch(source["id"])
    except (urllib.error.URLError, http.client.HTTPException, OSError,
            ValueError) as exc:
        raise RefreshError(f"{model}: fetching {source['id']} failed: {exc}") from exc


@dataclass
class Result:
    """A run's outcome: the file it would write, and what it reports."""
    doc: dict
    moves: list[Move]
    vanished: list[tuple[str, str]]
    refusals: list[str]
    notices: list[str]
    sampled: dict[str, dict[str, str]]


@dataclass
class ModelResult:
    """One model's entries and report details for a refresh run."""
    moves: list[Move]
    vanished: list[tuple[str, str]]
    refusals: list[str]
    notices: list[str]
    sampled: dict[str, str]


@dataclass(frozen=True)
class RefreshContext:
    """Shared inputs for the models in one refresh run."""
    doc: dict
    fetch: Fetch
    region: str | None
    stamp: str
    at: datetime


@dataclass
class ListedModel:
    """One model's endpoint rows before log classification."""
    payload: object
    rows: dict[str, Listing]
    refused: dict[str, str]
    notices: list[str]
    untracked: dict[str, str]


def _same_rates(left: dict, right: dict) -> bool:
    left_rates, right_rates = effective_rates(left), effective_rates(right)
    return all(round(float(left_rates[field]), 10) == round(float(right_rates[field]), 10)
               for field in RATE_FIELDS)


def _listed_model(context: RefreshContext, model: str, source: dict,
                  hosts: dict) -> ListedModel:
    payload = _fetch(context.fetch, model, source)
    rows, refused, notices, untracked = listed_rows(
        model, payload, context.region, source.get("resolve", {}),
        {host: effective_rates(history[-1])
         for host, history in hosts.items()}, context.at)
    return ListedModel(payload, rows, refused, notices, untracked)


def _delisted(source: dict, catalog: set[str] | None) -> bool:
    """Whether OpenRouter's /api/v1/models catalog no longer lists the
    tracked id. A catalog that could not be read (None) proves nothing."""
    return (catalog is not None and isinstance(source.get("id"), str)
            and source["id"] not in catalog)


def _refresh_model(context: RefreshContext, model: str, source: dict,
                   read: refresh_pricelog.LogRead) -> ModelResult:
    """Refresh one model while keeping refusals local to that model."""
    hosts = context.doc["providers"].setdefault(model, {})
    try:
        listed = _listed_model(context, model, source, hosts)
    except RefreshError as exc:
        return ModelResult([], [], [str(exc)], [], {})
    deferred_notices: list[str] = []
    logged = refresh_logrun.classify_log_rows(
        model, listed.payload, listed.rows, hosts, read, context.region,
        source.get("resolve", {}),
        partial(_append_logged, deferred_notices=deferred_notices),
        _same_rates, context.at)
    moves = logged.moves + _append(
        model, hosts, logged.sampled_rows, context.stamp, context.at, listed.notices)
    notices = listed.notices + list(listed.untracked.values()) + logged.notices
    notices += deferred_notices
    notices += [f"non-uniform schedule: Token Breakdown split is approximate "
                f"for {model} via {move.host}"
                for move in moves if not _scales_alike(move.new)]
    vanished = [(model, host) for host in hosts
                if host not in listed.rows and host not in listed.refused
                and host not in listed.untracked]
    return ModelResult(moves, vanished, list(listed.refused.values()), notices, logged.sampled)


def refresh(doc: dict, fetch: Fetch, stamp: str,
            fetch_models: FetchModels | None = None,
            fetch_log: FetchLog | None = None) -> Result:
    """Append each log-backed move at its change time and sample the rest."""
    try:
        pricing.load_tables(doc)
    except ValueError as exc:
        raise RefreshError(f"the source file would not load: {exc}") from exc
    source_doc = expand_pricing_doc(doc)
    tracked, region = _sources(source_doc)
    logs, catalog = refresh_pricelog.read_logs(tracked, fetch_models, fetch_log)
    context = RefreshContext(
        source_doc, fetch, region, stamp,
        datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc))
    result = _refresh_models(context, tracked, logs, catalog)
    # The loaders' own rules, run on what would be written: among them, a
    # detection time not after a row's newest entry.
    try:
        pricing.load_tables(result.doc)
    except ValueError as exc:
        raise RefreshError(f"the refreshed file would not load: {exc}") from exc
    return result


def _refresh_models(context: RefreshContext, tracked: dict,
                    logs: dict, catalog: set[str] | None) -> Result:
    """Collect each tracked model's changes, refusals, and notices."""
    result = Result(context.doc, [], [], [], [], {})
    for model, source in tracked.items():
        # Every model is fetched even after one is refused, so a red run
        # names everything a human must look at. A model the catalog no
        # longer lists is delisted: skipped with a notice, row kept — it
        # rejoins on its own when the catalog relists it.
        if _delisted(source, catalog):
            result.notices.append(
                f"{model} ({source['id']}): delisted from OpenRouter's catalog; "
                "row kept, not fetched")
            continue
        outcome = _refresh_model(context, model, source, logs[model])
        result.moves += outcome.moves
        result.vanished += outcome.vanished
        result.refusals += outcome.refusals
        result.notices += outcome.notices
        result.sampled[model] = outcome.sampled
    if result.moves:
        result.doc["provider_rates_fetched"] = context.stamp
    return result


def _refusals(result: Result, outcome) -> list[str]:
    """Both passes' refusals, the provider pass's own when no vendor pass
    ran."""
    return result.refusals + (outcome.refusals if outcome is not None else [])


def main(argv: list[str] | None = None, *, fetch: Fetch = refresh_pricelog.fetch_endpoints,
         fetch_models: FetchModels = refresh_pricelog.fetch_models,
         fetch_log: FetchLog = refresh_pricelog.fetch_listed_pricing,
         now: datetime | None = None, pricing_path: Path = PRICING_JSON,
         constants_path: Path = CONSTANTS_PY,
         vendor: Callable | None = refresh_vendor_rates.vendor_pass) -> int:
    args = _arguments(argv)
    stamp = detection_stamp(now or datetime.now(timezone.utc))
    try:
        result = refresh(json.loads(pricing_path.read_text(encoding="utf-8")), fetch, stamp,
                         fetch_models, fetch_log)
        # The vendor pass runs on the provider pass's own document, after its
        # loader validation; it validates the would-be file again itself.
        outcome = (vendor(result.doc, fetch_models, fetch)
                   if vendor is not None else None)
        constants = constants_path.read_text(encoding="utf-8")
        if result.moves or (outcome is not None and outcome.moves):
            constants = bump_pricing_version(constants)
    except RefreshError as exc:
        print(f"refresh_provider_rates: {exc}", file=sys.stderr)
        return 1
    body = report(stamp, result, result.doc["openrouter"]["models"])
    if outcome is not None and (outcome.moves or outcome.refusals or outcome.notices):
        body += "\n\n" + vendor_report(stamp, outcome)
    print(body)
    if (result.moves or (outcome is not None and outcome.moves)) \
            and not args.dry_run:
        pricing_path.write_text(serialize_pricing_doc(result.doc),
                                encoding="utf-8", newline="\n")
        constants_path.write_text(constants, encoding="utf-8", newline="\n")
        if args.commit_msg:
            args.commit_msg.write_text(
                commit_message(result, body, outcome), encoding="utf-8",
                newline="\n")
    # Every other move is written; the run is still red, so a human sees it.
    if _refusals(result, outcome):
        print("\n".join(f"refresh_provider_rates: {r}"
                        for r in _refusals(result, outcome)),
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
