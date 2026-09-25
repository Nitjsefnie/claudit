"""Per-model cost rates (USD per million tokens).

The rates themselves are data in src/pricing.json, which src/parser.js
reads too (SV-RATE-DATA); this module loads it and resolves a record to
its rates. Bump PARSER_VERSION when a change reprices stored records.

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

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TypedDict

UTC = timezone.utc


# Every rate lives in src/pricing.json (SV-RATE-DATA); this module holds
# resolution logic only. The file sits under src/ because the browser's
# parser.js reads the same file from /src.
PRICING_JSON = Path(__file__).resolve().parent.parent / "src" / "pricing.json"
RATE_FIELDS = ("fresh", "create_5m", "create_1h", "read", "output")

Windows = list[tuple[datetime, dict]]


class RateTables(TypedDict):
    """load_tables' result, keyed by the module attribute each value binds."""
    MODEL_RATES: dict[str, dict]
    DATED_RATES: dict[str, Windows]
    PROVIDER_RATES: dict[tuple[str, str], dict]
    PROVIDER_DATED_RATES: dict[tuple[str, str], Windows]
    PROVIDER_RATES_FETCHED: datetime
    RATE_EPOCHS: list[datetime]


def _instant(stamp: str, where: str) -> datetime:
    when = datetime.fromisoformat(stamp)
    if when.tzinfo is None:
        raise ValueError(f"{where}: {stamp!r} carries no UTC offset")
    return when


def _history(entries: list[dict], where: str) -> tuple[dict, Windows]:
    """A row's append-only history as (list rates, dated windows).

    Entries run oldest first; the first has ``from: null`` and every later
    one a strictly later ``from``. The newest entry is the list price, and
    each earlier entry is a window ending where its successor starts —
    the shape DATED_RATES has always had, so appending an entry turns the
    previous list price into a window without editing it.
    """
    rates: list[dict] = []
    starts: list[datetime] = []
    for i, entry in enumerate(entries):
        fields = set(entry) - {"from", "note"}
        if fields != set(RATE_FIELDS):
            raise ValueError(f"{where}[{i}]: fields {sorted(fields)}")
        stamp = entry["from"]
        if (stamp is None) != (i == 0):
            raise ValueError(f"{where}[{i}]: only the first entry has no 'from'")
        if stamp is not None:
            start = _instant(stamp, f"{where}[{i}]")
            if starts and start <= starts[-1]:
                raise ValueError(f"{where}[{i}]: 'from' is not after the previous entry's")
            starts.append(start)
        rates.append({f: entry[f] for f in RATE_FIELDS})
    if not rates:
        raise ValueError(f"{where}: empty history")
    return rates[-1], list(zip(starts, rates[:-1]))


def load_tables(doc: dict) -> RateTables:
    """Every rate table, derived from the parsed pricing.json, in file order."""
    model_rates: dict[str, dict] = {}
    dated_rates: dict[str, Windows] = {}
    for key, entries in doc["models"].items():
        model_rates[key], windows = _history(entries, key)
        if windows:
            dated_rates[key] = windows
    provider_rates: dict[tuple[str, str], dict] = {}
    provider_dated: dict[tuple[str, str], Windows] = {}
    for model, hosts in doc["providers"].items():
        for host, entries in hosts.items():
            provider_rates[model, host], windows = _history(entries, f"{model} via {host}")
            if windows:
                provider_dated[model, host] = windows
    return {
        "MODEL_RATES": model_rates,
        "DATED_RATES": dated_rates,
        "PROVIDER_RATES": provider_rates,
        "PROVIDER_DATED_RATES": provider_dated,
        "PROVIDER_RATES_FETCHED": _instant(doc["provider_rates_fetched"],
                                           "provider_rates_fetched"),
        # Sorted boundaries where any rate changes. Read-time aggregation
        # that re-derives rates from summed tokens must group by these, or
        # its per-component breakdown drifts from the stored per-record cost.
        "RATE_EPOCHS": sorted(
            {end for windows in dated_rates.values() for end, _ in windows}
            | {end for windows in provider_dated.values() for end, _ in windows}
        ),
    }


_TABLES = load_tables(json.loads(PRICING_JSON.read_text(encoding="utf-8")))
MODEL_RATES = _TABLES["MODEL_RATES"]
# Dated overrides per exact key: (end_exclusive_utc, rates), oldest first.
# Applied only when a timestamp is supplied and only on an EXACT key match —
# a tier fallback never inherits another model's promotional price.
DATED_RATES = _TABLES["DATED_RATES"]
# Per-provider rates, keyed by (normalised model id, provider): OpenRouter's
# provider_name as the transcript's message.provider spells it. Rows come
# from OpenRouter's endpoints API (/api/v1/models/<author>/<slug>/endpoints)
# as of PROVIDER_RATES_FETCHED, host promotional discounts already applied
# (an entry's note records one). Every endpoint lists cache_write 0, so both
# create buckets carry the input rate.
PROVIDER_RATES = _TABLES["PROVIDER_RATES"]
PROVIDER_DATED_RATES = _TABLES["PROVIDER_DATED_RATES"]
PROVIDER_RATES_FETCHED = _TABLES["PROVIDER_RATES_FETCHED"]
RATE_EPOCHS = _TABLES["RATE_EPOCHS"]

DEFAULT_RATES = MODEL_RATES["claude-opus-4-7"]

# Every rate an OpenRouter free model carries: zero. Returned for any id
# ending in ":free" or starting with "stealth/" (see _is_free).
FREE_RATES = dict.fromkeys(RATE_FIELDS, 0.00)

# The two GPT-5.6 repricing instants, named for the tests that price
# around them.
JUL30_CUT = DATED_RATES["gpt-5-6-terra"][0][0]
AUG21_CUT = DATED_RATES["gpt-5-6-sol"][0][0]

# A Codex request whose prompt exceeds this size bills the WHOLE request at
# the long-context meter: 2x input, 1.5x output. Ported from codexmeter.
# Applied per record by the caller, which is the only place that knows the
# request's prompt size.
LONG_CONTEXT_THRESHOLD = 272_000
LONG_CONTEXT_INPUT_MULT = 2.0
LONG_CONTEXT_OUTPUT_MULT = 1.5

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


def _is_free(model: str, norm: str) -> bool:
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


def _match_key(norm: str) -> str | None:
    """The LONGEST table key `norm` names, so ``claude-opus-4-1-20250805``
    is claude-opus-4-1, never claude-opus-4 — whatever the table order."""
    best = None
    for key in MODEL_RATES:
        if not norm.startswith(key) or (best and len(key) <= len(best)):
            continue
        rest = norm[len(key):]
        if rest == "" or rest[0] in "[@" or _SNAPSHOT_SUFFIX.match(rest):
            best = key
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


def _dated(key: str, ts: datetime | None) -> dict:
    return _in_window(DATED_RATES.get(key), ts, MODEL_RATES[key])


# OpenRouter's dated permaslug ("deepseek/deepseek-v4-flash-20260731") names
# the same model as its short slug ("deepseek/deepseek-v4-flash-0731").
_PERMASLUG_DATE = re.compile(r"-20\d{2}(\d{4})$")


def _provider_key(norm: str, provider: str) -> tuple[str, str] | None:
    """The PROVIDER_RATES key for a record, or None.

    Exact on the normalised id, or on its permaslug folded to the slug.
    Never MODEL_RATES' snapshot-suffix tolerance: that would read the
    permaslug as the UNDATED model, a different row at a different price.
    """
    for model in (norm, _PERMASLUG_DATE.sub(r"-\1", norm)):
        if (model, provider) in PROVIDER_RATES:
            return model, provider
    return None


def resolve(model: str, ts: datetime | None = None,
            provider: str | None = None) -> Resolution:
    """Resolve a model id to rates, reporting how confident the match is.

    `provider` is the record's serving host. A (model, provider) row wins;
    with no row, or no provider, the model alone decides.
    """
    norm = _normalise(model)
    if _is_free(model, norm):
        return Resolution(FREE_RATES, "exact", norm)
    pkey = _provider_key(norm, provider) if provider else None
    if pkey is not None:
        return Resolution(
            _in_window(PROVIDER_DATED_RATES.get(pkey), ts, PROVIDER_RATES[pkey]),
            "exact", pkey[0])
    key = _match_key(norm)
    if key is not None:
        return Resolution(_dated(key, ts), "exact", key)
    for pattern, rates in _TIER_FALLBACKS:
        if pattern.search(norm):
            return Resolution(rates, "tier")
    return Resolution(DEFAULT_RATES, "default")


def rate_for(model: str, ts: datetime | None = None,
             provider: str | None = None) -> dict:
    """Rates for a model at a point in time. Omitting ts yields list price."""
    return resolve(model, ts, provider).rates


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
    provider: str | None = None,
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

    provider is the record's serving host (OpenRouter's message.provider);
    None prices by the model alone, exactly as before the provider table.
    """
    r = rate_for(model, ts, provider)
    in_mult = LONG_CONTEXT_INPUT_MULT if long_context else 1.0
    out_mult = LONG_CONTEXT_OUTPUT_MULT if long_context else 1.0
    return (
        fresh * r["fresh"] * in_mult / 1_000_000
        + eph5 * r["create_5m"] * in_mult / 1_000_000
        + (eph1h + unsplit_create) * r["create_1h"] * in_mult / 1_000_000
        + read * r["read"] * in_mult / 1_000_000
        + output * r["output"] * out_mult / 1_000_000
    )
