"""Resolve per-record USD rates from shared pricing data (SV-RATE-DATA).

Free/stealth ids price at zero before all table matches and count as exact:
zero is intentional, never an estimate.
A serving-provider pair wins when available at the request timestamp;
otherwise match model keys or bare vendor forms, then estimated Claude tier
rates, then an estimated default. Longest model-key matches permit snapshot
and bracket suffixes, never unknown model versions that would silently
inherit a different generation's price.

Use each request's timestamp for dated rates and provider schedules. Cache
writes with declared TTLs use their rate columns; unsplit writes use the
1h rate (SV-COST-SPLIT). Meter membership, thresholds and optional factors
are pricing data. Bump PRICING_VERSION when a change reprices stored rows."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from backend.long_context import (  # noqa: F401  (re-export)  # pylint: disable=unused-import
    LONG_CONTEXT_INPUT_MULT, LONG_CONTEXT_OUTPUT_MULT, LONG_CONTEXT_THRESHOLD)
from backend.resolution import Resolution  # noqa: F401  (re-export)  # pylint: disable=unused-import
from backend.model_names import (  # noqa: F401  (re-export)  # pylint: disable=unused-import
    _PERMASLUG_DATE,
    _SNAPSHOT_SUFFIX,
    _VARIANT_SUFFIX,
    _VERSIONED_KEY,
    _is_free,
    _normalise,
    _variant_folded,
)
from backend.pricing_load import (
    DATED_RATES,
    DEFAULT_RATES,
    MODEL_RATES,
    PROVIDER_DATED_RATES,
    PROVIDER_RATES,
    PROVIDER_RATES_FETCHED,
    PROVIDER_SCHEDULES,
    PROVIDER_STARTS,
    PRICING_JSON,
    RATE_EPOCHS,
    RATE_FIELDS,
    LONG_CONTEXT_MODELS,
    LONG_CONTEXT_METERS,
    VENDOR_BARE,
    VENDOR_PREFIXES,
    VENDOR_HOSTS,
    _DAYS,
    Windows,
    RateTables,
    ScheduleWindow,
    load_tables,
)
from backend.pricing_load import (  # noqa: F401  (re-export)  # pylint: disable=unused-import
    _INSTANT,
    _hhmm,
    _history,
    _instant,
    _schedule,
)

__all__ = [  # re-exports the rate tables and their loader (SV-RATE-DATA)
    "DATED_RATES", "DEFAULT_RATES", "LONG_CONTEXT_MODELS",
    "LONG_CONTEXT_METERS",
    "MODEL_RATES", "PROVIDER_DATED_RATES",
    "PROVIDER_RATES", "PROVIDER_RATES_FETCHED",
    "PROVIDER_SCHEDULES",
    "PROVIDER_STARTS", "PRICING_JSON", "RATE_EPOCHS", "RATE_FIELDS",
    "VENDOR_BARE", "VENDOR_PREFIXES", "VENDOR_HOSTS",
    "Windows", "RateTables", "ScheduleWindow", "load_tables",
]

UTC = timezone.utc

# Every rate an OpenRouter free model carries: zero. Returned for any id
# ending in ":free" or starting with "stealth/" (see _is_free).
FREE_RATES = dict.fromkeys(RATE_FIELDS, 0.00)


def _vendor_row(key: str) -> tuple[str, str] | None:
    """The (tracked key, vendor host) row a bare key's rates live at, or
    None when the key names no tracked vendor row — or names one whose
    providers row does not exist yet (a model auto-added to the tracked
    table before its first provider row: the bare path falls through the
    same way)."""
    tracked = VENDOR_BARE.get(key)
    if tracked not in VENDOR_HOSTS:
        return None
    row = (tracked, VENDOR_HOSTS[tracked])
    return row if row in PROVIDER_RATES else None


def _list_rates(key: str) -> dict:
    """A key's list rates in the merged view: the models-table row when
    the key names one, else its tracked vendor row's."""
    if key in MODEL_RATES:
        return MODEL_RATES[key]
    row = _vendor_row(key)
    if row is None:
        raise KeyError(key)
    return PROVIDER_RATES[row]


def _key_windows(key: str) -> Windows | None:
    """The dated windows that move a bare key's rates in the merged
    view: the models-table row's when the key names one, else its tracked
    vendor row's."""
    if key in MODEL_RATES:
        return DATED_RATES.get(key)
    row = _vendor_row(key)
    return PROVIDER_DATED_RATES.get(row) if row else None


def _latest(*families: str) -> dict | None:
    """Rates of the highest-versioned key of `families` in the merged
    view (models-table keys and tracked vendor bare keys), or None when
    no family member carries a row — a family with no rates contributes
    no fallback instead of refusing every resolve (the bench's bounded
    document and any patched-tables test name none of the families).

    ``claude-opus-5-5`` is version (5, 5); legacy ``claude-3-opus-`` keys
    do not match. A tracked key with no provider row yet — the auto-add's
    pickup delay, which the loaders admit — names no rates and is
    skipped, exactly as resolve()'s bare path falls through it. Ties keep
    table order (max returns the first).
    """
    versions = [
        (tuple(int(p) for p in m.group(2).split("-")), key)
        for key in {**MODEL_RATES, **VENDOR_BARE}
        if (m := _VERSIONED_KEY.match(key)) and m.group(1) in families
        and (key in MODEL_RATES or _vendor_row(key) is not None)
    ]
    if not versions:
        return None
    return _list_rates(max(versions, key=lambda v: v[0])[1])


# Family fallbacks for unrecognised Claude models — current-generation
# rates for the tier, at LIST price (never a dated promotion). Derived
# from the table, so adding a newer model moves its family's fallback.
# Derived on first use: the bench's bounded document need not
# carry every family's row, and a family with no row at all must not
# refuse the IMPORT for a fallback no unrecognised id has hit yet.
_TIER_FALLBACKS: tuple[tuple[re.Pattern, dict], ...] | None = None


def _tier_fallbacks() -> tuple[tuple[re.Pattern, dict], ...]:
    global _TIER_FALLBACKS
    if _TIER_FALLBACKS is None:
        _TIER_FALLBACKS = tuple(
            (pattern, rates) for pattern, rates in (
                (re.compile(r"fable|mythos"), _latest("fable", "mythos")),
                (re.compile(r"opus"), _latest("opus")),
                (re.compile(r"sonnet"), _latest("sonnet")),
                (re.compile(r"haiku"), _latest("haiku")),
            )
            if rates is not None)
    return _TIER_FALLBACKS


def clear_tier_fallbacks() -> None:
    """Forget the derived tier table. Tests patch the tables between
    calls and go through rate_fingerprint.clear_fingerprint_cache(), the
    same contract the match-key memo honors: a tier
    entry derived under a patch must never outlive it."""
    global _TIER_FALLBACKS
    _TIER_FALLBACKS = None


_MATCH_KEY_CACHE: dict[str, str | None] = {}


def _match_key(norm: str) -> str | None:
    """Longest model-table key matching norm, independent of table order.

    Allow snapshot suffixes without folding a different model version.
    Memoize per normalized id: reprice calls per row, while the loaded
    immutable table makes the match a process constant, avoiding an
    O(number of model keys) scan on each call."""
    try:
        return _MATCH_KEY_CACHE[norm]
    except KeyError:
        pass
    best = None
    for key in MODEL_RATES:
        if not norm.startswith(key) or (best and len(key) <= len(best)):
            continue
        rest = norm[len(key):]
        if rest == "" or rest[0] in "[@" or _SNAPSHOT_SUFFIX.match(rest):
            best = key
    _MATCH_KEY_CACHE[norm] = best
    return best


_VENDOR_MATCH_CACHE: dict[str, str | None] = {}
# The loaded table is stable; synthetic tables in tests replace the list.
_VENDOR_PREFIXES_SOURCE = VENDOR_PREFIXES
_VENDOR_PREFIXES_SET = frozenset(VENDOR_PREFIXES)


def _vendor_match(norm: str) -> str | None:
    """The tracked key named by a bare form, using the models table's suffix rules."""
    try:
        return _VENDOR_MATCH_CACHE[norm]
    except KeyError:
        pass
    best_form: str | None = None
    best_key: str | None = None
    for form, tracked in VENDOR_BARE.items():
        if best_key and len(form) <= len(best_form or ""):
            continue
        if not norm.startswith(form):
            continue
        rest = norm[len(form):]
        if rest == "" or rest[0] in "[@" or _SNAPSHOT_SUFFIX.match(rest):
            best_form, best_key = form, tracked
    _VENDOR_MATCH_CACHE[norm] = best_key
    return best_key


def _fold_vendor_prefix(norm: str) -> str:
    """Fold a tracked namespace prefix only when its remainder is a bare form."""
    prefix, separator, bare = norm.partition("/")
    if not separator:
        return norm
    prefixes = (_VENDOR_PREFIXES_SET
                if VENDOR_PREFIXES is _VENDOR_PREFIXES_SOURCE
                else frozenset(VENDOR_PREFIXES))
    if prefix in prefixes and _vendor_match(bare) is not None:
        return bare
    return norm


def _in_window(windows: list | None, ts: datetime | None,
               list_rates: dict) -> dict:
    if not windows or ts is None:
        return list_rates
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    for end_exclusive, rates in windows:
        if ts < end_exclusive:
            return rates
    return list_rates


def _scheduled(schedule: list[ScheduleWindow], ts: datetime) -> dict | None:
    """The first window `ts` falls in (by UTC weekday and HHMM), or None."""
    ts = (ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts).astimezone(UTC)
    day, hhmm = _DAYS[ts.weekday()], ts.hour * 100 + ts.minute
    for days, start, end, rates in schedule:
        if days is not None and day not in days:
            continue
        if (start is None or end is None or (start <= hhmm < end if start < end
                                             else hhmm >= start or hhmm < end)):
            return rates
    return None


def _provider_rates(pkey: tuple[str, str], ts: datetime | None) -> tuple[dict, bool]:
    """A provider row's rates at `ts`, and whether a schedule set them."""
    windows = PROVIDER_DATED_RATES.get(pkey)
    rates = _in_window(windows, ts, PROVIDER_RATES[pkey])
    if ts is None:
        return rates, False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    entry = sum(1 for end, _ in windows or [] if end <= ts)
    schedule = PROVIDER_SCHEDULES.get(pkey, {}).get(entry)
    if schedule is None:
        return rates, False
    return {**rates, **(_scheduled(schedule, ts) or {})}, True


def _dated(key: str, ts: datetime | None) -> dict:
    return _in_window(DATED_RATES.get(key), ts, MODEL_RATES[key])


def _provider_key(norm: str, provider: str,
                  ts: datetime | None) -> tuple[str, str] | None:
    """The PROVIDER_RATES key for a record, or None.

    Each normalised spelling maps to its tracked key before lookup:
    exact id, permaslug slug, then ONE trailing variant suffix stripped.
    OpenRouter variants (":nitro", ":floor") retain the model's price;
    only ":free" changes price (zero), and
    resolve() prices it before this lookup — it never folds. The exact id
    is tried first, so a table row spelled with the suffix still wins over
    the fold. Never MODEL_RATES' snapshot-suffix tolerance: that would
    read the permaslug as the UNDATED model, a different row at a
    different price. A row that begins at a time does not exist for a
    record before it.
    """
    if ts is not None and ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    for model in (norm, _PERMASLUG_DATE.sub(r"-\1", norm),
                  _variant_folded(norm)):
        model = VENDOR_BARE.get(model, model)
        if (model, provider) in PROVIDER_RATES:
            start = PROVIDER_STARTS.get((model, provider))
            if start is not None and ts is not None and ts < start:
                return None
            return model, provider
    return None


def resolve(model: str | None, ts: datetime | None = None,
            provider: str | None = None) -> Resolution:
    """Resolve a model id to rates, reporting how confident the match is.

    `provider` is the record's serving host. A (model, provider) row wins;
    with no row, or no provider, the model alone decides — a bare
    first-party id through its tracked vendor row (SV-RATE-DATA), at that
    row's web-search rate and without a provider-specific schedule.
    """
    norm = _normalise(model)
    if _is_free(model, norm):
        return Resolution(FREE_RATES, "exact", norm)
    if "/" in norm:
        norm = _fold_vendor_prefix(norm)
    pkey = _provider_key(norm, provider, ts) if provider else None
    if pkey is not None:
        rates, scheduled = _provider_rates(pkey, ts)
        return Resolution(rates, "exact", pkey[0], scheduled)
    key = _match_key(norm)
    if key is not None:
        return Resolution(_dated(key, ts), "exact", key)
    vkey = _vendor_match(norm)
    if vkey in VENDOR_HOSTS:
        row = (vkey, VENDOR_HOSTS[vkey])
        if row in PROVIDER_RATES:
            start = PROVIDER_STARTS.get(row)
            if not (start is not None and ts is not None and ts < start):
                rates = _in_window(PROVIDER_DATED_RATES.get(row), ts,
                                   PROVIDER_RATES[row])
                return Resolution(rates, "exact", vkey, False)
    for pattern, rates in _tier_fallbacks():
        if pattern.search(norm):
            return Resolution(rates, "tier")
    return Resolution(DEFAULT_RATES, "default")


def is_long_context_model(model: str) -> bool:
    """Whether a stored model id names a long-context-metered Codex model.

    Membership is pricing.json's long_context_models — dashed names of
    models-table keys or tracked keys — and the stored id normalises the
    way resolve() normalises, so the dotted gpt-5.6-sol matches tracked
    key gpt-5-6-sol (SV-RATE-ESTIMATES).
    """
    return _normalise(model) in LONG_CONTEXT_MODELS


def long_context_threshold(model: str | None) -> int:
    """Model threshold, defaulting to the global Codex value."""
    return LONG_CONTEXT_METERS.get(_normalise(model), {}).get(
        "threshold", LONG_CONTEXT_THRESHOLD)


def long_context_factors(model: str | None) -> tuple[float, float]:
    """Per-model meter factors, defaulting each omitted side independently."""
    meter = LONG_CONTEXT_METERS.get(_normalise(model), {})
    return (meter.get("input_mult", LONG_CONTEXT_INPUT_MULT),
            meter.get("output_mult", LONG_CONTEXT_OUTPUT_MULT))


def meter_flag(model: str | None, window: int) -> bool | None:
    """Meter decision for billed input: fresh + cache_creation + cache_read.

    Members bill above their own threshold; non-members keep NULL. Store
    the decision in the parse path so repricing derives only what a
    reparse stores. Codex instead applies its threshold test to every
    model directly."""
    norm = _normalise(model)
    if norm not in LONG_CONTEXT_MODELS:
        return None
    return window > LONG_CONTEXT_METERS.get(norm, {}).get(
        "threshold", LONG_CONTEXT_THRESHOLD)


def rate_for(model: str | None, ts: datetime | None = None,
             provider: str | None = None) -> dict:
    """Rates for a model at a point in time. Omitting ts yields list price."""
    return resolve(model, ts, provider).rates


@dataclass(frozen=True)
class CostAdjustments:
    """Optional whole-request cost adjustments beyond the token tally."""
    long_context: bool = False
    web_search_requests: int | None = None


def compute_cost(
    model: str | None,
    *,
    fresh: int,
    output: int,
    eph5: int,
    eph1h: int,
    unsplit_create: int,
    read: int,
    ts: datetime | None = None,
    res: "Resolution | None" = None,
    adjustments: CostAdjustments = CostAdjustments(),
) -> float:
    """USD cost for one request's tokens, dated at its own timestamp.

    Callers compute unsplit_create = max(0, cache_creation - eph5 - eph1h).
    Unsplit writes bill at 1h (SV-COST-SPLIT): main sessions predominantly
    use 1h, while 5m writes chiefly belong to subagents. adjustments carries the
    whole-request meter multiplier and explicit per-search fee. Codex sets
    long_context above threshold; Kimi has no such tier. Hot paths pass res
    to share one rate resolution."""
    if res is None:
        res = resolve(model, ts)
    r = res.rates
    in_mult, out_mult = (long_context_factors(model)
                         if adjustments.long_context else (1.0, 1.0))
    return (
        fresh * r["fresh"] * in_mult / 1_000_000
        + eph5 * r["create_5m"] * in_mult / 1_000_000
        + (eph1h + unsplit_create) * r["create_1h"] * in_mult / 1_000_000
        + read * r["read"] * in_mult / 1_000_000
        + output * r["output"] * out_mult / 1_000_000
        + (adjustments.web_search_requests or 0) * r.get("web_search", 0.0)
    )
