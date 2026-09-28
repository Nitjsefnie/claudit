#!/usr/bin/env python3
"""Refresh src/pricing.json's OpenRouter provider rates (SV-RATE-REFRESH).

Fetches every tracked model's endpoints and listed-pricing log. A host with
one provable endpoint history appends each logged rate change at OpenRouter's
change time; its first row carries the whole log. Hosts without an unambiguous
log join are sampled at detection time and use the existing alternation rule.
No existing entry is rewritten by the hourly refresh. A run that appends
bumps PRICING_VERSION (a reprice, never a reparse) by one from whatever
backend/constants.py holds and moves provider_rates_fetched; a quiet run
writes nothing.

Only endpoints in the account's data region count (tag_region). A host's
weekly time-of-day prices (pricing.overrides) become its entry's schedule;
its top-level price is the entry's default only when the fetch falls
outside every window, since inside one it is that window's price.
Tag/region normalisation and whole-week schedule coverage live in
refresh_prices.py and are imported here.
These refuse the host or model they concern, which appends nothing:
- a host with two endpoints in that region at different prices and no
  resolution for it (a tag, or "cheapest" of otherwise identical twins);
- a resolution that no longer applies;
- a price or override kind this script does not model, or a response
  shape it does not recognise;
- a host seen for the first time while the fetch falls inside one of its
  schedule's windows, unless its schedule covers the whole week: then no
  record is priced by the entry default and it starts as the listed top-level
  price; otherwise that price is a window price and the next fetch outside
  every window starts the row;
- a tracked model with no endpoints, or none in the region.

Every other sampled move is still written, then the script exits nonzero
naming each refusal. A detection time not after a sampled row's newest
entry writes nothing at all. The one-time, human-reviewed history rewrite
lives in backfill_provider_rates.py.

    python3 scripts/ci/refresh_provider_rates.py [--dry-run] [--commit-msg FILE]
"""
from __future__ import annotations

import copy
import json
import sys
import urllib.error
from dataclasses import dataclass
from itertools import combinations
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
# pylint: disable=wrong-import-position
sys.path.insert(0, str(Path(__file__).resolve().parent))
import refresh_alternation  # noqa: E402
import refresh_logrun  # noqa: E402
import refresh_pricelog  # noqa: E402
from refresh_pricelog import (FetchEndpoints as Fetch, FetchLog, FetchModels)  # noqa: E402
from refresh_common import (bump_pricing_version,  # noqa: E402
                            detection_stamp, sources as _sources)
from refresh_report import arguments as _arguments  # noqa: E402
from refresh_report import commit_message, report  # noqa: E402
from refresh_prices import (PRICED, RefreshError, as_listed, covers_week,  # noqa: E402
                            entry_schedule, in_a_window, is_zero, rates_of, tag_region,
                            unknown_suffixes)
from backend import pricing  # noqa: E402

PRICING_JSON = REPO_ROOT / "src" / "pricing.json"
CONSTANTS_PY = REPO_ROOT / "backend" / "constants.py"
RATE_FIELDS = pricing.RATE_FIELDS
_IDENTITY = ("tag", "quantization", "context_length", "max_completion_tokens",
             "max_prompt_tokens")
# Price order for "select": "cheapest": cache read, then input, then output.
_ORDER = ("read", "fresh", "output")


@dataclass(frozen=True)
class Listing:
    """One listed endpoint, normalised to the table's shape."""
    tag: str
    identity: str
    rates: dict
    schedule: list | None
    discount: Decimal

    @property
    def price(self) -> str:
        return json.dumps([self.rates, self.schedule], sort_keys=True)


@dataclass
class Move:
    """One provider row the run appends to (old is None for a new host)."""
    model: str
    host: str
    old: dict | None
    new: Listing
    entries_appended: int = 1
    source: str = "sampled"


def _listing(endpoint: object, where: str, at: datetime, kept: dict | None) -> Listing:
    """One listed endpoint at the fetch instant `at`. The listed price
    already has any promotional discount applied; the discount is kept only
    as the note beside it.

    A scheduled host's top-level price is the window active at `at`, not a
    stable default. So inside a window the row's own default, `kept`, stays
    the default, and a price a window does not name is that default's. Only
    a fetch outside every window reads the default from the listing. A host
    with no row yet is refused inside a window unless its schedule covers
    every instant of the week: then no record is priced by the entry default,
    so that default starts as the listed top-level price. Otherwise starting
    with a window's price would misprice every outside-window record. The
    trigger is per endpoint, before the region filter: an out-of-region
    endpoint of a first-seen host, one the account is never billed by,
    carrying a schedule and fetched inside its window can refuse the whole
    host."""
    if not (isinstance(endpoint, dict) and isinstance(endpoint.get("tag"), str)
            and isinstance(endpoint.get("pricing"), dict)):
        raise RefreshError(f"{where}: unrecognised endpoint shape")
    price = endpoint["pricing"]
    for key, value in price.items():
        if key not in (*PRICED, "discount", "overrides") and not is_zero(value):
            raise RefreshError(f"{where}: pricing {key} {value!r} is not modelled")
    discount = price.get("discount", 0)
    if (not isinstance(discount, (int, float)) or isinstance(discount, bool)
            or not 0 <= discount < 1):
        raise RefreshError(f"{where}: discount {discount!r} is not a fraction")
    # What tells two of one host's endpoints apart when the price does not.
    identity = json.dumps([endpoint.get(k) for k in _IDENTITY])
    rates, schedule = rates_of(price, where), entry_schedule(price, where)
    if schedule and in_a_window(schedule, at):
        if kept is None and not covers_week(schedule):
            raise RefreshError(
                f"{where}: first seen inside one of its windows: the listed "
                "top-level price is the active window's, not a default; the "
                "next fetch outside every window starts the row")
        if kept is not None:
            rates = kept
            schedule = entry_schedule({**as_listed(kept), "overrides": price["overrides"]}, where)
    return Listing(endpoint["tag"], identity, rates, schedule, Decimal(str(discount)))


def listed_rows(model: str, payload: object, region: str | None, resolutions: dict,
                stored: dict[str, dict], at: datetime
                ) -> tuple[dict[str, Listing], dict[str, str], list[str]]:
    """Each host's one listing for `model`, each refused host's reason, and
    notices for a human that refuse nothing.

    Endpoints are grouped by host before any is read, so a malformed one
    refuses its own host only. A host's endpoints outside the data `region`
    (None: global, untagged by region) are not ones the account is billed
    by, unless the file pins that host to a tag. Endpoints at one price are
    one row; several prices left over need a resolution in the file, or
    that host is refused rather than guessed. `stored` is each host's
    current rates, which is how a price-order resolution sees its order,
    and the default a scheduled host fetched inside a window keeps; `at`
    is the fetch instant.
    """
    rows, refused, notices = {}, {}, []
    for host, endpoints in _by_host(model, payload).items():
        try:
            chosen, host_notices = _host_row(f"{model} via {host}", endpoints, region,
                                             resolutions.get(host), stored.get(host), at)
        except RefreshError as exc:
            refused[host] = str(exc)
            continue
        notices += host_notices
        if chosen is not None:
            rows[host] = chosen
    if not rows and not refused:
        raise RefreshError(f"{model}: no endpoint in the data region")
    return rows, refused, notices


def _by_host(model: str, payload: object) -> dict[str, list[tuple[int, object]]]:
    """The payload's endpoints (with their index), grouped by host."""
    by_host: dict[str, list[tuple[int, object]]] = {}
    for i, endpoint in enumerate(_endpoints_of(model, payload)):
        host = endpoint.get("provider_name") if isinstance(endpoint, dict) else None
        if not isinstance(host, str):
            raise RefreshError(f"{model} endpoint {i}: names no provider")
        by_host.setdefault(host, []).append((i, endpoint))
    return by_host


def _host_row(where: str, endpoints: list[tuple[int, object]], region: str | None,
              pin: object, stored: dict | None,
              at: datetime) -> tuple[Listing | None, list[str]]:
    """One host's listing, and its notices; RefreshError refuses the host."""
    listed = [_listing(e, f"{where} (endpoint {i})", at, stored) for i, e in endpoints]
    chosen, notice = _host_price(where, listed, region, pin, stored)
    notices = [f"{where}: tag {tag!r} names neither a known region nor a quantization"
               for tag in sorted({e.tag for e in listed if unknown_suffixes(e.tag)})]
    if (chosen is not None and stored is None and chosen.schedule
            and in_a_window(chosen.schedule, at)):
        notices.append(
            f"{where}: first seen inside one of its windows, whose schedule covers the "
            "whole week: no record is ever priced by the entry default, so the default "
            "starts as the listed top-level price")
    return chosen, notices + ([notice] if notice else [])


def _endpoints_of(model: str, payload: object) -> list:
    data = payload.get("data") if isinstance(payload, dict) else None
    endpoints = data.get("endpoints") if isinstance(data, dict) else None
    if not isinstance(endpoints, list):
        raise RefreshError(f"{model}: unrecognised response shape")
    if not endpoints:
        raise RefreshError(f"{model}: no endpoints listed; read as a broken fetch, "
                           "never as every host vanishing")
    return endpoints


def _host_price(where: str, listed: list[Listing], region: str | None, pin: object,
                stored: dict | None) -> tuple[Listing | None, str | None]:
    """A host's one listing (None when it lists nothing the account can
    use), and a notice when a price-order resolution may have switched."""
    override = _override(where, pin)
    if override.get("tag") is not None:
        chosen = [e for e in listed if e.tag == override["tag"]]
        if not chosen:
            tags = ", ".join(sorted({e.tag for e in listed}))
            raise RefreshError(f"{where}: pinned tag {override['tag']!r} is not listed "
                               f"({tags})")
        listed = chosen
    else:
        listed = [e for e in listed if tag_region(e.tag) == region]
    prices = list({e.price: e for e in reversed(listed)}.values())
    if len(prices) > 1 and override.get("select") == "cheapest":
        return _cheapest(where, prices, stored)
    if len(prices) > 1:
        tags = ", ".join(sorted({e.tag or "(untagged)" for e in listed}))
        raise RefreshError(f"{where}: {len(listed)} endpoints ({tags}) at "
                           f"{len(prices)} different prices; resolve it in "
                           "openrouter.models.<model>.resolve")
    return (prices[0] if prices else None), None


def _override(where: str, pin: object) -> dict:
    """A per-host resolution: {"tag": ...} takes that endpoint whatever its
    region; {"select": "cheapest"} takes the cheaper of otherwise identical
    endpoints. Either carries a "why". Never a price: a pin on a price stops
    matching the moment that price moves."""
    if pin is None:
        return {}
    if isinstance(pin, dict) and set(pin) - {"why"} in ({"tag"}, {"select"}):
        if isinstance(pin.get("tag"), str) or pin.get("select") == "cheapest":
            return pin
    raise RefreshError(f"{where}: a resolution is keyed on 'tag' or 'select': "
                       f"'cheapest' (with a 'why'), never a price: {pin!r}")


def _cheapest(where: str, prices: list[Listing],
              stored: dict | None) -> tuple[Listing, str | None]:
    """The cheaper of endpoints that differ in nothing but price, ordered by
    cache read, then input, then output.

    The price is the only identity such twins have, so the endpoint the
    row tracks is the one still listed at the row's price. When that one is
    no longer the cheaper, the order has flipped and a human must look; an
    equal order between different prices is refused the same way.

    A rise is reported, not refused. The tracked twin moving above the
    other looks exactly like the other becoming the row's price, and since
    the other was the dearer one, that always shows as the row's price
    rising (unless the other fell at the same time). No data can tell it
    from a genuine rise.
    """
    if len({e.identity for e in prices}) > 1:
        raise RefreshError(f"{where}: 'cheapest' applies only to endpoints identical "
                           "in tag, quantization and limits; these differ")
    ranked = sorted(prices, key=lambda e: [e.rates[f] for f in _ORDER])
    chosen, other = ranked[0], ranked[1]
    if [chosen.rates[f] for f in _ORDER] == [other.rates[f] for f in _ORDER]:
        raise RefreshError(f"{where}: a tie in price order between different prices")
    if stored is not None and any(e.rates == stored for e in ranked[1:]):
        raise RefreshError(f"{where}: the price order flipped: the endpoint at the "
                           "row's price is no longer the cheaper")
    notice = None
    if stored is not None and [chosen.rates[f] for f in _ORDER] > [stored[f] for f in _ORDER]:
        moved = ", ".join(f"{f} {stored[f]!r} → {chosen.rates[f]!r}"
                          for f in _ORDER if chosen.rates[f] != stored[f])
        notice = (f"possible twin switch: {where}: the price rose ({moved}); the other "
                  f"twin is at {', '.join(f'{f} {other.rates[f]!r}' for f in _ORDER)}. "
                  "Check which endpoint the row tracks")
    return chosen, notice


def _entry(stamp: str, listing: Listing) -> dict:
    entry = {"from": stamp, **listing.rates}
    if listing.discount:
        entry["note"] = _discount_note(listing.discount)
    if listing.schedule:
        entry["schedule"] = listing.schedule
    return entry


def _discount_note(discount: Decimal) -> str:
    return f"{format((discount * 100).normalize(), 'f')}% off"


def _append(model: str, hosts: dict, rows: dict[str, Listing], stamp: str,
            at: datetime, notices: list[str]) -> list[Move]:
    """Append each moved price (a rate or the schedule, compared as a
    whole) to its row, and start a row for each new host, in place; the
    moves made, with an alternating price's notice appended to `notices`
    instead of an entry to the row (SV-RATE-REFRESH)."""
    moves = []
    for host, listing in rows.items():
        history = hosts.get(host)
        if history is None:
            hosts[host] = [_entry(stamp, listing)]
            moves.append(Move(model, host, None, listing))
            continue
        newest = history[-1]
        if ({f: newest[f] for f in RATE_FIELDS} == listing.rates
                and newest.get("schedule") == listing.schedule):
            continue
        notice = refresh_alternation.notice(
            history, listing.rates, listing.schedule, newest, at,
            f"{model} via {host}")
        if notice:
            notices.append(notice)
            continue
        history.append(_entry(stamp, listing))
        moves.append(Move(model, host, newest, listing))
    return moves


def _price_instant(stamp: object, where: str) -> datetime:
    """Parse an entry timestamp with the pricing loader's rules."""
    return pricing._instant(stamp, where)  # pylint: disable=protected-access


def _append_logged(model: str, hosts: dict, host: str, listing: Listing,
                   entries: list[dict]) -> Move | None:
    """Append every new log state after the stored row without rewriting it."""
    history = hosts.get(host)
    if history is None:
        hosts[host] = copy.deepcopy(entries)
        return Move(model, host, None, listing, len(entries), "log")
    newest = history[-1]
    newest_from = newest["from"]
    newest_at = (_price_instant(newest_from, f"{model} via {host}")
                 if newest_from is not None else None)
    previous = {field: newest[field] for field in RATE_FIELDS}
    additions = []
    for entry in entries:
        entry_at = _price_instant(entry["from"], f"{model} via {host}")
        if newest_at is not None and entry_at <= newest_at:
            continue
        rates = {field: entry[field] for field in RATE_FIELDS}
        if rates == previous:
            continue
        additions.append(copy.deepcopy(entry))
        previous = rates
    if not additions:
        return None
    history.extend(additions)
    return Move(model, host, newest, listing, len(additions), "log")


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
    except (urllib.error.URLError, OSError, ValueError) as exc:
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


def _same_rates(left: dict, right: dict) -> bool:
    return all(round(float(left[field]), 10) == round(float(right[field]), 10)
               for field in RATE_FIELDS)


def _listed_model(context: RefreshContext, model: str, source: dict,
                  hosts: dict) -> ListedModel:
    payload = _fetch(context.fetch, model, source)
    rows, refused, notices = listed_rows(
        model, payload, context.region, source.get("resolve", {}),
        {host: {field: history[-1][field] for field in RATE_FIELDS}
         for host, history in hosts.items()}, context.at)
    return ListedModel(payload, rows, refused, notices)


def _refresh_model(context: RefreshContext, model: str, source: dict,
                   read: refresh_pricelog.LogRead) -> ModelResult:
    """Refresh one model while keeping refusals local to that model."""
    hosts = context.doc["providers"].setdefault(model, {})
    try:
        listed = _listed_model(context, model, source, hosts)
    except RefreshError as exc:
        return ModelResult([], [], [str(exc)], [], {})
    logged = refresh_logrun.classify_log_rows(
        model, listed.payload, listed.rows, hosts, read, context.region,
        source.get("resolve", {}), _append_logged, _same_rates)
    moves = logged.moves + _append(
        model, hosts, logged.sampled_rows, context.stamp, context.at, listed.notices)
    notices = listed.notices + logged.notices
    notices += [f"non-uniform schedule: Token Breakdown split is approximate "
                f"for {model} via {move.host}"
                for move in moves if not _scales_alike(move.new)]
    vanished = [(model, host) for host in hosts
                if host not in listed.rows and host not in listed.refused]
    return ModelResult(moves, vanished, list(listed.refused.values()), notices, logged.sampled)


def refresh(doc: dict, fetch: Fetch, stamp: str,
            fetch_models: FetchModels | None = None,
            fetch_log: FetchLog | None = None) -> Result:
    """Append each log-backed move at its change time and sample the rest."""
    tracked, region = _sources(doc)
    at = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    logs = refresh_pricelog.read_logs(tracked, fetch_models, fetch_log)
    result = Result(copy.deepcopy(doc), [], [], [], [], {})
    context = RefreshContext(result.doc, fetch, region, stamp, at)
    for model, source in tracked.items():
        # Every model is fetched even after one is refused, so a red run
        # names everything a human must look at.
        outcome = _refresh_model(context, model, source, logs[model])
        result.moves += outcome.moves
        result.vanished += outcome.vanished
        result.refusals += outcome.refusals
        result.notices += outcome.notices
        result.sampled[model] = outcome.sampled
    if result.moves:
        result.doc["provider_rates_fetched"] = stamp
    # The loaders' own rules, run on what would be written: among them, a
    # detection time not after a row's newest entry.
    try:
        pricing.load_tables(result.doc)
    except ValueError as exc:
        raise RefreshError(f"the refreshed file would not load: {exc}") from exc
    return result


def main(argv: list[str] | None = None, *, fetch: Fetch = refresh_pricelog.fetch_endpoints,
         fetch_models: FetchModels = refresh_pricelog.fetch_models,
         fetch_log: FetchLog = refresh_pricelog.fetch_listed_pricing,
         now: datetime | None = None, pricing_path: Path = PRICING_JSON,
         constants_path: Path = CONSTANTS_PY) -> int:
    args = _arguments(argv)
    stamp = detection_stamp(now or datetime.now(timezone.utc))
    try:
        result = refresh(json.loads(pricing_path.read_text(encoding="utf-8")), fetch, stamp,
                         fetch_models, fetch_log)
        constants = constants_path.read_text(encoding="utf-8")
        if result.moves:
            constants = bump_pricing_version(constants)
    except RefreshError as exc:
        print(f"refresh_provider_rates: {exc}", file=sys.stderr)
        return 1
    body = report(stamp, result, result.doc["openrouter"]["models"])
    print(body)
    if result.moves and not args.dry_run:
        pricing_path.write_text(json.dumps(result.doc, indent=2, sort_keys=True) + "\n",
                                encoding="utf-8")
        constants_path.write_text(constants, encoding="utf-8")
        if args.commit_msg:
            args.commit_msg.write_text(commit_message(result, body), encoding="utf-8")
    # Every other move is written; the run is still red, so a human sees it.
    if result.refusals:
        print("\n".join(f"refresh_provider_rates: {r}" for r in result.refusals),
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
