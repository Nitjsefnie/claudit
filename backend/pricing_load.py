"""The pricing.json loader (SV-RATE-DATA): every rate table, checked.

Every rate lives in ``src/pricing.json``; this module reads it once at
import and refuses a rule-breaking file, naming the row. The backend's
resolution logic lives in ``backend/pricing.py``, which re-exports the
loaded tables; the browser mirror is ``src/parser.js``'s own loader.
``load_tables`` is the one entry point the refresh script and the rate
tests call on synthetic documents.
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TypedDict

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
    FEES: dict[str, dict[int, float]]
    PROVIDER_RATES: dict[tuple[str, str], dict]
    PROVIDER_DATED_RATES: dict[tuple[str, str], Windows]
    PROVIDER_FEES: dict[tuple[str, str], dict[int, float]]
    PROVIDER_STARTS: dict[tuple[str, str], datetime]
    PROVIDER_SCHEDULES: dict[tuple[str, str], dict[int, list[ScheduleWindow]]]
    PROVIDER_RATES_FETCHED: datetime
    RATE_EPOCHS: list[datetime]
    # The vendor tables (SV-RATE-DATA): the bare first-party form each
    # vendor_host-carrying tracked entry prices, and its host. The rates
    # themselves stay provider rows, keyed (tracked key, host).
    VENDOR_BARE: dict[str, str]
    VENDOR_HOSTS: dict[str, str]
    # The default estimate's rates: the merged view's claude-opus-4-7 row.
    DEFAULT_RATES: dict
    # The Codex long-context meter's membership (pricing.json's
    # long_context_models): dashed names of models-table keys or tracked
    # keys, compared against a record's normalised model id.
    LONG_CONTEXT_MODELS: frozenset[str]
    # The meter's per-model thresholds (issue #765): member key ->
    # threshold integer, overriding the meter's global default.
    LONG_CONTEXT_METERS: dict[str, int]


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


# The per-request fee a RECORDED_FEE note records (issue #469). The refresh
# (scripts/ci/refresh_prices.py fee_notes) writes one part per fee into the
# entry note, joined with "; " beside a discount note:
#   `<fee> $<amount>/request not modelled: per-request, unpriceable from
#    token counts`
# with the amount a normalized decimal (never an exponent spelling). The
# loader parses each entry's note into a per-request USD fee, so the fee is
# PRICED (folded into cost_usd) while its provenance stays in the note.
_FEE_NOTE = re.compile(
    r"^[a-z0-9_]+ \$(?P<amount>\d+(?:\.\d+)?)/request not modelled: "
    r"per-request, unpriceable from token counts$")


def _fee_of_note(note: str, at: str) -> float:
    """A note's per-request fees, summed. A part containing "/request" is
    fee-shaped and must match the documented shape in full — a fee-shaped
    part the loader cannot price refuses the file, because silently
    dropping a real cost is the failure issue #469 records; anything else
    (a discount note, a future non-fee note) parses no fee."""
    if not note:
        return 0.0
    total = 0.0
    for part in note.split("; "):
        if "/request" not in part:
            continue
        m = _FEE_NOTE.fullmatch(part)
        if m is None:
            raise ValueError(
                f"{at}: note part {part!r} names a per-request fee but is "
                "not the documented `<fee> $<amount>/request not modelled: "
                "per-request, unpriceable from token counts` shape")
        total += float(m.group("amount"))
    return total


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


def check_band(band: object, at: str) -> dict:
    """An entry's oscillating price range, checked (SV-RATE-DATA).

    A band maps each rate field it constrains to a ``[min, max]`` pair of
    finite non-negative numbers with ``min <= max``; a field it does not name
    is unconstrained. The five rate fields beside it stay the priced rates —
    the loader reads a band only to refuse a rule-breaking one. Mirrored by
    parser.js's _checkBand, and shared with scripts/ci/price_band.py so the
    refresh and this loader spell one shape.
    """
    if not isinstance(band, dict):
        raise ValueError(f"{at}: band is not a mapping of rate fields to [min, max]")
    out: dict[str, list[float]] = {}
    for field, span in band.items():
        where = f"{at}.band[{field}]"
        if field not in RATE_FIELDS:
            raise ValueError(f"{where}: not one of {list(RATE_FIELDS)}")
        if (not isinstance(span, list) or len(span) != 2
                or not all(_is_rate(value) for value in span)
                or span[0] > span[1]):
            raise ValueError(f"{where}: not a [min, max] pair of finite "
                             "non-negative numbers with min <= max")
        out[field] = [span[0], span[1]]
    return out


def _check_entry_fields(entry: dict, at: str, may_band: bool = False) -> None:
    """An entry's field set and rate values, checked."""
    fields = set(entry) - {"from", "note", "schedule", "band"}
    if fields != set(RATE_FIELDS) or "from" not in entry:
        raise ValueError(f"{at}: fields {sorted(entry)}")
    bad = [f for f in RATE_FIELDS if not _is_rate(entry[f])]
    if bad:
        raise ValueError(f"{at}: {bad} not a finite non-negative number")
    if "band" in entry:
        if not may_band:
            raise ValueError(f"{at}: only a provider row carries a band")
        check_band(entry["band"], at)


def _history(entries: list[dict], where: str, may_begin: bool = False
             ) -> tuple[dict, Windows, datetime | None,
                        dict[int, list[ScheduleWindow]], dict[int, float]]:
    """A row's append-only history as (list rates, dated windows, start,
    schedules, fees).

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
    fees: dict[int, float] = {}
    for i, entry in enumerate(entries):
        at = f"{where}[{i}]"
        _check_entry_fields(entry, at, may_begin)
        if "schedule" in entry:
            if not may_begin:
                raise ValueError(f"{at}: only a provider row carries a schedule")
            schedules[i] = _schedule(entry["schedule"], at)
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
    fees = {i: fee for i, entry in enumerate(entries)
            if (fee := _fee_of_note(entry.get("note", ""), f"{where}[{i}]"))}
    begin = entries[0]["from"] is not None
    return (rates[-1], list(zip(starts[begin:], rates[:-1])),
            starts[0] if begin else None, schedules, fees)


def _long_context_members(doc: dict) -> frozenset[str]:
    """The long-context meter's membership, checked: distinct non-empty
    strings, each naming a models-table key OR a tracked key (the meter
    rides whichever table's rate rows the id prices through, so a name
    with no row in either is a typo the loader refuses)."""
    if "long_context_models" not in doc:
        raise ValueError("pricing.json: long_context_models is missing")
    members = doc["long_context_models"]
    if (not isinstance(members, list)
            or not all(isinstance(k, str) and k for k in members)
            or len(set(members)) != len(members)):
        raise ValueError(
            "long_context_models: not a list of distinct non-empty keys")
    tracked = (doc.get("openrouter") or {}).get("models") or {}
    unknown = [k for k in members
               if k not in doc["models"] and k not in tracked]
    if unknown:
        raise ValueError(
            f"long_context_models: {unknown} name no models-table or tracked key")
    return frozenset(members)


def _long_context_meters(doc: dict) -> dict[str, int]:
    """The meter's per-model thresholds (issue #765), checked: a map of
    member keys to exactly {"threshold": N} for a positive integral N —
    an integral float spelling (200000.0) folds to the integer, so both
    loaders accept the same bytes where JSON has already collapsed the
    spelling (the browser's Number sees one value); a fractional one is
    refused. A member absent here keeps the meter's global threshold.
    The key must be a member — a threshold for a model that does not
    meter is inert data the loaders refuse."""
    meters = doc.get("long_context_meters") or {}
    if not isinstance(meters, dict):
        raise ValueError("long_context_meters: not a map of member keys "
                         "to thresholds")
    members = _long_context_members(doc)
    for key, value in meters.items():
        if key not in members:
            raise ValueError(
                f"long_context_meters: {key!r} names no long_context_models "
                "member")
        if not isinstance(value, dict) or set(value) != {"threshold"}:
            raise ValueError(
                f"long_context_meters: {key!r} is not a {{threshold: "
                "positive integer}}")
        threshold = value["threshold"]
        if (isinstance(threshold, bool)
                or not isinstance(threshold, (int, float))
                or not float(threshold).is_integer() or threshold <= 0):
            raise ValueError(
                f"long_context_meters: {key!r} is not a {{threshold: "
                "positive integer}}")
    return {k: int(v["threshold"]) for k, v in meters.items()}
# The tracked table's namespace prefixes (SV-RATE-DATA): a tracked key
# "p/<rest>" under a configured prefix names the bare first-party id
# "<rest>"; an unprefixed key is its own bare form.
_VENDOR_NAME = re.compile(r"^[a-z0-9-]+$")


def _bare_form(key: str, prefixes: list[str]) -> str:
    """A tracked key's bare form: the key with its namespace prefix
    stripped, longest prefix first; an unprefixed key is its own bare
    form."""
    for prefix in sorted(prefixes, key=len, reverse=True):
        if key.startswith(f"{prefix}/"):
            return key[len(prefix) + 1:]
    return key


def _vendor_tables(doc: dict, model_rates: dict) -> tuple[dict[str, str], dict[str, str]]:
    """The bare-id vendor tables (SV-RATE-DATA), checked: for each tracked
    entry carrying `vendor_host`, the (bare form → tracked key) match table
    and the (tracked key → host) host table. A tracked entry without
    vendor_host carries no first-party pricing and takes no part. A bare
    form two entries share, or one that is also a models-table key, is an
    ambiguity that would silently pick one price: refuse naming both rows.
    """
    openrouter = doc.get("openrouter")
    if not isinstance(openrouter, dict):
        raise ValueError("pricing.json: openrouter is missing")
    vendor = openrouter.get("vendor")
    prefixes = vendor.get("prefixes") if isinstance(vendor, dict) else None
    if (not isinstance(prefixes, list) or not prefixes
            or not all(isinstance(p, str) and p and _VENDOR_NAME.fullmatch(p)
                       for p in prefixes)
            or len(set(prefixes)) != len(prefixes)):
        raise ValueError(
            "pricing.json: openrouter.vendor.prefixes is missing or not a "
            "list of distinct lowercase [a-z0-9-] namespace strings")
    tracked = openrouter.get("models")
    if not isinstance(tracked, dict):
        raise ValueError("pricing.json: openrouter.models is missing")
    bare: dict[str, str] = {}
    hosts: dict[str, str] = {}
    for key, entry in tracked.items():
        where = f"openrouter.models[{key!r}]"
        if not isinstance(entry, dict):
            raise ValueError(f"pricing.json: {where} is not a mapping")
        if not isinstance(entry.get("id"), str) or not entry["id"]:
            raise ValueError(f"pricing.json: {where} carries no 'id'")
        host = entry.get("vendor_host")
        if host is None:
            continue
        if not isinstance(host, str) or not host:
            raise ValueError(
                f"pricing.json: {where}: vendor_host is not a non-empty string")
        form = _bare_form(key, prefixes)
        if not form:
            raise ValueError(
                f"pricing.json: {where}: bare form is empty; a tracked key "
                "may not be exactly its namespace prefix")
        if form in bare:
            raise ValueError(
                f"pricing.json: {where} and openrouter.models[{bare[form]!r}] "
                f"both carry the bare form {form!r}")
        if form in model_rates:
            raise ValueError(
                f"pricing.json: {where}: bare form {form!r} is also a "
                "models-table key; a transcript id would resolve two ways")
        bare[form] = key
        hosts[key] = host
    return bare, hosts


def _default_rates(model_rates: dict, vendor_bare: dict[str, str],
                   vendor_hosts: dict[str, str],
                   provider_rates: dict) -> dict:
    """The default estimate's rates: the merged view's claude-opus-4-7
    row — the models-table row when the key names one, else its tracked
    vendor row's list price (the migration's new location for the claude
    families). A document naming neither refuses: there is no honest
    default without one, so both loaders refuse (SV-RATE-DATA)."""
    rates = model_rates.get("claude-opus-4-7")
    if rates is None:
        tracked = vendor_bare.get("claude-opus-4-7")
        if tracked is not None:
            row = (tracked, vendor_hosts[tracked])
            rates = provider_rates.get(row)
    if rates is None:
        raise ValueError(
            "pricing.json: no claude-opus-4-7 row prices the default estimate")
    return rates


def _provider_tables(doc: dict) -> tuple[dict, dict, dict, dict, dict]:
    """The provider-row tables (rates, dated windows, fees, starts,
    schedules), one row per (model, host) the document names."""
    provider_rates: dict[tuple[str, str], dict] = {}
    provider_dated: dict[tuple[str, str], Windows] = {}
    provider_fees: dict[tuple[str, str], dict[int, float]] = {}
    provider_starts: dict[tuple[str, str], datetime] = {}
    provider_schedules: dict[tuple[str, str], dict[int, list[ScheduleWindow]]] = {}
    for model, hosts in doc["providers"].items():
        for host, entries in hosts.items():
            where = f"{model} via {host}"
            (provider_rates[model, host], windows, start,
             schedules, fees) = _history(entries, where, may_begin=True)
            if schedules:
                provider_schedules[model, host] = schedules
            if windows:
                provider_dated[model, host] = windows
            if start is not None:
                provider_starts[model, host] = start
            if fees:
                provider_fees[model, host] = fees
    return (provider_rates, provider_dated, provider_fees, provider_starts,
            provider_schedules)


def load_tables(doc: dict) -> RateTables:
    """Every rate table, derived from the parsed pricing.json, in file order."""
    model_rates: dict[str, dict] = {}
    dated_rates: dict[str, Windows] = {}
    model_fees: dict[str, dict[int, float]] = {}
    for key, entries in doc["models"].items():
        model_rates[key], windows, _, _, fees = _history(entries, key)
        if windows:
            dated_rates[key] = windows
        if fees:
            model_fees[key] = fees
    (provider_rates, provider_dated, provider_fees, provider_starts,
     provider_schedules) = _provider_tables(doc)
    vendor_bare, vendor_hosts = _vendor_tables(doc, model_rates)
    return {
        "MODEL_RATES": model_rates,
        "DATED_RATES": dated_rates,
        "FEES": model_fees,
        "PROVIDER_RATES": provider_rates,
        "PROVIDER_DATED_RATES": provider_dated,
        "PROVIDER_FEES": provider_fees,
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
        "VENDOR_BARE": vendor_bare,
        "VENDOR_HOSTS": vendor_hosts,
        "DEFAULT_RATES": _default_rates(model_rates, vendor_bare,
                                        vendor_hosts, provider_rates),
        "LONG_CONTEXT_MODELS": _long_context_members(doc),
        "LONG_CONTEXT_METERS": _long_context_meters(doc),
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
# Per-request fees (issue #469), per history entry that carries one, keyed
# like PROVIDER_SCHEDULES: windows[i] is entry i, and the newest entry is
# index len(windows). Parsed from the entry's RECORDED_FEE note; an entry
# with no fee note is absent.
FEES = _TABLES["FEES"]
PROVIDER_FEES = _TABLES["PROVIDER_FEES"]
PROVIDER_RATES_FETCHED = _TABLES["PROVIDER_RATES_FETCHED"]
RATE_EPOCHS = _TABLES["RATE_EPOCHS"]
# The Codex long-context meter's membership: dashed names of models-table
# keys or tracked keys, the shape SV-RATE-ESTIMATES' comparison needs.
# pricing re-exports it.
LONG_CONTEXT_MODELS = _TABLES["LONG_CONTEXT_MODELS"]
# The meter's per-model thresholds (issue #765): a member key's
# {"threshold": N} override of the global default. pricing re-exports it.
LONG_CONTEXT_METERS = _TABLES["LONG_CONTEXT_METERS"]
# The vendor tables (SV-RATE-DATA): the bare first-party form each
# vendor_host-carrying tracked entry prices, and its host. pricing
# re-exports both.
VENDOR_BARE = _TABLES["VENDOR_BARE"]
VENDOR_HOSTS = _TABLES["VENDOR_HOSTS"]


DEFAULT_RATES = _TABLES["DEFAULT_RATES"]
