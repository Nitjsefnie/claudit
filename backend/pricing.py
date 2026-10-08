"""Per-model cost rates (USD per million tokens).

The rates themselves are data in src/pricing.json, which src/parser.js
reads too (SV-RATE-DATA); this module loads it and resolves a record to
its rates. Bump PRICING_VERSION when a change reprices stored records.

Cache writes are split by TTL:
  5m write = 1.25x base input (column 'create_5m')
  1h write = 2x base input    (column 'create_1h')

Tokens recorded as cache_creation_input_tokens with NO ephemeral_5m/1h
split are charged at the 1h rate: main sessions write 98.7% of their
cache at 1h and 5m is the subagent exception. See SV-COST-SPLIT in
.claude/rules/claudit-doctrine.md.

Three resolution behaviours matter, in priority order:

1. EXACT — the normalised model id matches a key in MODEL_RATES, allowing
   only a dated-snapshot or bracket suffix after it. A version suffix the
   table doesn't know (``claude-opus-4-9``) deliberately does NOT match the
   shorter ``claude-opus-4`` key: billing a future Opus at retired 15/75
   rates is a silent 3x overcount.
2. TIER — an unrecognised Claude model falls back to its family's
   current-generation rates and is reported as ``kind="tier"`` so callers
   can mark the figure estimated rather than presenting it as fact.
3. DEFAULT — anything else. Also flagged.

Before all three, a model id ending in ``:free`` or starting with
``stealth/`` (OpenRouter's free tier and preview models) prices at ZERO.
The id list churns weekly, so the match is on the id's shape rather than
an enumerated row — checked on the raw id AND its normalised form,
case-insensitively, so no spelling can dodge it, and it outranks an
exact table key (``stealth/claude-opus-4-8`` stays free). It is reported
``kind="exact"``: the zero is a deliberate price, not an estimate, so
the API must not flag it (the same reasoning as the bonsai-2-27b row).

Rates are a function of (model, timestamp): a model may carry dated
overrides (e.g. an introductory price). Cost must be computed against the
timestamp of the request being priced, not the time of rendering.

A record that names its serving provider (OpenRouter's
``message.provider``) is priced from PROVIDER_RATES, keyed by
(normalised model, provider), when that pair has a row; otherwise, and
always when the provider is absent, by the model alone as above. A record
with no provider therefore prices exactly as it did before the provider
table existed.
"""
from __future__ import annotations

import re
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
    FEES,
    MODEL_RATES,
    PROVIDER_DATED_RATES,
    PROVIDER_FEES,
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
    "DATED_RATES", "DEFAULT_RATES", "FEES", "LONG_CONTEXT_MODELS",
    "LONG_CONTEXT_METERS",
    "MODEL_RATES", "PROVIDER_DATED_RATES", "PROVIDER_FEES",
    "PROVIDER_RATES", "PROVIDER_RATES_FETCHED", "PROVIDER_SCHEDULES",
    "PROVIDER_STARTS", "PRICING_JSON", "RATE_EPOCHS", "RATE_FIELDS",
    "VENDOR_BARE", "VENDOR_HOSTS",
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
    if tracked is None:
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


def _latest(*families: str) -> dict:
    """Rates of the highest-versioned key of `families` in the merged
    view (models-table keys and tracked vendor bare keys).

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
    return _list_rates(max(versions, key=lambda v: v[0])[1])


# Family fallbacks for unrecognised Claude models — current-generation
# rates for the tier, at LIST price (never a dated promotion). Derived
# from the table, so adding a newer model moves its family's fallback.
# Derived on first use (issue #840): the bench's bounded document need not
# carry every family's row, and a family with no row at all must not
# refuse the IMPORT for a fallback no unrecognised id has hit yet.
_TIER_FALLBACKS: tuple[tuple[re.Pattern, dict], ...] | None = None


def _tier_fallbacks() -> tuple[tuple[re.Pattern, dict], ...]:
    global _TIER_FALLBACKS
    if _TIER_FALLBACKS is None:
        _TIER_FALLBACKS = (
            (re.compile(r"fable|mythos"), _latest("fable", "mythos")),
            (re.compile(r"opus"), _latest("opus")),
            (re.compile(r"sonnet"), _latest("sonnet")),
            (re.compile(r"haiku"), _latest("haiku")),
        )
    return _TIER_FALLBACKS


_MATCH_KEY_CACHE: dict[str, str | None] = {}


def _match_key(norm: str) -> str | None:
    """The LONGEST models-table key `norm` names, so
    ``claude-opus-4-1-20250805`` is claude-opus-4-1, never claude-opus-4
    — whatever the table order.

    Memoized per normalised id (issue #350): the reprice pass calls this
    once per stored record — over a million rows naming fewer than a
    dozen distinct models — and the scan is O(|MODEL_RATES|) startswith
    checks per call. The table loads once at import and is immutable at
    runtime, so the result per norm is a process constant and a dict hit
    replaces the scan. Same outputs as the scan, every call.
    """
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


def _vendor_match(norm: str) -> str | None:
    """The tracked key whose vendor bare form `norm` names, by the models
    table's own longest-match and suffix rules (empty rest, ``[``, ``@``,
    a snapshot suffix). A transcript id spelled WITH the vendor prefix
    (``z-ai/glm-5-3``) matches no bare form: it names the OpenRouter
    catalog model, not the first-party id, and keeps pricing default.
    Memoized exactly like _match_key."""
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
    return _scheduled(schedule, ts) or rates, True


def _dated(key: str, ts: datetime | None) -> dict:
    return _in_window(DATED_RATES.get(key), ts, MODEL_RATES[key])


def _provider_key(norm: str, provider: str,
                  ts: datetime | None) -> tuple[str, str] | None:
    """The PROVIDER_RATES key for a record, or None.

    Exact on the normalised id, on its permaslug folded to the slug, or on
    the id with ONE trailing variant suffix stripped: OpenRouter's variant
    suffixes (":nitro", ":floor") are service tiers that do not change the
    price, so a tiered id resolves to the bare model's row and prices at
    the bare model's rate; only ":free" changes price (zero), and
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
        if (model, provider) in PROVIDER_RATES:
            start = PROVIDER_STARTS.get((model, provider))
            if start is not None and ts is not None and ts < start:
                return None
            return model, provider
    return None


def _fee_at(fees: dict, key, windows: Windows | None, ts: datetime | None
            ) -> float:
    """The per-request fee of the history entry in force at `ts` — the
    newest entry (the list price) when `ts` is None, the entry the
    window walk selects otherwise. Keyed by entry index exactly like
    PROVIDER_SCHEDULES: windows[i] is entry i, the tail entry is index
    len(windows)."""
    if key is None:
        return 0.0
    row = fees.get(key)
    if not row:
        return 0.0
    if ts is None:
        return row.get(len(windows or []), 0.0)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return row.get(sum(1 for end, _ in windows or [] if end <= ts), 0.0)


def resolve(model: str | None, ts: datetime | None = None,
            provider: str | None = None) -> Resolution:
    """Resolve a model id to rates, reporting how confident the match is.

    `provider` is the record's serving host. A (model, provider) row wins;
    with no row, or no provider, the model alone decides — a bare
    first-party id through its tracked vendor row (SV-RATE-DATA), its
    fee-free and schedule-free terms.
    """
    norm = _normalise(model)
    if _is_free(model, norm):
        return Resolution(FREE_RATES, "exact", norm)
    pkey = _provider_key(norm, provider, ts) if provider else None
    if pkey is not None:
        rates, scheduled = _provider_rates(pkey, ts)
        return Resolution(rates, "exact", pkey[0], scheduled,
                          request_fee=_fee_at(
                              PROVIDER_FEES, pkey,
                              PROVIDER_DATED_RATES.get(pkey), ts))
    key = _match_key(norm)
    if key is not None:
        return Resolution(_dated(key, ts), "exact", key,
                          request_fee=_fee_at(
                              FEES, key, DATED_RATES.get(key), ts))
    vkey = _vendor_match(norm)
    if vkey is not None:
        row = (vkey, VENDOR_HOSTS[vkey])
        if row in PROVIDER_RATES:
            start = PROVIDER_STARTS.get(row)
            if not (start is not None and ts is not None and ts < start):
                rates = _in_window(PROVIDER_DATED_RATES.get(row), ts,
                                   PROVIDER_RATES[row])
                return Resolution(rates, "exact", vkey, False, 0.0)
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
    """The meter threshold in force for a model (issue #765): its
    long_context_meters entry when the card carries one, else the Codex
    meter's global threshold. The id normalises the way
    is_long_context_model normalises."""
    return LONG_CONTEXT_METERS.get(_normalise(model),
                                   LONG_CONTEXT_THRESHOLD)


def meter_flag(model: str | None, window: int) -> bool | None:
    """The meter decision for one record of `model` at input window
    `window` — the billed input side, fresh + cache_creation + cache_read.

    A member above its own threshold bills the band and a member at or
    below it stays flat; every non-member keeps the NULL marker, the
    Claude format's parse-stored shape (issue #249: the reprice pass
    re-derives only what a reparse would store, so the decision lives in
    the parse path). The Codex lane passes its threshold test alone and
    never consults this — parse_codex bills every record above the
    threshold whatever its model (issue #194)."""
    norm = _normalise(model)
    if norm not in LONG_CONTEXT_MODELS:
        return None
    return window > long_context_threshold(norm)


def rate_for(model: str | None, ts: datetime | None = None,
             provider: str | None = None) -> dict:
    """Rates for a model at a point in time. Omitting ts yields list price."""
    return resolve(model, ts, provider).rates


def request_fee(model: str, ts: datetime | None = None,
                provider: str | None = None) -> float:
    """The per-request fee in force for one request (issue #469), resolved
    exactly like rates: a (model, provider) row's entry note decides, the
    model row when no provider row applies. Zero when neither carries a
    fee. Omitting ts yields the list entry's fee."""
    return resolve(model, ts, provider).request_fee


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
    long_context: bool = False,
    res: "Resolution | None" = None,
) -> float:
    """USD cost for one request's token tally.

    unsplit_create = max(0, cache_creation_input_tokens - eph5 - eph1h);
    must already be computed by the caller. Pass the record's own
    timestamp so dated rates apply to when the tokens were spent.

    A write with no declared TTL is priced as 1h: main sessions write
    98.7% of their cache at 1h, and 5m is the subagent exception (96% of
    all 5m writes). See SV-COST-SPLIT.

    long_context applies the Codex long-context meter (2x input side,
    1.5x output) to the whole request. It defaults off, so every existing
    caller is unaffected: every Codex record above the threshold bills
    the meter, whatever plan served the rollout (issue #194); no Kimi
    caller passes it (the wire format has no such tier).

    A per-request fee the resolved entry's note records (issue #469) is
    folded in ONCE per call — one call prices one request — so cost_usd
    is what the session cost. The fee's provenance stays in the provider
    row's note and in records.request_fee_usd. A caller that also needs
    the fee split out — or that prices per record on a hot path —
    resolves once itself and passes `res`: the Resolution carries the
    rates, the serving host's fee and the record's own timestamp's dated
    window, so one request costs one resolution and the (model, ts,
    provider) triple is resolved in exactly one place.
    """
    if res is None:
        res = resolve(model, ts)
    r = res.rates
    in_mult = LONG_CONTEXT_INPUT_MULT if long_context else 1.0
    out_mult = LONG_CONTEXT_OUTPUT_MULT if long_context else 1.0
    return (
        fresh * r["fresh"] * in_mult / 1_000_000
        + eph5 * r["create_5m"] * in_mult / 1_000_000
        + (eph1h + unsplit_create) * r["create_1h"] * in_mult / 1_000_000
        + read * r["read"] * in_mult / 1_000_000
        + output * r["output"] * out_mult / 1_000_000
        + res.request_fee
    )
