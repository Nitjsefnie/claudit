#!/usr/bin/env python3
"""Refresh src/pricing.json's OpenRouter provider rates (SV-RATE-REFRESH).

Fetches every tracked model's endpoints from OpenRouter's public API and
APPENDS an entry, effective from the detection time, to each provider row
whose price moved; a host seen for the first time gets a row that begins
then. No existing entry is ever rewritten. A run that appends bumps
PARSER_VERSION in backend/constants.py by one from whatever it holds, and
moves provider_rates_fetched; a run that appends nothing writes nothing.

Exits nonzero, writing nothing, on anything a human must judge: a host
listing one model at two prices with no pinned resolution, a pin no listed
price matches, a response shape this script does not recognise, a tracked
model with no endpoints, or a detection time not after a row's newest entry.

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


def _endpoint(endpoint: object, where: str) -> tuple[str, dict, Decimal]:
    """(host, rates, discount) of one listed endpoint.

    The listed price already has any promotional discount applied; the
    discount is kept only as the note beside it. Cache writes take the
    listed write price when it is nonzero, the input rate otherwise.
    """
    if not (isinstance(endpoint, dict) and isinstance(endpoint.get("provider_name"), str)
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
    return endpoint["provider_name"], rates, Decimal(str(discount))


def listed_rows(model: str, payload: object,
                resolutions: dict) -> dict[str, tuple[dict, Decimal]]:
    """Each host's one price for `model`, from its endpoints payload.

    Endpoints of one host at one price are one row. A host at several
    prices needs a pinned resolution in the file (match: the rates that
    name the endpoint to take); without one, or when nothing matches it,
    the run is refused rather than guessed.
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    endpoints = data.get("endpoints") if isinstance(data, dict) else None
    if not isinstance(endpoints, list):
        raise RefreshError(f"{model}: unrecognised response shape")
    if not endpoints:
        raise RefreshError(f"{model}: no endpoints listed; read as a broken fetch, "
                           "never as every host vanishing")
    prices: dict[str, list[tuple[dict, Decimal]]] = {}
    for i, endpoint in enumerate(endpoints):
        host, rates, discount = _endpoint(endpoint, f"{model} endpoint {i}")
        listed = prices.setdefault(host, [])
        if all(rates != seen for seen, _ in listed):
            listed.append((rates, discount))
    return {host: _choose(f"{model} via {host}", listed, resolutions.get(host))
            for host, listed in prices.items()}


def _choose(where: str, listed: list[tuple[dict, Decimal]],
            pin: object) -> tuple[dict, Decimal]:
    """A host's one price: its only one, or the one its pin names."""
    if pin is None:
        if len(listed) > 1:
            raise RefreshError(f"{where}: listed at {len(listed)} different prices "
                               "and the file pins no resolution")
        return listed[0]
    match = pin.get("match") if isinstance(pin, dict) else None
    if not isinstance(match, dict) or not set(match) <= set(RATE_FIELDS):
        raise RefreshError(f"{where}: resolution needs a 'match' of rate fields")
    chosen = [row for row in listed
              if all(row[0][field] == value for field, value in match.items())]
    if len(chosen) != 1:
        raise RefreshError(f"{where}: the pinned resolution {match} matches "
                           f"{len(chosen)} of the {len(listed)} listed prices")
    return chosen[0]


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


def refresh(doc: dict, fetch: Fetch,
            stamp: str) -> tuple[dict, list[Move], list[tuple[str, str]]]:
    """The file with every move appended, the moves, and vanished hosts."""
    tracked = doc.get("openrouter")
    if not isinstance(tracked, dict) or not set(doc["providers"]) <= set(tracked):
        raise RefreshError("every provider-table model needs an openrouter id")
    new_doc = copy.deepcopy(doc)
    moves: list[Move] = []
    vanished: list[tuple[str, str]] = []
    refusals: list[str] = []
    for model, source in tracked.items():
        # Every model is fetched even after one is refused, so a red run
        # names everything a human must look at.
        try:
            rows = listed_rows(model, _fetch(fetch, model, source),
                               source.get("resolve", {}))
        except RefreshError as exc:
            refusals.append(str(exc))
            continue
        hosts = new_doc["providers"].setdefault(model, {})
        moves += _append(model, hosts, rows, stamp)
        vanished += [(model, host) for host in hosts if host not in rows]
    if refusals:
        raise RefreshError("\n".join(refusals))
    if moves:
        new_doc["provider_rates_fetched"] = stamp
    # The loaders' own rules, run on what would be written: among them, a
    # detection time not after a row's newest entry.
    try:
        pricing.load_tables(new_doc)
    except ValueError as exc:
        raise RefreshError(f"the refreshed file would not load: {exc}") from exc
    return new_doc, moves, vanished


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
           tracked: dict) -> str:
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
    return "\n".join(lines)


def commit_message(moves: list[Move], vanished: list[tuple[str, str]], body: str) -> str:
    changed = sum(1 for m in moves if m.old is not None)
    counts = [f"{changed} changed" if changed else "",
              f"{len(moves) - changed} new" if len(moves) > changed else "",
              f"{len(vanished)} vanished" if vanished else ""]
    subject = "Refresh OpenRouter provider rates: " + ", ".join(c for c in counts if c)
    return f"{subject}\n\n{body}\n\nCaptured by .github/workflows/refresh-pricing.yml.\n"


def main(argv: list[str] | None = None, *, fetch: Fetch = fetch_endpoints,
         now: datetime | None = None, pricing_path: Path = PRICING_JSON,
         constants_path: Path = CONSTANTS_PY) -> int:
    parser = argparse.ArgumentParser(
        description="Append moved OpenRouter provider rates to src/pricing.json.")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be appended; write nothing")
    parser.add_argument("--commit-msg", type=Path,
                        help="write the commit message here when anything is appended")
    args = parser.parse_args(argv)
    stamp = detection_stamp(now or datetime.now(timezone.utc))
    try:
        doc = json.loads(pricing_path.read_text(encoding="utf-8"))
        new_doc, moves, vanished = refresh(doc, fetch, stamp)
        constants = constants_path.read_text(encoding="utf-8")
        if moves:
            constants = bump_parser_version(constants, stamp)
    except RefreshError as exc:
        print(f"refresh_provider_rates: {exc}", file=sys.stderr)
        return 1
    body = report(stamp, moves, vanished, new_doc["openrouter"])
    print(body)
    if moves and not args.dry_run:
        pricing_path.write_text(json.dumps(new_doc, indent=2, sort_keys=True) + "\n",
                                encoding="utf-8")
        constants_path.write_text(constants, encoding="utf-8")
        if args.commit_msg:
            args.commit_msg.write_text(commit_message(moves, vanished, body),
                                       encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
