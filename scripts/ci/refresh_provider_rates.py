#!/usr/bin/env python3
"""Refresh src/pricing.json's OpenRouter provider rates (SV-RATE-REFRESH).

Fetches every tracked model's endpoints from OpenRouter's public API and
APPENDS an entry, effective from the detection time, to each provider row
whose price moved; a host seen for the first time gets a row that begins
then. No existing entry is ever rewritten. A run that appends bumps
PARSER_VERSION in backend/constants.py by one from whatever it holds, and
moves provider_rates_fetched; a run that appends nothing writes nothing.

Only endpoints in the account's data region count (tag_region). These
refuse the host or model they concern, which appends nothing:
- a host with two endpoints in that region at different prices and no
  resolution for it (a tag, or "cheapest" of otherwise identical twins);
- a resolution that no longer applies;
- a response shape this script does not recognise;
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
_REGION = re.compile(r"[a-z]{2}(?:-[a-z0-9]+)*")
_IDENTITY = ("tag", "quantization", "context_length", "max_completion_tokens",
             "max_prompt_tokens")
# Price order for "select": "cheapest": cache read, then input, then output.
_ORDER = ("read", "fresh", "output")

Fetch = Callable[[str], object]


class RefreshError(Exception):
    """A run a human must look at: nothing is written."""


@dataclass
class Move:
    """One provider row the run appends to (old is None for a new host)."""
    model: str
    host: str
    old: dict | None
    new: dict
    discount: Decimal


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


def _endpoint(endpoint: object, where: str) -> tuple[str, "Listed"]:
    """(host, listing) of one listed endpoint.

    The listed price already has any promotional discount applied; the
    discount is kept only as the note beside it. Cache writes take the
    listed write price when it is nonzero, the input rate otherwise.
    """
    if not (isinstance(endpoint, dict) and isinstance(endpoint.get("provider_name"), str)
            and isinstance(endpoint.get("tag"), str)
            and isinstance(endpoint.get("pricing"), dict)):
        raise RefreshError(f"{where}: unrecognised endpoint shape")
    price = endpoint["pricing"]
    fresh = _per_million(price.get("prompt"), where)
    output = _per_million(price.get("completion"), where)
    read = _per_million(price["input_cache_read"], where) if "input_cache_read" in price else 0.0
    write = (_per_million(price["input_cache_write"], where)
             if "input_cache_write" in price else 0.0)
    discount = price.get("discount", 0)
    if (not isinstance(discount, (int, float)) or isinstance(discount, bool)
            or not 0 <= discount < 1):
        raise RefreshError(f"{where}: discount {discount!r} is not a fraction")
    create = write or fresh
    rates = {"fresh": fresh, "create_5m": create, "create_1h": create,
             "read": read, "output": output}
    # What tells two of one host's endpoints apart when the price does not.
    identity = json.dumps([endpoint.get(k) for k in _IDENTITY])
    return endpoint["provider_name"], (endpoint["tag"], identity, rates,
                                       Decimal(str(discount)))


def tag_region(tag: str) -> str | None:
    """The data region an endpoint tag names, or None for a global one.

    OpenRouter tags an endpoint `host` or `host/<suffix>`. The suffixes seen
    are quantizations (fp4, fp8, nvfp4) and regions (us); a region is a
    two-letter code, optionally qualified (us, eu, us-east-1), a shape no
    quantization suffix has.
    """
    _, slash, suffix = tag.rpartition("/")
    return suffix if slash and _REGION.fullmatch(suffix) else None


Listed = tuple[str, str, dict, Decimal]    # tag, identity, rates, discount
Price = tuple[dict, Decimal]                # rates, discount


def listed_rows(model: str, payload: object, region: str | None, resolutions: dict,
                stored: dict[str, dict]) -> tuple[dict[str, Price], dict[str, str]]:
    """Each host's one price for `model`, and each refused host's reason.

    A host's endpoints outside the data `region` (None: global, untagged
    by region) are not ones the account is billed by, unless the file pins
    that host to a tag. Endpoints at one price are one row; several prices
    left over need a resolution in the file, or that host is refused rather
    than guessed. `stored` is each host's current rates, which is how a
    price-order resolution sees its order flip.
    """
    by_host: dict[str, list[Listed]] = {}
    for i, endpoint in enumerate(_endpoints_of(model, payload)):
        host, listing = _endpoint(endpoint, f"{model} endpoint {i}")
        by_host.setdefault(host, []).append(listing)
    rows, refused = {}, {}
    for host, listed in by_host.items():
        try:
            price = _host_price(f"{model} via {host}", listed, region,
                                resolutions.get(host), stored.get(host))
        except RefreshError as exc:
            refused[host] = str(exc)
            continue
        if price is not None:
            rows[host] = price
    if not rows and not refused:
        raise RefreshError(f"{model}: no endpoint in the data region")
    return rows, refused


def _endpoints_of(model: str, payload: object) -> list:
    data = payload.get("data") if isinstance(payload, dict) else None
    endpoints = data.get("endpoints") if isinstance(data, dict) else None
    if not isinstance(endpoints, list):
        raise RefreshError(f"{model}: unrecognised response shape")
    if not endpoints:
        raise RefreshError(f"{model}: no endpoints listed; read as a broken fetch, "
                           "never as every host vanishing")
    return endpoints


def _host_price(where: str, listed: list[Listed], region: str | None, pin: object,
                stored: dict | None) -> Price | None:
    """A host's one price, or None when it lists nothing the account can use."""
    override = _override(where, pin)
    if override.get("tag") is not None:
        chosen = [e for e in listed if e[0] == override["tag"]]
        if not chosen:
            tags = ", ".join(sorted({e[0] for e in listed}))
            raise RefreshError(f"{where}: pinned tag {override['tag']!r} is not listed "
                               f"({tags})")
        listed = chosen
    else:
        listed = [e for e in listed if tag_region(e[0]) == region]
    prices = list({json.dumps(rates, sort_keys=True): (rates, discount)
                   for _, _, rates, discount in reversed(listed)}.values())
    if len(prices) > 1 and override.get("select") == "cheapest":
        return _cheapest(where, listed, prices, stored)
    if len(prices) > 1:
        tags = ", ".join(sorted({tag or "(untagged)" for tag, _, _, _ in listed}))
        raise RefreshError(f"{where}: {len(listed)} endpoints ({tags}) at "
                           f"{len(prices)} different prices; resolve it in "
                           "openrouter.models.<model>.resolve")
    return prices[0] if prices else None


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


def _cheapest(where: str, listed: list[Listed], prices: list[Price],
              stored: dict | None) -> Price:
    """The cheaper of endpoints that differ in nothing but price, ordered by
    cache read, then input, then output.

    The price is the only identity such twins have, so the endpoint the
    row tracks is the one still listed at the row's price. When that one is
    no longer the cheaper, the order has flipped and a human must look; an
    equal order between different prices is refused the same way.
    """
    if len({identity for _, identity, _, _ in listed}) > 1:
        raise RefreshError(f"{where}: 'cheapest' applies only to endpoints identical "
                           "in tag, quantization and limits; these differ")
    ranked = sorted(prices, key=lambda p: [p[0][f] for f in _ORDER])
    if [ranked[0][0][f] for f in _ORDER] == [ranked[1][0][f] for f in _ORDER]:
        raise RefreshError(f"{where}: a tie in price order between different prices")
    if stored is not None and any(rates == stored for rates, _ in ranked[1:]):
        raise RefreshError(f"{where}: the price order flipped: the endpoint at the "
                           "row's price is no longer the cheaper")
    return ranked[0]


def _entry(stamp: str, rates: dict, discount: Decimal) -> dict:
    entry = {"from": stamp, **rates}
    if discount:
        entry["note"] = f"{format((discount * 100).normalize(), 'f')}% off"
    return entry


def _append(model: str, hosts: dict, rows: dict[str, tuple[dict, Decimal]],
            stamp: str) -> list[Move]:
    """Append each moved price to its row, and start a row for each new
    host, in place; the moves made."""
    moves = []
    for host, (rates, discount) in rows.items():
        history = hosts.get(host)
        if history is None:
            hosts[host] = [_entry(stamp, rates, discount)]
            moves.append(Move(model, host, None, rates, discount))
            continue
        newest = history[-1]
        if all(newest[field] == rates[field] for field in RATE_FIELDS):
            continue
        history.append(_entry(stamp, rates, discount))
        moves.append(Move(model, host, {f: newest[f] for f in RATE_FIELDS},
                          rates, discount))
    return moves


def _fetch(fetch: Fetch, model: str, source: object) -> object:
    if not isinstance(source, dict) or not isinstance(source.get("id"), str):
        raise RefreshError(f"{model}: openrouter entry needs an 'id'")
    try:
        return fetch(source["id"])
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RefreshError(f"{model}: fetching {source['id']} failed: {exc}") from exc


def data_region(config: object) -> str | None:
    """openrouter.data_region: "global" (None: endpoints no region tag
    names) or the region code whose tagged endpoints the account uses."""
    region = config.get("data_region") if isinstance(config, dict) else None
    if region == "global":
        return None
    if isinstance(region, str) and _REGION.fullmatch(region):
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


def refresh(doc: dict, fetch: Fetch, stamp: str
            ) -> tuple[dict, list[Move], list[tuple[str, str]], list[str]]:
    """The file with every move appended, the moves, the vanished hosts,
    and each refusal. A refused host, or a refused model, blocks only
    itself: its rows are left untouched and every other move stands."""
    tracked, region = _sources(doc)
    new_doc = copy.deepcopy(doc)
    moves: list[Move] = []
    vanished: list[tuple[str, str]] = []
    refusals: list[str] = []
    for model, source in tracked.items():
        # Every model is fetched even after one is refused, so a red run
        # names everything a human must look at.
        hosts = new_doc["providers"].setdefault(model, {})
        try:
            rows, refused = listed_rows(
                model, _fetch(fetch, model, source), region, source.get("resolve", {}),
                {host: {f: h[-1][f] for f in RATE_FIELDS} for host, h in hosts.items()})
        except RefreshError as exc:
            refusals.append(str(exc))
            continue
        refusals += refused.values()
        moves += _append(model, hosts, rows, stamp)
        vanished += [(model, host) for host in hosts
                     if host not in rows and host not in refused]
    if moves:
        new_doc["provider_rates_fetched"] = stamp
    # The loaders' own rules, run on what would be written: among them, a
    # detection time not after a row's newest entry.
    try:
        pricing.load_tables(new_doc)
    except ValueError as exc:
        raise RefreshError(f"the refreshed file would not load: {exc}") from exc
    return new_doc, moves, vanished, refusals


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


def _rates_text(rates: dict) -> str:
    return ", ".join(f"{field} {rates[field]!r}" for field in RATE_FIELDS)


def report(stamp: str, moves: list[Move], vanished: list[tuple[str, str]],
           tracked: dict, refusals: list[str]) -> str:
    lines = [f"OpenRouter provider rates, detected {stamp}"]
    if not moves:
        lines.append("no rate moved")
    for model, source in tracked.items():
        section = []
        for move in (m for m in moves if m.model == model):
            off = f" ({_entry('', {}, move.discount)['note']})" if move.discount else ""
            if move.old is None:
                section.append(f"  new       {move.host}: {_rates_text(move.new)}{off}")
            else:
                moved = ", ".join(f"{f} {move.old[f]!r} → {move.new[f]!r}"
                                  for f in RATE_FIELDS if move.old[f] != move.new[f])
                section.append(f"  changed   {move.host}: {moved}{off}")
        section += [f"  vanished  {host} (row kept)" for m, host in vanished if m == model]
        if section:
            lines += ["", f"{model} ({source['id']})", *section]
    if refusals:
        lines += ["", "refused, rows left untouched:", *(f"  {r}" for r in refusals)]
    return "\n".join(lines)


def commit_message(moves: list[Move], vanished: list[tuple[str, str]],
                   refusals: list[str], body: str) -> str:
    changed = sum(1 for m in moves if m.old is not None)
    counts = [f"{changed} changed" if changed else "",
              f"{len(moves) - changed} new" if len(moves) > changed else "",
              f"{len(vanished)} vanished" if vanished else "",
              f"{len(refusals)} refused" if refusals else ""]
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
        new_doc, moves, vanished, refusals = refresh(
            json.loads(pricing_path.read_text(encoding="utf-8")), fetch, stamp)
        constants = constants_path.read_text(encoding="utf-8")
        if moves:
            constants = bump_parser_version(constants, stamp)
    except RefreshError as exc:
        print(f"refresh_provider_rates: {exc}", file=sys.stderr)
        return 1
    body = report(stamp, moves, vanished, new_doc["openrouter"]["models"], refusals)
    print(body)
    if moves and not args.dry_run:
        pricing_path.write_text(json.dumps(new_doc, indent=2, sort_keys=True) + "\n",
                                encoding="utf-8")
        constants_path.write_text(constants, encoding="utf-8")
        if args.commit_msg:
            args.commit_msg.write_text(commit_message(moves, vanished, refusals, body),
                                       encoding="utf-8")
    # Every other move is written; the run is still red, so a human sees it.
    if refusals:
        print("\n".join(f"refresh_provider_rates: {r}" for r in refusals), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
