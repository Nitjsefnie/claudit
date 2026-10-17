#!/usr/bin/env python3
"""Resolution-free endpoint selection for one host's listed prices.

Explicit resolve entries keep precedence. Otherwise multiple prices resolve
by offering shape or continuity with stored token rates:

    - a bare namespace endpoint beside its service tiers takes the bare one;
    - the one quantization endpoint beside throughput tiers takes that one;
    - a base tag beside only its suffix extensions takes the base for any
      suffixes, if quantization and all non-tag identity fields agree;
    - the data-region filter takes its region; under `global`, prefer
      `<ns>/global`, else bare `<ns>`, over unrecognised regional spellings;
    - one quantization's twins take the cheaper by cache read, input, then
      output, with the recorded `cheapest` pins' twin-switch report. Only
      quantization/region suffixes belong to this family, never nested tiers;
    - last, take the sole group matching the stored five token rates at ten
      decimals, ignoring search fees/schedules. Requires a stored row.

Otherwise refuse, naming tags: unrelated offerings without a unique stored
match, including first-seen multi-price hosts with no structural fit.
Identity mismatches disqualify tier-base; twin ties/flips refuse before
continuity. Every automatic choice reports its rule as rule-resolved.

An unrecognised region spelling has one suffix naming neither a known
region, quantization nor tier (`azure/swedencentral`). Only a namespace
with a global endpoint can treat it as regional: unknown offerings must
not silently disappear.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from refresh_prices import (QUANTIZATIONS, REGION_RE, RefreshError, tag_region)
from backend.pricing_document import RATE_FIELDS, effective_rates

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
    """Group exact listed prices in listed order; equal-price tags interchange."""
    grouped: dict[str, list[Listing]] = {}
    for endpoint in listed:
        grouped.setdefault(endpoint.price, []).append(endpoint)
    return list(grouped.values())


def select_region(where: str, listed: list[Listing], region: str | None,
                  pin: object) -> RegionSelection:
    """Filter to the account's region, reporting a choice if it leaves one price.

    Regionless tags survive the global filter, so prefer the namespace's
    global endpoint over unrecognised regional spellings. Named regions
    need only filtering. Explicit resolutions suppress automatic choices."""
    kept = [endpoint for endpoint in listed if _account_region(endpoint.tag) == region]
    if pin is None:
        if region is None:
            preferred = _global_choice(where, kept, listed)
            if preferred is not None:
                return preferred
        if len(kept) < len(listed) and len(price_groups(kept)) == 1:
            return _filtered_choice(where, kept, listed, region)
    return RegionSelection(kept, None, None)


def _account_region(tag: str) -> str | None:
    """Map the recognized global marker to the account's geographic sentinel."""
    marker = tag_region(tag)
    return None if marker == "global" else marker


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
                       if _account_region(endpoint.tag) != region})
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


def tier_variant_base(where: str, groups: list[list[Listing]]
                      ) -> tuple[Listing, str] | None:
    """Take a base tag over arbitrary suffix extensions of the same identity.

    Every endpoint at another price must extend the base's exact tag;
    quantization suffixes and all non-tag identity fields must agree.
    Equal-price aliases in the base group remain interchangeable."""
    if len(groups) < 2:
        return None
    listed = [endpoint for group in groups for endpoint in group]
    for group in groups:
        others = [endpoint for candidate in groups if candidate is not group
                  for endpoint in candidate]
        for base in group:
            if not base.tag or not all(endpoint.tag.startswith(base.tag + "/")
                                       for endpoint in others):
                continue
            if not all(_tag_quantizations(endpoint.tag) == _tag_quantizations(base.tag)
                       and all(endpoint.identity.get(key) == value
                               for key, value in base.identity.items() if key != "tag")
                       for endpoint in listed):
                continue
            over = sorted({endpoint.tag for endpoint in others})
            detail = (f"took the base endpoint {base.tag!r} over tier variants "
                      f"{', '.join(repr(tag) for tag in over)}")
            return base, rule_notice(where, "tier variant base", detail, group)
    return None


def same_quantization(where: str, groups: list[list[Listing]], stored: dict | None
                      ) -> tuple[Listing, str] | None:
    """Assume one offering priced per region; take the account's cheapest twin.

    As with `cheapest` pins, ties/flips refuse and rises report. Unlike
    pins, this rule compares no identity fields: differing limits resolve
    here unless a retained pin requires human review. A same-price service
    tier rides in its group as an interchangeable tag. Other offerings
    (a second quantization or an unrecognised region spelling) refuse."""
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


def stored_price_continuity(where: str, groups: list[list[Listing]], stored: dict | None
                            ) -> tuple[Listing, str] | None:
    """Take the sole group matching the stored five token rates at ten decimals.

    Search fees and schedules do not identify the token price; two matching
    groups remain ambiguous even when those are their only differences."""
    if stored is None:
        return None
    kept = effective_rates(stored)
    matching = [group for group in groups
                if all(round(float(group[0].rates[field]), 10) == round(float(kept[field]), 10)
                       for field in RATE_FIELDS)]
    if len(matching) != 1:
        return None
    group = matching[0]
    chosen = group[0]
    detail = f"kept the endpoint {chosen.tag!r} at the row's price; assumes the tracked endpoint did not reprice this hour"
    return chosen, rule_notice(where, "stored price continuity", detail, group)


def price_order(where: str, prices: list[Listing],
                stored: dict | None) -> tuple[Listing, str | None]:
    """Cheapest by cache read, input, then output; report possible twin switches.

    Price alone identifies twins, so ties/flips refuse for human review.
    Rises only report: the tracked twin rising above the other looks like
    the other becoming the row's price. Either raises the row's price,
    unless the other simultaneously fell."""
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
    """A tag naming one quantization with only region suffixes beside it."""
    return (namespace_of(tag) == namespace
            and _tag_quantizations(tag) == {quantization}
            and all(suffix == quantization or REGION_RE.fullmatch(suffix)
                    for suffix in tag_suffixes(tag)))


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
