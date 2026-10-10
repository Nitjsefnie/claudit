#!/usr/bin/env python3
"""Resolution-free endpoint selection for one host's listed prices (#877).

Beneath an explicit resolve entry — which always keeps precedence — a
host's several listed prices resolve mechanically when they are variants
of one offering rather than several offerings:

    - a bare namespace endpoint beside its service tiers takes the bare one;
    - the one quantization endpoint beside throughput tiers takes that one;
    - the configured data region takes its own endpoint: the region filter,
      and under `global` the namespace's global endpoint (an explicit
      `<ns>/global`, else the bare `<ns>`) over an unrecognised regional
      spelling;
    - one quantization's price twins take the cheaper, ordered by cache
      read, then input, then output, with the twin-switch report the
      recorded `cheapest` pins use.

Everything else refuses, naming the tags: a quantization beside a bare
namespace, two quantizations, two prices under one tag, a region spelling
with no global endpoint to prefer. The run reports each rule that fired as
rule-resolved, so an automatic choice is never silent.

A region spelling is a tag with one suffix the vocabulary does not carry —
neither a recognized data region, nor a quantization, nor a service tier
(`azure/swedencentral`). Nothing is read as a region unless the namespace
also lists a global endpoint to prefer over it, so an offering the
refresh does not recognise never silently disappears from a row.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from refresh_prices import (QUANTIZATIONS, REGION_RE, RefreshError, tag_region)

if TYPE_CHECKING:
    from refresh_selection import Listing

# Throughput/service tiers: a distinct offering under its own tag, never a
# price twin of the endpoint beside it. `zdr` (zero data retention) is
# Mistral's same-price service variant, listed beside its own quantization.
SERVICE_TIERS = frozenset({"fast", "highspeed", "flex", "ultrafast", "zdr"})
# Price order for the cheapest rule: cache read, then input, then output.
_ORDER = ("read", "fresh", "output")


@dataclass(frozen=True)
class RegionSelection:
    """A host's listings after the data-region decision."""
    listings: list[Listing]
    chosen: Listing | None
    notice: str | None


def namespace_of(tag: str) -> str:
    """A tag's namespace: everything before its first suffix."""
    return tag.partition("/")[0].lower()


def tag_suffixes(tag: str) -> list[str]:
    """A tag's suffixes, lowercased."""
    return [suffix.lower() for suffix in tag.partition("/")[2].split("/") if suffix]


def price_groups(listed: list[Listing]) -> list[list[Listing]]:
    """The listings grouped by exact listed price, in listed order: one
    group per price, its members interchangeable tags at that price."""
    grouped: dict[str, list[Listing]] = {}
    for endpoint in listed:
        grouped.setdefault(endpoint.price, []).append(endpoint)
    return list(grouped.values())


def select_region(where: str, listed: list[Listing], region: str | None,
                  pin: object) -> RegionSelection:
    """The configured region's listings, and the rule-resolved choice when
    the region alone leaves one price.

    The filter drops every endpoint naming another data region. Under the
    global region an endpoint naming no region survives it, so the
    namespace's global endpoint is preferred over its unrecognised regional
    spellings here; a named region needs no such preference, its own tag
    being the one the filter keeps. An explicit resolution suppresses both,
    and decides for itself."""
    kept = [endpoint for endpoint in listed if tag_region(endpoint.tag) == region]
    if pin is None:
        if region is None:
            preferred = _global_choice(where, kept, listed)
            if preferred is not None:
                return preferred
        if len(kept) < len(listed) and len(price_groups(kept)) == 1:
            return _filtered_choice(where, kept, listed, region)
    return RegionSelection(kept, None, None)


def _global_choice(where: str, kept: list[Listing],
                   listed: list[Listing]) -> RegionSelection | None:
    """The namespace's global endpoint over its regional spellings, or None
    when there is no such pair of prices to decide."""
    groups = price_groups(kept)
    if len(groups) < 2:
        return None
    chosen, namespace = _global_endpoint(kept)
    if chosen is None:
        return None
    chosen_group = next(group for group in groups if chosen in group)
    others = [group for group in groups if group is not chosen_group]
    if not all(_is_global_alias(endpoint.tag, namespace)
               for group in others for endpoint in group):
        return None
    over = sorted({endpoint.tag for group in others for endpoint in group})
    detail = (f"took the global endpoint {chosen.tag!r} over "
              f"{', '.join(repr(tag) for tag in over)}")
    return RegionSelection(kept, chosen, rule_notice(
        where, "data region", detail, chosen_group))


def _global_endpoint(kept: list[Listing]) -> tuple[Listing | None, str]:
    """The one endpoint naming its namespace's global offering: an explicit
    `<ns>/global` when the namespace lists one, else the bare `<ns>`."""
    explicit = [endpoint for endpoint in kept if "global" in tag_suffixes(endpoint.tag)]
    if len(explicit) == 1:
        return explicit[0], namespace_of(explicit[0].tag)
    bare = [endpoint for endpoint in kept if not tag_suffixes(endpoint.tag)
            and "/" not in endpoint.tag]
    if len(bare) == 1:
        return bare[0], namespace_of(bare[0].tag)
    return None, ""


def _is_global_alias(tag: str, namespace: str) -> bool:
    """A tag naming the same offering without naming another offering: the
    bare namespace, or one suffix the vocabulary does not carry."""
    if namespace_of(tag) != namespace:
        return False
    suffixes = tag_suffixes(tag)
    return not suffixes or (len(suffixes) == 1 and _unknown(suffixes[0]))


def _unknown(suffix: str) -> bool:
    """A suffix naming neither a data region, a quantization, nor a service
    tier: the shape a region spelled outside the vocabulary takes."""
    return (not REGION_RE.fullmatch(suffix) and suffix not in QUANTIZATIONS
            and suffix not in SERVICE_TIERS)


def _filtered_choice(where: str, kept: list[Listing], listed: list[Listing],
                     region: str | None) -> RegionSelection:
    """The one price the data-region filter left, reported."""
    chosen_group = price_groups(kept)[0]
    chosen = _region_endpoint(chosen_group, region)
    excluded = sorted({endpoint.tag for endpoint in listed
                       if tag_region(endpoint.tag) != region})
    detail = f"the {_region_name(region)} data region leaves {chosen.tag!r}"
    if excluded:
        detail += f"; dropped {', '.join(repr(tag) for tag in excluded)}"
    return RegionSelection(kept, chosen, rule_notice(
        where, "data region", detail, chosen_group))


def _region_endpoint(group: list[Listing], region: str | None) -> Listing:
    """The endpoint of a chosen group that names the configured region: its
    explicit tag when the group holds one, else the first."""
    wanted = _region_name(region)
    return next((endpoint for endpoint in group
                 if wanted in tag_suffixes(endpoint.tag)), group[0])


def _region_name(region: str | None) -> str:
    return "global" if region is None else region


def bare_namespace(where: str, groups: list[list[Listing]]
                   ) -> tuple[Listing, str] | None:
    """Take the bare namespace endpoint beside only its service tiers.

    The bare tag is the namespace's list price; a tier is a distinct
    offering, not a price twin, so the shape needs no human decision."""
    bare = [endpoint for group in groups for endpoint in group
            if endpoint.tag and "/" not in endpoint.tag]
    if len(bare) != 1 or len(groups) < 2:
        return None
    base, namespace = bare[0], namespace_of(bare[0].tag)
    base_group = next(group for group in groups if base in group)
    if not all(endpoint.tag == base.tag or _is_tier_tag(endpoint.tag, namespace)
               for endpoint in base_group):
        return None
    tiers = [group for group in groups if group is not base_group]
    if not all(_is_tier_group(group, namespace) for group in tiers):
        return None
    named = sorted({endpoint.tag for group in tiers for endpoint in group})
    detail = (f"took the bare namespace endpoint {base.tag!r} over service "
              f"tiers {', '.join(repr(tag) for tag in named)}")
    return base, rule_notice(where, "bare namespace", detail, base_group)


def unique_quantization(where: str, groups: list[list[Listing]]
                        ) -> tuple[Listing, str] | None:
    """Take the one quantization endpoint beside only service tiers.

    A quantization is the endpoint the model's list price is quoted at;
    the throughput tiers beside it are distinct offerings. A tier listed at
    the quantization's own price rides in that group (one listing, two
    tags), so the group's members are the quantization's tag or one of the
    namespace's tiers, and any other offering refuses."""
    quantized = [(group, _group_quantizations(group)) for group in groups
                 if _group_quantizations(group)]
    if len(quantized) != 1 or len(groups) < 2:
        return None
    group, quantizations = quantized[0]
    if len(quantizations) != 1:
        return None
    quantization = next(iter(quantizations))
    namespace = namespace_of(_quantization_endpoint(group, quantization).tag)
    if not _is_quantization_group(group, namespace, quantization):
        return None
    tiers = [candidate for candidate in groups if candidate is not group]
    if not all(_is_tier_group(candidate, namespace) for candidate in tiers):
        return None
    chosen = _quantization_endpoint(group, quantization)
    detail = (f"took the {quantization} endpoint {chosen.tag!r} over service "
              f"tiers")
    return chosen, rule_notice(where, "unique quantization", detail, group)


def same_quantization(where: str, groups: list[list[Listing]], stored: dict | None
                      ) -> tuple[Listing, str] | None:
    """Take the cheapest of one quantization's price twins.

    Endpoints listed under one quantization at several prices are one
    offering priced per region, so the cheapest is the one the account
    reaches; the price-order rules that guard the recorded `cheapest` pins
    guard this too (a tie refuses, a flip refuses, a rise is reported). The
    premise is taken, not checked: the rule compares prices, never the
    identity fields a recorded `cheapest` pin compares, so a host whose
    same-quantization endpoints differ in a limit is resolved here rather
    than held for a human — that is the pin's job, and such a host keeps
    its pin. A service tier at one of those prices rides in its group and
    is interchangeable with the quantization tag beside it, while any other
    offering — a second quantization, a region spelling — keeps refusing."""
    candidates: list[list[Listing]] = []
    tiers: list[list[Listing]] = []
    quantizations: set[str] = set()
    for group in groups:
        group_quantizations = _group_quantizations(group)
        if not group_quantizations:
            tiers.append(group)
            continue
        if len(group_quantizations) != 1:
            return None
        quantizations |= group_quantizations
        candidates.append(group)
    if len(quantizations) != 1 or len(candidates) < 2:
        return None
    quantization = next(iter(quantizations))
    namespace = namespace_of(_quantization_endpoint(candidates[0], quantization).tag)
    if not all(_is_quantization_group(group, namespace, quantization)
               for group in candidates):
        return None
    if not all(_is_tier_group(group, namespace) for group in tiers):
        return None
    chosen, twin_notice = price_order(
        where, [_quantization_endpoint(group, quantization) for group in candidates],
        stored)
    chosen_group = next(group for group in candidates if chosen in group)
    detail = (f"took the cheapest {quantization} endpoint {chosen.tag!r}, by "
              "cache read, then input, then output")
    rule = rule_notice(where, "same quantization", detail, chosen_group)
    return chosen, "; ".join(part for part in (rule, twin_notice) if part)


def price_order(where: str, prices: list[Listing],
                stored: dict | None) -> tuple[Listing, str | None]:
    """The cheaper of several prices, ordered by cache read, then input,
    then output, with the twin-switch report a rise earns.

    The price is the only identity such twins have, so a tie in the order
    and a flip away from the row's own price both refuse: a human must look
    at which endpoint the account reaches. A rise is reported, not refused:
    the tracked twin moving above the other is indistinguishable from the
    other becoming the row's price, and always shows as the row's price
    rising (unless the other fell at the same time)."""
    ranked = sorted(prices, key=lambda endpoint: [endpoint.rates[f] for f in _ORDER])
    chosen, other = ranked[0], ranked[1]
    if [chosen.rates[f] for f in _ORDER] == [other.rates[f] for f in _ORDER]:
        raise RefreshError(f"{where}: a tie in price order between different prices")
    if stored is not None and any(endpoint.rates == stored for endpoint in ranked[1:]):
        raise RefreshError(f"{where}: the price order flipped: the endpoint at the "
                           "row's price is no longer the cheaper")
    notice = None
    if stored is not None and [chosen.rates[f] for f in _ORDER] > [stored[f] for f in _ORDER]:
        moved = ", ".join(f"{field} {stored[field]!r} → {chosen.rates[field]!r}"
                          for field in _ORDER if chosen.rates[field] != stored[field])
        notice = (f"possible twin switch: {where}: the price rose ({moved}); the other "
                  f"twin is at {', '.join(f'{field} {other.rates[field]!r}' for field in _ORDER)}. "
                  "Check which endpoint the row tracks")
    return chosen, notice


def rule_notice(where: str, rule: str, detail: str, group: list[Listing]) -> str:
    """One rule-resolved report line, naming the tags an equal price made
    interchangeable."""
    tags = sorted({endpoint.tag for endpoint in group})
    interchangeable = (
        f"; equal-price tags {', '.join(repr(tag) for tag in tags)} are "
        "interchangeable" if len(tags) > 1 else "")
    return f"{where}: rule-resolved ({rule}): {detail}{interchangeable}"


def _tag_quantizations(tag: str) -> set[str]:
    return {suffix for suffix in tag_suffixes(tag) if suffix in QUANTIZATIONS}


def _group_quantizations(group: list[Listing]) -> set[str]:
    return {quantization for endpoint in group
            for quantization in _tag_quantizations(endpoint.tag)}


def _is_quantization_tag(tag: str, namespace: str, quantization: str) -> bool:
    """A tag naming the namespace's one quantization and nothing unknown."""
    return (namespace_of(tag) == namespace
            and _tag_quantizations(tag) == {quantization}
            and all(not _unknown(suffix) for suffix in tag_suffixes(tag)))


def _is_quantization_group(group: list[Listing], namespace: str,
                           quantization: str) -> bool:
    """A price group of the namespace's one quantization: its endpoints are
    that quantization's tag or one of the namespace's service tiers, so a
    same-price tier rides along and any other offering refuses."""
    return all(_is_quantization_tag(endpoint.tag, namespace, quantization)
               or _is_tier_tag(endpoint.tag, namespace) for endpoint in group)


def _quantization_endpoint(group: list[Listing], quantization: str) -> Listing:
    """The endpoint of a group whose tag carries the quantization."""
    return next(endpoint for endpoint in group
                if quantization in _tag_quantizations(endpoint.tag))


def _is_tier_group(group: list[Listing], namespace: str) -> bool:
    return bool(group) and all(_is_tier_tag(endpoint.tag, namespace)
                               for endpoint in group)


def _is_tier_tag(tag: str, namespace: str) -> bool:
    """A tag naming one of the namespace's throughput tiers: at least one
    suffix is a tier, and every suffix is one the vocabulary carries."""
    suffixes = tag_suffixes(tag)
    return (namespace_of(tag) == namespace and bool(suffixes)
            and any(suffix in SERVICE_TIERS for suffix in suffixes)
            and all(suffix in SERVICE_TIERS or suffix == "global"
                    or REGION_RE.fullmatch(suffix) for suffix in suffixes))
