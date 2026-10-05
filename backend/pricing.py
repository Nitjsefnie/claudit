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
from dataclasses import dataclass
from datetime import datetime, timezone

from backend.long_context import (  # noqa: F401  (re-export)  # pylint: disable=unused-import
    LONG_CONTEXT_INPUT_MULT, LONG_CONTEXT_OUTPUT_MULT, LONG_CONTEXT_THRESHOLD)
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
    "MODEL_RATES", "PROVIDER_DATED_RATES", "PROVIDER_FEES",
    "PROVIDER_RATES", "PROVIDER_RATES_FETCHED", "PROVIDER_SCHEDULES",
    "PROVIDER_STARTS", "PRICING_JSON", "RATE_EPOCHS", "RATE_FIELDS",
    "Windows", "RateTables", "ScheduleWindow", "load_tables",
]

UTC = timezone.utc

# Every rate an OpenRouter free model carries: zero. Returned for any id
# ending in ":free" or starting with "stealth/" (see _is_free).
FREE_RATES = dict.fromkeys(RATE_FIELDS, 0.00)

# The two GPT-5.6 repricing instants, named for the tests that price
# around them.
JUL30_CUT = DATED_RATES["gpt-5-6-terra"][0][0]
AUG21_CUT = DATED_RATES["gpt-5-6-sol"][0][0]

_VERSIONED_KEY = re.compile(r"^claude-([a-z]+)-(\d+(?:-\d+)*)$")


def _latest(*families: str) -> dict:
    """Rates of the highest-versioned table key in `families`.

    ``claude-opus-5-5`` is version (5, 5); legacy ``claude-3-opus-`` keys
    do not match. Ties keep table order (max returns the first).
    """
    versions = [
        (tuple(int(p) for p in m.group(2).split("-")), key)
        for key in MODEL_RATES
        if (m := _VERSIONED_KEY.match(key)) and m.group(1) in families
    ]
    return MODEL_RATES[max(versions, key=lambda v: v[0])[1]]


# Family fallbacks for unrecognised Claude models — current-generation
# rates for the tier, at LIST price (never a dated promotion). Derived
# from the table, so adding a newer model moves its family's fallback.
_TIER_FALLBACKS: tuple[tuple[re.Pattern, dict], ...] = (
    (re.compile(r"fable|mythos"), _latest("fable", "mythos")),
    (re.compile(r"opus"), _latest("opus")),
    (re.compile(r"sonnet"), _latest("sonnet")),
    (re.compile(r"haiku"), _latest("haiku")),
)

# A dated snapshot suffix ("-20250514") is the same model; a short version
# suffix ("-9") or a mode suffix ("-fast") is a DIFFERENT model.
_SNAPSHOT_SUFFIX = re.compile(r"^-?\d{6,8}$")


@dataclass(frozen=True)
class Resolution:
    """Outcome of resolving a model id to rates.

    kind: "exact" | "tier" | "default". Anything other than "exact" means
    the figure is an estimate and should be surfaced as such.
    """
    rates: dict
    kind: str
    key: str | None = None
    # True when the rates came from a weekly schedule's window or default
    # for this record's own time: a fold re-deriving cost at one
    # representative time cannot reproduce them (SV-RATE-DATA).
    scheduled: bool = False
    # The serving host's per-request fee in force (issue #469): USD this
    # one request costs beside its tokens, folded into compute_cost's
    # total and stored on records.request_fee_usd. Zero when the resolved
    # entry carries no fee note (every non-OpenRouter lane, every
    # unmodelled listing).
    request_fee: float = 0.0

    @property
    def estimated(self) -> bool:
        return self.kind != "exact"


def _normalise(model: str | None) -> str:
    """Strip provider/region prefixes and normalise version separators.

    ``anthropic/claude-opus-4.8`` and ``us.anthropic.claude-opus-4-8``
    both denote the same model as ``claude-opus-4-8``.
    """
    m = (model or "").strip().lower()
    if not m:
        return ""
    # Everything before the first "claude" is provider/region routing.
    i = m.find("claude")
    if i > 0:
        m = m[i:]
    return m.replace(".", "-")


def _is_free(model: str | None, norm: str) -> bool:
    """True for an OpenRouter free model: an id ending in ``:free`` or
    starting with ``stealth/``, case-insensitively.

    Checked on the raw id as well as its normalised form because
    ``_normalise`` strips everything before ``claude`` — a
    ``stealth/claude-…`` id loses that prefix in ``norm`` and only the
    raw check still sees it. (The ``:free`` suffix survives every
    normalisation step; the raw check covers it symmetrically.)
    """
    raw = (model or "").strip().lower()
    return (norm.endswith(":free") or norm.startswith("stealth/")
            or raw.endswith(":free") or raw.startswith("stealth/"))


_MATCH_KEY_CACHE: dict[str, str | None] = {}


def _match_key(norm: str) -> str | None:
    """The LONGEST table key `norm` names, so ``claude-opus-4-1-20250805``
    is claude-opus-4-1, never claude-opus-4 — whatever the table order.

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


# OpenRouter's dated permaslug ("deepseek/deepseek-v4-flash-20260731") names
# the same model as its short slug ("deepseek/deepseek-v4-flash-0731").
_PERMASLUG_DATE = re.compile(r"-20\d{2}(\d{4})$")

# OpenRouter's variant suffix (":nitro", ":floor") names a service tier,
# not a price: the tiered id is the bare model at the bare model's price.
# Only ":free" changes price (zero), and resolve() prices it before any
# provider lookup — the guard here keeps a direct caller honest too.
_VARIANT_SUFFIX = re.compile(r":([^:]*)$")


def _variant_folded(norm: str) -> str:
    """`norm` without ONE trailing ":<suffix>", when that suffix is not
    "free" (case-insensitively); `norm` itself otherwise. Mirrored by
    parser.js's _providerModelKey."""
    m = _VARIANT_SUFFIX.search(norm)
    if m is None or m.group(1).lower() == "free":
        return norm
    return norm[: m.start()]


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
    with no row, or no provider, the model alone decides.
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
    for pattern, rates in _TIER_FALLBACKS:
        if pattern.search(norm):
            return Resolution(rates, "tier")
    return Resolution(DEFAULT_RATES, "default")


def is_long_context_model(model: str) -> bool:
    """Whether a stored model id names a long-context-metered Codex model.

    Membership is pricing.json's long_context_models — dashed models-table
    keys — and the stored id normalises the way resolve() normalises, so
    the dotted gpt-5.6-sol matches key gpt-5-6-sol (SV-RATE-ESTIMATES).
    """
    return _normalise(model) in LONG_CONTEXT_MODELS


def rate_for(model: str, ts: datetime | None = None,
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
