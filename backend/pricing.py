"""Per-model cost rates (USD per million tokens).

SINGLE SOURCE OF TRUTH for cost in claudit. Mirrored by src/parser.js
(SV-PARSER-SPEC) — keep both in lockstep. Bump PARSER_VERSION when this
table changes; every session reparses.

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

Rates are a function of (model, timestamp): a model may carry dated
overrides (e.g. an introductory price). Cost must be computed against the
timestamp of the request being priced, not the time of rendering.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

UTC = timezone.utc


# List prices. Order: most-specific first.
MODEL_RATES = {
    # bonsai-2-27b is served by a local llama.cpp (the operator's own
    # hardware), so there is no price. Listed rather than left to the
    # DEFAULT fallback, which would bill a free lane at Opus list.
    "bonsai-2-27b":      {"fresh": 0.00,  "create_5m": 0.00,  "create_1h": 0.00,  "read": 0.00,  "output": 0.00},
    # GLM (Z.ai): cache WRITES are free and reads are 0.2x input, so the
    # Anthropic 1.25x/2x/0.1x relations do not hold; explicit numbers.
    "glm-5-3-flash":     {"fresh": 0.15, "create_5m": 0.00,  "create_1h": 0.00,  "read": 0.03,  "output": 0.50},
    # Codex (OpenAI) and Kimi lanes, merged into claudit's one table from
    # codexmeter's pricing (D4/D6): every rate explicit. D4 prices a cache
    # write at ONE rate whatever TTL the record declares (or fails to
    # declare), so create_5m and create_1h carry the same value and an
    # unsplit write is billed identically. Kimi bills cache_create at a
    # flat ZERO; Codex cache writes at 1.25x uncached input, reads at 0.1x
    # -- confirmed against OpenAI's own Sol table (4 / 0.4 / 5 / 20) and
    # Astra table (10 / 1 / 12.50 / 50). GPT-6 Sol and Luna are not in
    # codexmeter (D6). Keys are written in _normalise form (dots folded to
    # dashes): a record's "gpt-5.6-sol" normalises to "gpt-5-6-sol" before
    # matching.
    "kimi-k3":           {"fresh": 3.00,  "create_5m": 0.00,  "create_1h": 0.00,  "read": 0.30,  "output": 15.00},
    "kimi-k2-7-code":    {"fresh": 0.95,  "create_5m": 0.00,  "create_1h": 0.00,  "read": 0.19,  "output": 4.00},
    "kimi-k2-6":         {"fresh": 0.95,  "create_5m": 0.00,  "create_1h": 0.00,  "read": 0.16,  "output": 4.00},
    "gpt-6-astra":       {"fresh": 10.00, "create_5m": 12.50, "create_1h": 12.50, "read": 1.00,  "output": 50.00},
    "gpt-6-sol":         {"fresh": 2.00,  "create_5m": 2.50,  "create_1h": 2.50,  "read": 0.20,  "output": 10.00},
    "gpt-6-luna":        {"fresh": 0.10,  "create_5m": 0.125, "create_1h": 0.125, "read": 0.01,  "output": 0.50},
    "gpt-5-6-sol":       {"fresh": 4.00,  "create_5m": 5.00,  "create_1h": 5.00,  "read": 0.40,  "output": 20.00},
    "gpt-5-6-terra":     {"fresh": 2.00,  "create_5m": 2.50,  "create_1h": 2.50,  "read": 0.20,  "output": 12.00},
    "gpt-5-6-luna":      {"fresh": 0.20,  "create_5m": 0.25,  "create_1h": 0.25,  "read": 0.02,  "output": 1.20},
    # Fable 5.1 / Mythos 5.1 price cache HITS at 0.025x base input, not the
    # 0.1x every other model uses — reads are 0.25, a quarter of Fable 5's.
    "claude-fable-5-1":  {"fresh": 10.00, "create_5m": 12.50, "create_1h": 20.00, "read": 0.25, "output": 50.00},
    "claude-mythos-5-1": {"fresh": 10.00, "create_5m": 12.50, "create_1h": 20.00, "read": 0.25, "output": 50.00},
    "claude-fable-5":    {"fresh": 10.00, "create_5m": 12.50, "create_1h": 20.00, "read": 1.00, "output": 50.00},
    "claude-mythos-5":   {"fresh": 10.00, "create_5m": 12.50, "create_1h": 20.00, "read": 1.00, "output": 50.00},
    # Opus 5.5 prices cache HITS at 0.05x base input (0.20 on a 4.00 base).
    "claude-opus-5-5":   {"fresh": 4.00,  "create_5m": 5.00,  "create_1h": 8.00,  "read": 0.20, "output": 20.00},
    "claude-opus-5":     {"fresh": 5.00,  "create_5m": 6.25,  "create_1h": 10.00, "read": 0.50, "output": 25.00},
    "claude-opus-4-8":   {"fresh": 5.00,  "create_5m": 6.25,  "create_1h": 10.00, "read": 0.50, "output": 25.00},
    "claude-opus-4-7":   {"fresh": 5.00,  "create_5m": 6.25,  "create_1h": 10.00, "read": 0.50, "output": 25.00},
    "claude-opus-4-6":   {"fresh": 5.00,  "create_5m": 6.25,  "create_1h": 10.00, "read": 0.50, "output": 25.00},
    "claude-opus-4-5":   {"fresh": 5.00,  "create_5m": 6.25,  "create_1h": 10.00, "read": 0.50, "output": 25.00},
    "claude-opus-4-1":   {"fresh": 15.00, "create_5m": 18.75, "create_1h": 30.00, "read": 1.50, "output": 75.00},
    "claude-opus-4":     {"fresh": 15.00, "create_5m": 18.75, "create_1h": 30.00, "read": 1.50, "output": 75.00},
    "claude-sonnet-5":   {"fresh": 2.00,  "create_5m": 2.50,  "create_1h": 4.00,  "read": 0.20, "output": 10.00},
    "claude-sonnet-4-6": {"fresh": 3.00,  "create_5m": 3.75,  "create_1h": 6.00,  "read": 0.30, "output": 15.00},
    "claude-sonnet-4-5": {"fresh": 3.00,  "create_5m": 3.75,  "create_1h": 6.00,  "read": 0.30, "output": 15.00},
    "claude-sonnet-4":   {"fresh": 3.00,  "create_5m": 3.75,  "create_1h": 6.00,  "read": 0.30, "output": 15.00},
    "claude-haiku-4-5":  {"fresh": 1.00,  "create_5m": 1.25,  "create_1h": 2.00,  "read": 0.10, "output": 5.00},
    "claude-3-7-sonnet-": {"fresh": 3.00, "create_5m": 3.75,  "create_1h": 6.00,  "read": 0.30, "output": 15.00},
    "claude-3-5-sonnet-": {"fresh": 3.00, "create_5m": 3.75,  "create_1h": 6.00,  "read": 0.30, "output": 15.00},
    "claude-3-5-haiku-": {"fresh": 0.80, "create_5m": 1.00,  "create_1h": 1.60,  "read": 0.08, "output": 4.00},
    "claude-3-opus-":    {"fresh": 15.00, "create_5m": 18.75, "create_1h": 30.00, "read": 1.50, "output": 75.00},
    "claude-3-haiku-":   {"fresh": 0.25, "create_5m": 0.30,  "create_1h": 0.50,  "read": 0.03, "output": 1.25},
}

DEFAULT_RATES = MODEL_RATES["claude-opus-4-7"]

# A Codex request whose prompt exceeds this size bills the WHOLE request at
# the long-context meter: 2x input, 1.5x output. Ported from codexmeter.
# Applied per record by the caller, which is the only place that knows the
# request's prompt size.
LONG_CONTEXT_THRESHOLD = 272_000
LONG_CONTEXT_INPUT_MULT = 2.0
LONG_CONTEXT_OUTPUT_MULT = 1.5


# Dated overrides, per exact key: (end_exclusive_utc, rates). Applied only
# when a timestamp is supplied and only on an EXACT key match — a tier
# fallback never inherits another model's promotional price. Write a window's
# rates in full, same shape as MODEL_RATES (for Anthropic models 5m = 1.25x
# input, 1h = 2x, read = 0.1x; GLM carries its own explicit numbers).
#
# GLM-5.3-Flash launch promotion: 50% off list through 2026-09-09 24:00
# UTC+8 (= 16:00 UTC). List price applies from the cutover on with no code
# change. The window stays after it expires: every PARSER_VERSION bump
# reparses the whole bucket, and a record from inside the window must
# come out at the price in force then — drop it and the next reparse
# silently reprices that history at list.
_GLM_FLASH_PROMO = {"fresh": 0.075, "create_5m": 0.00, "create_1h": 0.00, "read": 0.015, "output": 0.25}

# The two GPT-5.6 repricings, ported from codexmeter, as frozen UTC
# instants — NOT live expressions. Each is the moment of OpenAI's own
# @Product Updates post, the finest resolution available; the posts say
# "starting today" and carry no separate effective time. The source
# timestamps were read in Europe/Prague (CEST, UTC+2), so each is the
# posted wall clock minus two hours. If that reading is wrong the
# boundary moves by exactly that offset and nothing else about the
# mechanism changes.
JUL30_CUT = datetime(2026, 7, 30, 18, 12, tzinfo=UTC)   # 20:12 Europe/Prague
AUG21_CUT = datetime(2026, 8, 21, 19, 40, tzinfo=UTC)   # 21:40 Europe/Prague

# The GPT-5.6 family repriced twice, and each cut moved a different subset:
#   2026-07-09  GA                sol 5/30     terra 2.50/15   luna 1/6
#   2026-07-30  luna -80%, terra -20%          sol untouched
#   2026-08-21  sol -20% in / -33% out         terra and luna untouched
# Sol's cut is promotional, announced as running at least through
# 2026-11-21. Nothing is encoded for that: a reversion that has not happened
# is not a rate, and guessing one would silently overbill every record after
# the guessed date. Add a window when it actually moves.
#
# Ordered oldest-first, and _dated returns the FIRST window the timestamp
# falls before, so a key may carry several.
DATED_RATES: dict[str, list[tuple[datetime, dict]]] = {
    "glm-5-3-flash": [
        (datetime(2026, 9, 9, 16, 0, tzinfo=UTC), _GLM_FLASH_PROMO),
    ],
    "gpt-5-6-sol": [
        (AUG21_CUT, {"fresh": 5.00, "create_5m": 6.25, "create_1h": 6.25,
                     "read": 0.50, "output": 30.00}),
    ],
    "gpt-5-6-terra": [
        (JUL30_CUT, {"fresh": 2.50, "create_5m": 3.125, "create_1h": 3.125,
                     "read": 0.25, "output": 15.00}),
    ],
    "gpt-5-6-luna": [
        (JUL30_CUT, {"fresh": 1.00, "create_5m": 1.25, "create_1h": 1.25,
                     "read": 0.10, "output": 6.00}),
    ],
}

# Sorted boundaries where any rate changes. Read-time aggregation that
# re-derives rates from summed tokens must group by these, or its
# per-component breakdown drifts from the stored per-record cost.
RATE_EPOCHS: list[datetime] = sorted(
    {end for windows in DATED_RATES.values() for end, _ in windows}
)

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

    @property
    def estimated(self) -> bool:
        return self.kind != "exact"


def _normalise(model: str) -> str:
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


def _match_key(norm: str) -> str | None:
    for key in MODEL_RATES:
        if not norm.startswith(key):
            continue
        rest = norm[len(key):]
        if rest == "" or rest[0] in "[@" or _SNAPSHOT_SUFFIX.match(rest):
            return key
    return None


def _dated(key: str, ts: datetime | None) -> dict:
    windows = DATED_RATES.get(key)
    if not windows or ts is None:
        return MODEL_RATES[key]
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    for end_exclusive, rates in windows:
        if ts < end_exclusive:
            return rates
    return MODEL_RATES[key]


def resolve(model: str, ts: datetime | None = None) -> Resolution:
    """Resolve a model id to rates, reporting how confident the match is."""
    norm = _normalise(model)
    key = _match_key(norm)
    if key is not None:
        return Resolution(_dated(key, ts), "exact", key)
    for pattern, rates in _TIER_FALLBACKS:
        if pattern.search(norm):
            return Resolution(rates, "tier")
    return Resolution(DEFAULT_RATES, "default")


def rate_for(model: str, ts: datetime | None = None) -> dict:
    """Rates for a model at a point in time. Omitting ts yields list price."""
    return resolve(model, ts).rates


def compute_cost(
    model: str,
    *,
    fresh: int,
    output: int,
    eph5: int,
    eph1h: int,
    unsplit_create: int,
    read: int,
    ts: datetime | None = None,
    long_context: bool = False,
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
    caller is unaffected: no Kimi caller passes it (the wire format has no
    such tier), and neither does a Codex record on a subscription.
    """
    r = rate_for(model, ts)
    in_mult = LONG_CONTEXT_INPUT_MULT if long_context else 1.0
    out_mult = LONG_CONTEXT_OUTPUT_MULT if long_context else 1.0
    return (
        fresh * r["fresh"] * in_mult / 1_000_000
        + eph5 * r["create_5m"] * in_mult / 1_000_000
        + (eph1h + unsplit_create) * r["create_1h"] * in_mult / 1_000_000
        + read * r["read"] * in_mult / 1_000_000
        + output * r["output"] * out_mult / 1_000_000
    )
