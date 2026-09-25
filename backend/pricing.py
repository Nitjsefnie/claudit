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
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TypedDict

UTC = timezone.utc


# Every rate lives in src/pricing.json (SV-RATE-DATA); this module holds
# resolution logic only. The file sits under src/ because the browser's
# parser.js reads the same file from /src.
PRICING_JSON = Path(__file__).resolve().parent.parent / "src" / "pricing.json"
RATE_FIELDS = ("fresh", "create_5m", "create_1h", "read", "output")

Windows = list[tuple[datetime, dict]]


# One window of a weekly UTC schedule: (days or None for every day, start
# HHMM or None for the whole day, end HHMM, rates). See _schedule.
ScheduleWindow = tuple[frozenset[str] | None, int | None, int | None, dict]
_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


class RateTables(TypedDict):
    """load_tables' result, keyed by the module attribute each value binds."""
    MODEL_RATES: dict[str, dict]
    DATED_RATES: dict[str, Windows]
    PROVIDER_RATES: dict[tuple[str, str], dict]
    PROVIDER_DATED_RATES: dict[tuple[str, str], Windows]
    PROVIDER_STARTS: dict[tuple[str, str], datetime]
    PROVIDER_SCHEDULES: dict[tuple[str, str], dict[int, list[ScheduleWindow]]]
    PROVIDER_RATES_FETCHED: datetime
    RATE_EPOCHS: list[datetime]


# The one timestamp spelling both loaders accept: whole seconds and an
# explicit offset, every field in range (year 1-9999, a real day of that
# month, hour 0-23, minute and second 0-59, offset under 24:00 with minutes
# 0-59). The ranges are enforced by the datetime CONSTRUCTOR, never left to
# fromisoformat: Python 3.14 accepts the ISO 24:00:00 spelling and rolls it
# over to the next midnight where 3.13 raised, and src/parser.js checks the
# same fields itself rather than trusting Date.parse, so the Python loader
# must not lean on the interpreter's whim either way.
_INSTANT = re.compile(
    r"(?P<y>[0-9]{4})-(?P<mo>[0-9]{2})-(?P<d>[0-9]{2})"
    r"T(?P<h>[0-9]{2}):(?P<mi>[0-9]{2}):(?P<s>[0-9]{2})"
    r"(?:Z|(?P<sign>[+-])(?P<oh>[0-9]{2}):(?P<om>[0-9]{2}))")


def _instant(stamp: object, where: str) -> datetime:
    m = _INSTANT.fullmatch(stamp) if isinstance(stamp, str) else None
    # An offset minute of 60+ would normalize through timedelta (14:60
    # would silently read as 15:00); the browser refuses it, so refuse it.
    if m and int(m["om"] or 0) < 60:
        try:
            delta = timedelta(0)
            if m["sign"]:
                delta = timedelta(hours=int(m["oh"]), minutes=int(m["om"]))
                if m["sign"] == "-":
                    delta = -delta
            return datetime(int(m["y"]), int(m["mo"]), int(m["d"]),
                            int(m["h"]), int(m["mi"]), int(m["s"]),
                            tzinfo=timezone(delta))
        except ValueError:
            pass
    raise ValueError(f"{where}: {stamp!r} is not YYYY-MM-DDTHH:MM:SS with Z or ±HH:MM")


def _is_rate(value: object) -> bool:
    """A finite, non-negative number; a bool is not one."""
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


def _hhmm(value: object, at: str) -> int:
    if (isinstance(value, int) and not isinstance(value, bool)
            and 0 <= value <= 2359 and value % 100 < 60):
        return value
    raise ValueError(f"{at}: {value!r} is not an HHMM time from 0 to 2359")


def _schedule(schedule: object, at: str) -> list[ScheduleWindow]:
    """A provider entry's weekly UTC schedule, checked. Mirrored by
    parser.js's _checkSchedule.

    Each window is {days?, start?, end?, rates}: `days` a list of distinct
    lowercase weekday names (absent: every day), `start`/`end` HHMM times,
    both or neither (absent: the whole day), end-exclusive and wrapping
    past midnight when start > end.
    """
    if not isinstance(schedule, list) or not schedule:
        raise ValueError(f"{at}: schedule is not a non-empty list of windows")
    out: list[ScheduleWindow] = []
    for j, window in enumerate(schedule):
        w = f"{at}.schedule[{j}]"
        if (not isinstance(window, dict) or "rates" not in window
                or set(window) - {"days", "start", "end", "rates"}):
            raise ValueError(f"{w}: a window is {{days?, start?, end?, rates}}")
        rates = window["rates"]
        if (not isinstance(rates, dict) or set(rates) != set(RATE_FIELDS)
                or not all(_is_rate(rates[f]) for f in RATE_FIELDS)):
            raise ValueError(f"{w}: rates are not the five finite non-negative rates")
        days = window.get("days")
        if days is not None and not (
                isinstance(days, list) and days and all(d in _DAYS for d in days)
                and len(set(days)) == len(days)):
            raise ValueError(f"{w}: days {days!r} are not distinct weekday names")
        if ("start" in window) != ("end" in window):
            raise ValueError(f"{w}: start and end come together")
        start = _hhmm(window["start"], w) if "start" in window else None
        end = _hhmm(window["end"], w) if "end" in window else None
        if start is not None and start == end:
            raise ValueError(f"{w}: start equals end")
        out.append((frozenset(days) if days else None, start, end,
                    {f: rates[f] for f in RATE_FIELDS}))
    return out


def _history(entries: list[dict], where: str, may_begin: bool = False
             ) -> tuple[dict, Windows, datetime | None, dict[int, list[ScheduleWindow]]]:
    """A row's append-only history as (list rates, dated windows, start).

    Entries run oldest first; every ``from`` after the first is strictly
    later than the one before. The newest entry is the list price, and
    each earlier entry is a window ending where its successor starts —
    the shape DATED_RATES has always had, so appending an entry turns the
    previous list price into a window without editing it.

    The first entry's ``from`` is null — the row covers all of time —
    unless `may_begin`, where it may instead name the instant the row
    begins; start is that instant, else None. Mirrored by parser.js's
    _checkHistory.
    """
    rates: list[dict] = []
    starts: list[datetime] = []
    schedules: dict[int, list[ScheduleWindow]] = {}
    for i, entry in enumerate(entries):
        at = f"{where}[{i}]"
        fields = set(entry) - {"from", "note", "schedule"}
        if fields != set(RATE_FIELDS) or "from" not in entry:
            raise ValueError(f"{at}: fields {sorted(entry)}")
        if "schedule" in entry:
            if not may_begin:
                raise ValueError(f"{at}: only a provider row carries a schedule")
            schedules[i] = _schedule(entry["schedule"], at)
        bad = [f for f in RATE_FIELDS if not _is_rate(entry[f])]
        if bad:
            raise ValueError(f"{at}: {bad} not a finite non-negative number")
        if not isinstance(entry.get("note", ""), str):
            raise ValueError(f"{at}: 'note' is not a string")
        stamp = entry["from"]
        if stamp is None and i > 0:
            raise ValueError(f"{at}: only the first entry has no 'from'")
        if stamp is not None and i == 0 and not may_begin:
            raise ValueError(f"{at}: this row cannot begin at a time; 'from' must be null")
        if stamp is not None:
            start = _instant(stamp, at)
            if starts and start <= starts[-1]:
                raise ValueError(f"{at}: 'from' is not after the previous entry's")
            starts.append(start)
        rates.append({f: entry[f] for f in RATE_FIELDS})
    if not rates:
        raise ValueError(f"{where}: empty history")
    begin = entries[0]["from"] is not None
    return (rates[-1], list(zip(starts[begin:], rates[:-1])),
            starts[0] if begin else None, schedules)


def load_tables(doc: dict) -> RateTables:
    """Every rate table, derived from the parsed pricing.json, in file order."""
    model_rates: dict[str, dict] = {}
    dated_rates: dict[str, Windows] = {}
    for key, entries in doc["models"].items():
        model_rates[key], windows, _, _ = _history(entries, key)
        if windows:
            dated_rates[key] = windows
    provider_rates: dict[tuple[str, str], dict] = {}
    provider_dated: dict[tuple[str, str], Windows] = {}
    provider_starts: dict[tuple[str, str], datetime] = {}
    provider_schedules: dict[tuple[str, str], dict[int, list[ScheduleWindow]]] = {}
    for model, hosts in doc["providers"].items():
        for host, entries in hosts.items():
            provider_rates[model, host], windows, start, schedules = _history(
                entries, f"{model} via {host}", may_begin=True)
            if schedules:
                provider_schedules[model, host] = schedules
            if windows:
                provider_dated[model, host] = windows
            if start is not None:
                provider_starts[model, host] = start
    return {
        "MODEL_RATES": model_rates,
        "DATED_RATES": dated_rates,
        "PROVIDER_RATES": provider_rates,
        "PROVIDER_DATED_RATES": provider_dated,
        "PROVIDER_STARTS": provider_starts,
        "PROVIDER_SCHEDULES": provider_schedules,
        "PROVIDER_RATES_FETCHED": _instant(doc["provider_rates_fetched"],
                                           "provider_rates_fetched"),
        # Sorted boundaries where any rate changes. Read-time aggregation
        # that re-derives rates from summed tokens must group by these, or
        # its per-component breakdown drifts from the stored per-record cost.
        "RATE_EPOCHS": sorted(
            {end for windows in dated_rates.values() for end, _ in windows}
            | {end for windows in provider_dated.values() for end, _ in windows}
            | set(provider_starts.values())
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
# (an entry's note records one); both create buckets carry the listed cache
# write price when it is nonzero, the input rate otherwise.
PROVIDER_RATES = _TABLES["PROVIDER_RATES"]
PROVIDER_DATED_RATES = _TABLES["PROVIDER_DATED_RATES"]
# The instant a provider row first applies, for a host first seen after the
# table was seeded; before it, a record from that host prices by the model.
PROVIDER_STARTS = _TABLES["PROVIDER_STARTS"]
# Weekly UTC schedules, per provider row, by the index of the history entry
# that carries one. A schedule's windows are not rate epochs: see
# SV-RATE-DATA for how the read-time fold treats a scheduled row.
PROVIDER_SCHEDULES = _TABLES["PROVIDER_SCHEDULES"]
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
    # True when the rates came from a weekly schedule's window or default
    # for this record's own time: a fold re-deriving cost at one
    # representative time cannot reproduce them (SV-RATE-DATA).
    scheduled: bool = False

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


def resolve(model: str, ts: datetime | None = None,
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
        return Resolution(rates, "exact", pkey[0], scheduled)
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
