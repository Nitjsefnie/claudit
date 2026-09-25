#!/usr/bin/env python3
"""Refresh src/pricing.json's OpenRouter provider rates (SV-RATE-REFRESH).

Fetches every tracked model's endpoints from OpenRouter's public API and
APPENDS an entry, effective from the detection time, to each provider row
whose price moved; a host seen for the first time gets a row that begins
then. No existing entry is ever rewritten. A run that appends bumps
PARSER_VERSION in backend/constants.py by one from whatever it holds, and
moves provider_rates_fetched; a run that appends nothing writes nothing.

Only endpoints in the account's data region count (tag_region). A host's
weekly time-of-day prices (pricing.overrides) become its entry's schedule;
its top-level price is the entry's default only when the fetch falls
outside every window, since inside one it is that window's price.
These refuse the host or model they concern, which appends nothing:
- a host with two endpoints in that region at different prices and no
  resolution for it (a tag, or "cheapest" of otherwise identical twins);
- a resolution that no longer applies;
- a price or override kind this script does not model, or a response
  shape it does not recognise;
- a tracked model with no endpoints, or none in the region.

Every other move is still written, then the script exits nonzero naming
each refusal. A detection time not after a row's newest entry writes
nothing at all.

    python3 scripts/ci/refresh_provider_rates.py [--dry-run] [--commit-msg FILE]
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from itertools import combinations
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
# pylint: disable=wrong-import-position
from backend import pricing  # noqa: E402

PRICING_JSON = REPO_ROOT / "src" / "pricing.json"
CONSTANTS_PY = REPO_ROOT / "backend" / "constants.py"
API_URL = "https://openrouter.ai/api/v1/models/{}/endpoints"
RATE_FIELDS = pricing.RATE_FIELDS
_PARSER_VERSION = re.compile(r'^PARSER_VERSION = "(\d+)"$', re.MULTILINE)
# An endpoint tag is `host` or `host/<suffix>[/<suffix>...]`: quantizations
# and data regions. A region is one of these codes, alone or qualified by an
# area and a number (us, us-east, us-east-1), in any case.
_REGIONS = ("us", "eu", "uk", "ca", "au", "ap", "jp", "sg", "in", "br", "de", "fr",
            "nl", "kr", "cn", "hk", "tw", "me", "sa", "za", "asia", "apac", "emea",
            "latam")
_REGION = re.compile(rf"(?:{'|'.join(_REGIONS)})(?:-[a-z]+(?:-[0-9]+)?)?",
                     re.IGNORECASE)
_QUANTIZATIONS = frozenset({"fp4", "fp6", "fp8", "fp16", "fp32", "bf16", "nvfp4",
                            "mxfp4", "int4", "int8", "awq", "gptq"})
# The prices this script models. Any other pricing key listed at a nonzero
# price (a per-request fee, an image price) refuses its host.
_PRICED = ("prompt", "completion", "input_cache_read", "input_cache_write")
_OVERRIDE_KEYS = frozenset({"utc_days", "utc_start", "utc_end", *_PRICED})
_IDENTITY = ("tag", "quantization", "context_length", "max_completion_tokens",
             "max_prompt_tokens")
# Price order for "select": "cheapest": cache read, then input, then output.
_ORDER = ("read", "fresh", "output")

Fetch = Callable[[str], object]


class RefreshError(Exception):
    """A host, a model or a run a human must look at: it writes nothing."""


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


def fetch_endpoints(model_id: str) -> object:
    request = urllib.request.Request(
        API_URL.format(model_id),
        headers={"User-Agent": "claudit-refresh-provider-rates"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def detection_stamp(now: datetime) -> str:
    """Whole seconds in UTC: the one spelling both loaders accept."""
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _per_million(price: object, where: str) -> float:
    """OpenRouter lists USD per token as a decimal string; the table is
    USD per million tokens. Decimal keeps "0.0000001275" exactly 0.1275."""
    try:
        value = Decimal(price) if isinstance(price, str) else None
    except InvalidOperation:
        value = None
    if value is None or not value.is_finite() or value < 0:
        raise RefreshError(f"{where}: price {price!r} is not a non-negative decimal string")
    return float(value.scaleb(6))


def _is_zero(value: object) -> bool:
    try:
        return not isinstance(value, bool) and Decimal(str(value)) == 0
    except InvalidOperation:
        return False


def _rates(price: dict, where: str) -> dict:
    """The five rates of one price. Cache writes take the listed write
    price when it is nonzero, the input rate otherwise; no listed cache-read
    price is 0."""
    fresh = _per_million(price.get("prompt"), where)
    output = _per_million(price.get("completion"), where)
    read = _per_million(price["input_cache_read"], where) if "input_cache_read" in price else 0.0
    write = (_per_million(price["input_cache_write"], where)
             if "input_cache_write" in price else 0.0)
    create = write or fresh
    return {"fresh": fresh, "create_5m": create, "create_1h": create,
            "read": read, "output": output}


def _as_listed(rates: dict) -> dict:
    """The pricing OpenRouter lists for `rates`: _rates' inverse."""
    price = {key: format(Decimal(repr(rates[f])).scaleb(-6).normalize(), "f")
             for key, f in (("prompt", "fresh"), ("completion", "output"),
                            ("input_cache_read", "read"))}
    if rates["create_5m"] != rates["fresh"]:
        price["input_cache_write"] = format(
            Decimal(repr(rates["create_5m"])).scaleb(-6).normalize(), "f")
    return price


def _schedule(price: dict, where: str) -> list | None:
    """The entry schedule for OpenRouter's pricing.overrides: weekly UTC
    windows (utc_days, utc_start/utc_end as HHMM), each with the prices it
    overrides; a price it does not name is the endpoint's own."""
    overrides = price.get("overrides")
    if overrides is None or overrides == []:
        return None
    if not isinstance(overrides, list) or not all(isinstance(o, dict) for o in overrides):
        raise RefreshError(f"{where}: pricing.overrides is not a list of windows")
    schedule = []
    for override in overrides:
        unknown = set(override) - _OVERRIDE_KEYS
        if unknown:
            raise RefreshError(f"{where}: override kind not modelled: {sorted(unknown)}")
        window: dict = {"rates": _rates({**{k: price[k] for k in _PRICED if k in price},
                                         **{k: override[k] for k in _PRICED if k in override}},
                                        where)}
        if "utc_days" in override:
            window["days"] = override["utc_days"]
        if "utc_start" in override or "utc_end" in override:
            window["start"] = override.get("utc_start")
            window["end"] = override.get("utc_end")
        schedule.append(window)
    try:
        pricing._schedule(schedule, where)  # pylint: disable=protected-access
    except ValueError as exc:
        raise RefreshError(f"{where}: pricing.overrides: {exc}") from exc
    return schedule


def _in_a_window(schedule: list, at: datetime) -> bool:
    # pylint: disable-next=protected-access
    return pricing._scheduled(pricing._schedule(schedule, "schedule"), at) is not None


def _listing(endpoint: object, where: str, at: datetime, kept: dict | None) -> Listing:
    """One listed endpoint at the fetch instant `at`. The listed price
    already has any promotional discount applied; the discount is kept only
    as the note beside it.

    A scheduled host's top-level price is the window active at `at`, not a
    stable default. So inside a window the row's own default, `kept`, stays
    the default, and a price a window does not name is that default's. Only
    a fetch outside every window reads the default from the listing."""
    if not (isinstance(endpoint, dict) and isinstance(endpoint.get("tag"), str)
            and isinstance(endpoint.get("pricing"), dict)):
        raise RefreshError(f"{where}: unrecognised endpoint shape")
    price = endpoint["pricing"]
    for key, value in price.items():
        if key not in (*_PRICED, "discount", "overrides") and not _is_zero(value):
            raise RefreshError(f"{where}: pricing {key} {value!r} is not modelled")
    discount = price.get("discount", 0)
    if (not isinstance(discount, (int, float)) or isinstance(discount, bool)
            or not 0 <= discount < 1):
        raise RefreshError(f"{where}: discount {discount!r} is not a fraction")
    # What tells two of one host's endpoints apart when the price does not.
    identity = json.dumps([endpoint.get(k) for k in _IDENTITY])
    rates, schedule = _rates(price, where), _schedule(price, where)
    if schedule and kept is not None and _in_a_window(schedule, at):
        rates = kept
        schedule = _schedule({**_as_listed(kept), "overrides": price["overrides"]}, where)
    return Listing(endpoint["tag"], identity, rates, schedule, Decimal(str(discount)))


def tag_region(tag: str) -> str | None:
    """The data region an endpoint tag names, or None for a global one."""
    for suffix in tag.split("/")[1:]:
        if _REGION.fullmatch(suffix):
            return suffix.lower()
    return None


def _unknown_suffixes(tag: str) -> list[str]:
    return [s for s in tag.split("/")[1:]
            if not _REGION.fullmatch(s) and s.lower() not in _QUANTIZATIONS]


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
               for tag in sorted({e.tag for e in listed if _unknown_suffixes(e.tag)})]
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


def _append(model: str, hosts: dict, rows: dict[str, Listing], stamp: str) -> list[Move]:
    """Append each moved price (a rate or the schedule, compared as a
    whole) to its row, and start a row for each new host, in place; the
    moves made."""
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
        history.append(_entry(stamp, listing))
        moves.append(Move(model, host, newest, listing))
    return moves


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


def data_region(config: object) -> str | None:
    """openrouter.data_region: "global" (None: endpoints no region tag
    names) or the lowercase region code whose tagged endpoints the account
    uses."""
    region = config.get("data_region") if isinstance(config, dict) else None
    if region == "global":
        return None
    if isinstance(region, str) and region == region.lower() and _REGION.fullmatch(region):
        return region
    raise RefreshError(f"openrouter.data_region {region!r} is neither 'global' "
                       "nor a region code")


def _sources(doc: dict) -> tuple[dict, str | None]:
    """The tracked models and the data region, from the openrouter section."""
    config = doc.get("openrouter")
    tracked = config.get("models") if isinstance(config, dict) else None
    if not isinstance(tracked, dict) or not set(doc["providers"]) <= set(tracked):
        raise RefreshError("every provider-table model needs an openrouter.models id")
    return tracked, data_region(config)


@dataclass
class Result:
    """A run's outcome: the file it would write, and what it reports."""
    doc: dict
    moves: list[Move]
    vanished: list[tuple[str, str]]
    refusals: list[str]
    notices: list[str]


def refresh(doc: dict, fetch: Fetch, stamp: str) -> Result:
    """The file with every move appended, and what the run reports. A
    refused host, or a refused model, blocks only itself: its rows are left
    untouched and every other move stands."""
    tracked, region = _sources(doc)
    at = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    result = Result(copy.deepcopy(doc), [], [], [], [])
    for model, source in tracked.items():
        # Every model is fetched even after one is refused, so a red run
        # names everything a human must look at.
        hosts = result.doc["providers"].setdefault(model, {})
        try:
            rows, refused, notices = listed_rows(
                model, _fetch(fetch, model, source), region, source.get("resolve", {}),
                {host: {f: h[-1][f] for f in RATE_FIELDS} for host, h in hosts.items()}, at)
        except RefreshError as exc:
            result.refusals.append(str(exc))
            continue
        result.refusals += refused.values()
        result.notices += notices
        moves = _append(model, hosts, rows, stamp)
        result.moves += moves
        result.notices += [f"non-uniform schedule: Token Breakdown split is approximate "
                           f"for {model} via {m.host}" for m in moves if not _scales_alike(m.new)]
        result.vanished += [(model, host) for host in hosts
                            if host not in rows and host not in refused]
    if result.moves:
        result.doc["provider_rates_fetched"] = stamp
    # The loaders' own rules, run on what would be written: among them, a
    # detection time not after a row's newest entry.
    try:
        pricing.load_tables(result.doc)
    except ValueError as exc:
        raise RefreshError(f"the refreshed file would not load: {exc}") from exc
    return result


def bump_parser_version(text: str, stamp: str) -> str:
    """PARSER_VERSION one past whatever the file holds, with its history
    line, so a manual bump that lands first is never collided with."""
    found = _PARSER_VERSION.findall(text)
    if len(found) != 1:
        raise RefreshError("backend/constants.py: expected exactly one PARSER_VERSION line")
    version = int(found[0]) + 1
    return _PARSER_VERSION.sub(
        f"# {version} appends the OpenRouter provider rates detected at {stamp};\n"
        "# the bump reparses every file so a record from then on that was\n"
        "# ingested before this commit reached the deploy takes the new rate.\n"
        f'PARSER_VERSION = "{version}"', text)


def _move_text(move: Move) -> str:
    new = move.new
    off = f" ({_discount_note(new.discount)})" if new.discount else ""
    windows = f", schedule of {len(new.schedule)} windows" if new.schedule else ""
    if move.old is None:
        rates = ", ".join(f"{f} {new.rates[f]!r}" for f in RATE_FIELDS)
        return f"  new       {move.host}: {rates}{windows}{off}"
    moved = [f"{f} {move.old[f]!r} → {new.rates[f]!r}"
             for f in RATE_FIELDS if move.old[f] != new.rates[f]]
    if move.old.get("schedule") != new.schedule:
        moved.append(f"schedule of {len(move.old.get('schedule') or [])} → "
                     f"{len(new.schedule or [])} windows")
    return f"  changed   {move.host}: {', '.join(moved)}{off}"


def report(stamp: str, result: Result, tracked: dict) -> str:
    lines = [f"OpenRouter provider rates, detected {stamp}"]
    if not result.moves:
        lines.append("no rate moved")
    for model, source in tracked.items():
        section = [_move_text(m) for m in result.moves if m.model == model]
        section += [f"  vanished  {host} (row kept)"
                    for m, host in result.vanished if m == model]
        if section:
            lines += ["", f"{model} ({source['id']})", *section]
    if result.refusals:
        lines += ["", "refused, rows left untouched:", *(f"  {r}" for r in result.refusals)]
    if result.notices:
        lines += ["", "notices:", *(f"  {n}" for n in result.notices)]
    return "\n".join(lines)


def commit_message(result: Result, body: str) -> str:
    changed = sum(1 for m in result.moves if m.old is not None)
    added = len(result.moves) - changed
    counts = [f"{changed} changed" if changed else "",
              f"{added} new" if added else "",
              f"{len(result.vanished)} vanished" if result.vanished else "",
              f"{len(result.refusals)} refused" if result.refusals else ""]
    subject = "Refresh OpenRouter provider rates: " + ", ".join(c for c in counts if c)
    return f"{subject}\n\n{body}\n\nCaptured by .github/workflows/refresh-pricing.yml.\n"


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Append moved OpenRouter provider rates to src/pricing.json.")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be appended; write nothing")
    parser.add_argument("--commit-msg", type=Path,
                        help="write the commit message here when anything is appended")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, *, fetch: Fetch = fetch_endpoints,
         now: datetime | None = None, pricing_path: Path = PRICING_JSON,
         constants_path: Path = CONSTANTS_PY) -> int:
    args = _arguments(argv)
    stamp = detection_stamp(now or datetime.now(timezone.utc))
    try:
        result = refresh(json.loads(pricing_path.read_text(encoding="utf-8")), fetch, stamp)
        constants = constants_path.read_text(encoding="utf-8")
        if result.moves:
            constants = bump_parser_version(constants, stamp)
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
