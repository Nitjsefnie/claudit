#!/usr/bin/env python3
"""Endpoint selection for the OpenRouter provider-rate refresh (SV-RATE-REFRESH).

One host's listed endpoints become its one row: the data-region filter, the
per-host resolution recorded in openrouter.models.<model>.resolve, and the
automatic rules of refresh_selection_rules.py. These refuse the host they
concern, which appends nothing: a stale pin; twins that differ beyond a
recorded ignore; a tied or flipped price order; a resolution keyed on a
price; a multi-price shape no rule fits. The hourly append machinery lives
in refresh_provider_rates.py.

    A resolution: {"tag": ...} takes that tag's endpoints, whatever their
    region; {"select": "cheapest"} takes the cheaper of endpoints identical
    in tag, quantization and limits, comparing cache read, then input, then
    output; the two combine, the tag narrowing first. "ignore": [fields]
    refines a 'cheapest': the named identity fields are a recorded human
    decision that the endpoints are one offering listed with a listing
    artifact, so 'cheapest' compares the rest. With no resolution at all the
    four rules of refresh_selection_rules.py resolve the mechanical
    multi-price shapes and the run's report records each as rule-resolved.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# pylint: disable=wrong-import-position
from refresh_prices import (PRICED, RefreshError, Untracked,  # noqa: E402
                            as_listed, covers_week, entry_schedule,
                            in_a_window, is_zero, rates_of, unknown_suffixes)
from refresh_selection_rules import (bare_namespace, price_groups,  # noqa: E402
                                     price_order, same_quantization,
                                     select_region, unique_quantization)

# What tells two of one host's endpoints apart when the price does not.
_IDENTITY = ("tag", "quantization", "context_length", "max_completion_tokens",
             "max_prompt_tokens")
# The identity fields a recorded ignore may name; never the tag, which a
# tag pin already narrows and whose mixing would pool different offerings.
_IGNORABLE = _IDENTITY[1:]


@dataclass(frozen=True)
class Listing:
    """One listed endpoint, normalised to the table's shape."""
    tag: str
    identity: dict
    rates: dict
    schedule: list | None
    discount: Decimal

    @property
    def price(self) -> str:
        return json.dumps([self.rates, self.schedule], sort_keys=True)


def _listing(endpoint: object, where: str, at: datetime,
             kept: dict | None) -> Listing:
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
        if (key not in (*PRICED, "discount", "overrides")
                and not is_zero(value)):
            raise RefreshError(f"{where}: pricing {key} {value!r} is not modelled")
    discount = price.get("discount", 0)
    if (not isinstance(discount, (int, float)) or isinstance(discount, bool)
            or not 0 <= discount < 1):
        raise RefreshError(f"{where}: discount {discount!r} is not a fraction")
    identity = {k: endpoint.get(k) for k in _IDENTITY}
    rates, schedule = rates_of(price, where), entry_schedule(price, where)
    if schedule and in_a_window(schedule, at):
        if kept is None and not covers_week(schedule):
            raise RefreshError(
                f"{where}: first seen inside one of its windows: the listed "
                "top-level price is the active window's, not a default; the "
                "next fetch outside every window starts the row")
        if kept is not None:
            # The top-level token price inside an active schedule is that
            # window's, while web_search remains a separate listing rate.
            search = rates.get("web_search")
            rates = dict(kept)
            if search:
                rates["web_search"] = search
            schedule = entry_schedule(
                {**as_listed(kept), "overrides": price["overrides"]},
                where)
    return Listing(endpoint["tag"], identity, rates, schedule,
                   Decimal(str(discount)))


def listed_rows(model: str, payload: object, region: str | None, resolutions: dict,
                stored: dict[str, dict], at: datetime
                ) -> tuple[dict[str, Listing], dict[str, str], list[str],
                           dict[str, str]]:
    """Each host's one listing for `model`, each refused host's reason, the
    notices for a human that refuse nothing, and each untracked host's
    notice (a listed shape the refresh deliberately leaves untracked — its
    row untouched, the run stays green).

    Endpoints are grouped by host before any is read, so a malformed one
    refuses its own host only. The data-region filter admits endpoints with
    no recognized region suffix and endpoints naming the configured region
    (`None` represents `global`); other regions are filtered unless the file
    pins that host to a tag. Endpoints at one price are one row; several
    prices left over need a resolution in the file, or that host is refused
    rather than guessed. `stored` is each host's current rates, which is how
    a price-order resolution sees its order, and the default a scheduled
    host fetched inside a window keeps; `at` is the fetch instant.
    """
    rows, refused, notices, untracked = {}, {}, [], {}
    for host, endpoints in _by_host(model, payload).items():
        try:
            chosen, host_notices = _host_row(
                f"{model} via {host}", endpoints, region,
                resolutions.get(host), stored.get(host), at)
        except Untracked as exc:
            untracked[host] = f"{exc}; the host was left untouched"
            continue
        except RefreshError as exc:
            refused[host] = str(exc)
            continue
        if chosen is not None:
            rows[host] = chosen
        notices += host_notices
    if not rows and not refused and not untracked:
        raise RefreshError(f"{model}: no endpoint in the data region")
    return rows, refused, notices, untracked


def _by_host(model: str, payload: object) -> dict[str, list[tuple[int, object]]]:
    """The payload's endpoints (with their index), grouped by host."""
    by_host: dict[str, list[tuple[int, object]]] = {}
    for i, endpoint in enumerate(_endpoints_of(model, payload)):
        host = endpoint.get("provider_name") if isinstance(endpoint, dict) else None
        if not isinstance(host, str):
            raise RefreshError(f"{model} endpoint {i}: names no provider")
        by_host.setdefault(host, []).append((i, endpoint))
    return by_host


def _host_row(where: str, endpoints: list[tuple[int, object]],
              region: str | None,
              pin: object, stored: dict | None,
              at: datetime) -> tuple[Listing | None, list[str]]:
    """One host's listing, and its notices; RefreshError refuses the host."""
    listed = [_listing(e, f"{where} (endpoint {i})", at, stored)
              for i, e in endpoints]
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
    use), and the notice a rule-resolved choice or a twin switch earns."""
    override = _override(where, pin)
    chosen = notice = None
    if override.get("tag") is not None:
        listed = _pinned_listings(where, listed, override["tag"])
    else:
        selected = select_region(where, listed, region, pin)
        listed, chosen, notice = selected.listings, selected.chosen, selected.notice
    return _finish_host_price(where, listed, override, pin, stored, chosen, notice)


def _finish_host_price(
        where: str, listed: list[Listing], override: dict, pin: object,
        stored: dict | None, chosen: Listing | None,
        notice: str | None) -> tuple[Listing | None, str | None]:
    """Resolve remaining prices after explicit tag and maintained region policy."""
    groups = price_groups(listed)
    prices = [group[0] for group in groups]
    if len(prices) > 1 and override.get("select") == "cheapest":
        return _cheapest(where, prices, stored, override.get("ignore") or [])
    if notice:
        return chosen, notice
    if len(prices) > 1 and pin is None:
        automatic = _automatic_price_rule(where, groups, stored)
        if automatic is not None:
            return automatic
    if len(prices) > 1:
        tags = ", ".join(sorted({e.tag or "(untagged)" for e in listed}))
        raise RefreshError(f"{where}: {len(listed)} endpoints ({tags}) at "
                           f"{len(prices)} different prices; resolve it in "
                           "openrouter.models.<model>.resolve")
    return (prices[0] if prices else None), None


def _automatic_price_rule(
        where: str, groups: list[list[Listing]], stored: dict | None
        ) -> tuple[Listing, str | None] | None:
    """Apply the target's mechanical rules in their maintained order."""
    for rule in (bare_namespace, unique_quantization):
        resolved = rule(where, groups)
        if resolved is not None:
            return resolved
    return same_quantization(where, groups, stored)


def _pinned_listings(where: str, listed: list[Listing], tag: str) -> list[Listing]:
    """The endpoints under an explicitly pinned tag, or the refusal naming
    the tags the host does list."""
    chosen = [endpoint for endpoint in listed if endpoint.tag == tag]
    if not chosen:
        tags = ", ".join(sorted({endpoint.tag for endpoint in listed}))
        raise RefreshError(f"{where}: pinned tag {tag!r} is not listed ({tags})")
    return chosen


def _override(where: str, pin: object) -> dict:
    """A per-host resolution: {"tag": ...} takes that endpoint whatever its
    region; {"select": "cheapest"} takes the cheaper of otherwise identical
    endpoints; the two combine, the tag narrowing first, whatever the
    region. "ignore": [fields] refines a 'cheapest': the named identity
    fields are a recorded human decision that the endpoints are one offering
    listed with a listing artifact, so 'cheapest' compares the rest. Any key
    carries a "why". Never a price: a pin on a price stops matching the
    moment that price moves."""
    if pin is None:
        return {}
    refuse = (f"{where}: a resolution is keyed on 'tag', 'select': 'cheapest', "
              f"or a 'cheapest' with a recorded 'ignore' (with a 'why'), "
              f"never a price: {pin!r}")
    if not isinstance(pin, dict):
        raise RefreshError(refuse)
    keys = set(pin) - {"why"}
    if not keys or not keys <= {"tag", "select", "ignore"} or not _pin_ok(pin, keys):
        raise RefreshError(refuse)
    return pin


def _pin_ok(pin: dict, keys: set[str]) -> bool:
    """The pin's grammar: tag a string; select exactly 'cheapest'; ignore a
    non-empty list of non-tag identity fields, refining a 'cheapest'."""
    if "tag" in keys and not isinstance(pin.get("tag"), str):
        return False
    if "select" in keys and pin.get("select") != "cheapest":
        return False
    if "ignore" in keys:
        ignore = pin.get("ignore")
        return (pin.get("select") == "cheapest"
                and isinstance(ignore, list) and bool(ignore)
                and all(isinstance(field, str) for field in ignore)
                and set(ignore) <= set(_IGNORABLE))
    return True


def _cheapest(where: str, prices: list[Listing],
              stored: dict | None, ignore: list[str]) -> tuple[Listing, str | None]:
    """The cheaper of endpoints that differ in nothing but price.

    The price is the only identity such twins have, so the endpoint the
    row tracks is the one still listed at the row's price; the price-order
    rules that guard it (a tie and a flip refuse, a rise is reported) live
    in refresh_selection_rules.price_order, which the automatic
    same-quantization rule shares.

    An ignore narrows what identity compares: only a recorded human decision
    may declare differing fields a listing artifact, and the comparison
    keeps every field it was not given.
    """
    keys = [key for key in _IDENTITY if key not in ignore]
    if len({tuple(e.identity.get(key) for key in keys) for e in prices}) > 1:
        raise RefreshError(f"{where}: 'cheapest' applies only to endpoints identical "
                           "in tag, quantization and limits; these differ")
    return price_order(where, prices, stored)
