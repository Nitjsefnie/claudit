"""Validate src/pricing.json once at import (SV-RATE-DATA).

pricing re-exports tables and resolves rates; src/pricing-loader.js mirrors
validation. load_tables accepts synthetic refresh/test documents and names
invalid rows in refusals."""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TypedDict

from backend.meter_tables import _long_context_meters
from backend.pricing_document import (RATE_FIELDS, check_compact_shape,
                                      check_dual_pin_tags, effective_rates,
                                      expand_pricing_doc)
from backend.pricing_rows import decode_pricing_doc, flatten_provider_history

# Every rate lives in src/pricing.json (SV-RATE-DATA); this module holds
# resolution logic only. The file sits under src/ because the browser's
# parser.js reads the same file from /src.
PRICING_JSON = Path(__file__).resolve().parent.parent / "src" / "pricing.json"

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
    PROVIDER_CONTEXT_LENGTHS: dict[tuple[str, str], int]
    TRACKED_MODELS: dict[str, dict]
    PROVIDER_DATED_RATES: dict[tuple[str, str], Windows]
    PROVIDER_STARTS: dict[tuple[str, str], datetime]
    PROVIDER_SCHEDULES: dict[tuple[str, str], dict[int, list[ScheduleWindow]]]
    PROVIDER_RATES_FETCHED: datetime
    RATE_EPOCHS: list[datetime]
    # The vendor tables: derivable bare forms map to tracked keys; only
    # vendor_host-carrying entries name first-party hosts. Rates stay
    # provider rows, keyed (tracked key, host).
    VENDOR_BARE: dict[str, str]
    VENDOR_PREFIXES: list[str]
    VENDOR_HOSTS: dict[str, str]
    # The default estimate's rates: the merged view's claude-opus-4-7 row.
    DEFAULT_RATES: dict
    # Derived membership: every key in LONG_CONTEXT_METERS is a member.
    LONG_CONTEXT_MODELS: frozenset[str]
    # The meter's per-model entries: member key -> threshold
    # and optional input/output factors, overriding global defaults.
    LONG_CONTEXT_METERS: dict[str, dict]


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
        required = set(RATE_FIELDS) - {"create_5m", "create_1h"}
        if (not isinstance(rates, dict) or not required <= set(rates)
                or set(rates) - set(RATE_FIELDS)
                or not all(_is_rate(value)
                           for value in effective_rates(rates).values())):
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
                    effective_rates(rates)))
    return out


def _check_entry_fields(entry: dict, at: str, provider: bool = False) -> None:
    """An entry's field set and rate values, checked."""
    fields = set(entry) - {"from", "note", "schedule", "context_length"}
    if "context_length" in entry:
        _check_context_length(entry, at)
        if not provider:
            raise ValueError(f"{at}: only a provider entry carries context_length")
    has_search_rate = "web_search" in fields
    if has_search_rate:
        fields.remove("web_search")
    rate_fields = fields - ({"web_search"} if has_search_rate else set())
    required = set(RATE_FIELDS) - {"create_5m", "create_1h"}
    if (not required <= rate_fields or rate_fields - set(RATE_FIELDS)
            or (has_search_rate and not provider)):
        raise ValueError(f"{at}: fields {sorted(entry)}")
    bad = [f for f, value in effective_rates(entry).items()
           if not _is_rate(value)]
    if has_search_rate and not _is_rate(entry["web_search"]):
        bad.append("web_search")
    if bad:
        raise ValueError(f"{at}: {bad} not a finite non-negative number")


def _check_context_length(entry: dict, at: str) -> None:
    if "context_length" in entry and (
            not isinstance(entry["context_length"], int)
            or isinstance(entry["context_length"], bool)
            or not 0 < entry["context_length"] <= 9007199254740991):
        raise ValueError(f"{at}: context_length is not a positive integer")


def _history(entries: list[dict], where: str, may_begin: bool = False,
             fetched: datetime | None = None
             ) -> tuple[dict, Windows, datetime | None,
                        dict[int, list[ScheduleWindow]]]:
    """A row's append-only history as (list rates, dated windows, start,
    schedules).

    Entries run oldest first; every ``from`` after the first is strictly
    later than the one before. The newest entry is the list price, and
    each earlier entry is a window ending where its successor starts —
    the shape DATED_RATES has always had, so appending an entry turns the
    previous list price into a window without editing it.

    The first entry's ``from`` is null — the row covers all of time —
    unless `may_begin`, where it may instead name the instant the row
    begins; start is that instant, else None. `fetched` bounds every
    dated entry; a predecessor-equal entry is refused.
    Mirrored by _checkHistory.
    """
    entries, owners = (flatten_provider_history(entries, where, _instant) if may_begin
                       else (entries, list(range(len(entries)))))
    starts: list[datetime] = []
    schedules: dict[int, list[ScheduleWindow]] = {}
    for i, entry in enumerate(entries):
        at = f"{where}[{owners[i]}]"
        _check_entry_fields(entry, at, may_begin)
        if "schedule" in entry:
            if not may_begin:
                raise ValueError(f"{at}: only a provider row carries a schedule")
            schedules[i] = _schedule(entry["schedule"], at)
        if not isinstance(entry.get("note", ""), str):
            raise ValueError(f"{at}: 'note' is not a string")
        stamp = entry.get("from")
        if stamp is None and i > 0:
            raise ValueError(f"{at}: only the first entry has no 'from'")
        if stamp is not None and i == 0 and not may_begin:
            raise ValueError(f"{at}: this row cannot begin at a time; 'from' must be null")
        if stamp is not None:
            start = _instant(stamp, at)
            if starts and start <= starts[-1]:
                raise ValueError(f"{at}: 'from' is not after the previous entry's")
            if fetched is not None and start > fetched:
                raise ValueError(
                    f"{at}: 'from' {stamp!r} is after provider_rates_fetched "
                    f"{fetched.isoformat().replace('+00:00', 'Z')!r}")
            starts.append(start)
        if i > 0 and owners[i] != owners[i - 1] and (
                {k: v for k, v in entry.items() if k != "from"}
                == {k: v for k, v in entries[i - 1].items()
                    if k != "from"}):
            raise ValueError(f"{at}: equal to the previous entry except 'from'")
    if not entries:
        raise ValueError(f"{where}: empty history")
    return _priced_history(entries, owners, schedules, where)


def _priced_history(entries: list[dict], owners: list[int], schedules: dict, where: str
                    ) -> tuple[dict, Windows, datetime | None, dict]:
    """Keep context metadata moves outside rate windows and their schedules."""
    priced = []
    for i, entry in enumerate(entries):
        if i and owners[i] != owners[i - 1] and (
                entry.get("context_length") != entries[i - 1].get("context_length")
                and effective_rates(entry) == effective_rates(entries[i - 1])
                and all(entry.get(field) == entries[i - 1].get(field)
                        for field in ("web_search", "note", "schedule"))):
            continue
        priced.append((i, entry))
    rates = [dict(effective_rates(entry), **(
        {"web_search": entry["web_search"]} if "web_search" in entry else {}))
        for _, entry in priced]
    starts = [_instant(entry["from"], where) for _, entry in priced
              if entry.get("from") is not None]
    begin = entries[0].get("from") is not None
    return (rates[-1], list(zip(starts[begin:], rates[:-1])),
            starts[0] if begin else None,
            {j: schedules[i] for j, (i, _) in enumerate(priced) if i in schedules})


# The tracked table's namespace prefixes (SV-RATE-DATA): a tracked key
# "p/<rest>" under a configured prefix names the bare first-party id
# "<rest>"; an unprefixed key is its own bare form.
_VENDOR_NAME = re.compile(r"^[a-z0-9-]+$")


def is_vendor_namespace(value: object) -> bool:
    """Whether a value is a lowercase OpenRouter namespace string."""
    return isinstance(value, str) and _VENDOR_NAME.fullmatch(value) is not None


def bare_form(key: str, prefixes: list[str]) -> str:
    """Strip the longest tracked namespace prefix; leave other keys unchanged."""
    for prefix in sorted(prefixes, key=len, reverse=True):
        if key.startswith(f"{prefix}/"):
            return key[len(prefix) + 1:]
    return key


def _vendor_tables(doc: dict, model_rates: dict) -> tuple[dict[str, str], dict[str, str]]:
    """Derivable bare forms map to tracked keys, independently of hosts.
    Only vendor_host-carrying entries join the first-party host table.
    Duplicate bare forms and models-table collisions refuse by name.
    """
    openrouter = doc.get("openrouter")
    if not isinstance(openrouter, dict):
        raise ValueError("pricing.json: openrouter is missing")
    vendor = openrouter.get("vendor")
    prefixes = vendor.get("prefixes") if isinstance(vendor, dict) else None
    if (not isinstance(prefixes, list) or not prefixes
            or not all(is_vendor_namespace(p) for p in prefixes)
            or len(set(prefixes)) != len(prefixes)):
        raise ValueError(
            "pricing.json: openrouter.vendor.prefixes is missing or not a "
            "list of distinct lowercase [a-z0-9-] namespace strings")
    tracked = openrouter.get("models")
    if not isinstance(tracked, dict):
        raise ValueError("pricing.json: openrouter.models is missing")
    # A key pinned in BOTH resolve tables must agree on its tag.
    check_dual_pin_tags(openrouter)
    bare: dict[str, str] = {}
    hosts: dict[str, str] = {}
    for key, entry in tracked.items():
        where = f"openrouter.models[{key!r}]"
        if not isinstance(entry, dict):
            raise ValueError(f"pricing.json: {where} is not a mapping")
        if not isinstance(entry.get("id"), str) or not entry["id"]:
            raise ValueError(f"pricing.json: {where} carries no 'id'")
        _check_context_length(entry, where)
        host = entry.get("vendor_host")
        if host is not None and (not isinstance(host, str) or not host):
            raise ValueError(
                f"pricing.json: {where}: vendor_host is not a non-empty string")
        form = bare_form(key, prefixes)
        if host is not None:
            hosts[key] = host
        if "/" in form:
            continue
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
    return bare, hosts


def _default_rates(model_rates: dict, vendor_bare: dict[str, str],
                   vendor_hosts: dict[str, str],
                   provider_rates: dict) -> dict:
    """Default estimate from the merged view's claude-opus-4-7 list rate.

    Prefer its models-table row, else its tracked vendor row. Refuse a
    document with neither: both loaders require a real default row
    (SV-RATE-DATA)."""
    rates = model_rates.get("claude-opus-4-7")
    if rates is None:
        tracked = vendor_bare.get("claude-opus-4-7")
        if tracked in vendor_hosts:
            row = (tracked, vendor_hosts[tracked])
            rates = provider_rates.get(row)
    if rates is None:
        raise ValueError(
            "pricing.json: no claude-opus-4-7 row prices the default estimate")
    return rates


def _provider_tables(doc: dict, fetched: datetime) -> tuple[dict, dict, dict, dict]:
    """The provider-row tables (rates, dated windows, starts, schedules),
    one row per (model, host) the document names."""
    provider_rates: dict[tuple[str, str], dict] = {}
    provider_dated: dict[tuple[str, str], Windows] = {}
    provider_starts: dict[tuple[str, str], datetime] = {}
    provider_schedules: dict[tuple[str, str], dict[int, list[ScheduleWindow]]] = {}
    for model, hosts in doc["providers"].items():
        for host, entries in hosts.items():
            where = f"{model} via {host}"
            (provider_rates[model, host], windows, start,
             schedules) = _history(entries, where, may_begin=True,
                                   fetched=fetched)
            if schedules:
                provider_schedules[model, host] = schedules
            if windows:
                provider_dated[model, host] = windows
            if start is not None:
                provider_starts[model, host] = start
    return (provider_rates, provider_dated, provider_starts,
            provider_schedules)


def _stored_document(raw: dict) -> dict:
    """Decode once, check storage, then expand before shared validation."""
    decoded = decode_pricing_doc(raw)
    check_compact_shape(raw, decoded)
    return expand_pricing_doc(decoded)


def load_tables(doc: dict) -> RateTables:
    """Every rate table, derived from the parsed pricing.json, in file order."""
    if "_format" in doc:
        return load_tables(_stored_document(doc))
    fetched = _instant(doc["provider_rates_fetched"],
                       "provider_rates_fetched")
    model_rates: dict[str, dict] = {}
    dated_rates: dict[str, Windows] = {}
    for key, entries in doc["models"].items():
        if not isinstance(key, str) or not key:
            raise ValueError(
                f"pricing.json: models key {key!r} is not a non-empty string")
        model_rates[key], windows, _, _ = _history(entries, key,
                                                   fetched=fetched)
        if windows:
            dated_rates[key] = windows
    (provider_rates, provider_dated, provider_starts,
     provider_schedules) = _provider_tables(doc, fetched)
    vendor_bare, vendor_hosts = _vendor_tables(doc, model_rates)
    long_context_meters = _long_context_meters(doc)
    return {
        "MODEL_RATES": model_rates,
        "DATED_RATES": dated_rates,
        "PROVIDER_RATES": provider_rates,
        "PROVIDER_CONTEXT_LENGTHS": {
            (model, host): newest["context_length"]
            for model, hosts in doc["providers"].items()
            for host, entries in hosts.items()
            for newest in [flatten_provider_history(
                entries, f"{model} via {host}", _instant)[0][-1]]
            if "context_length" in newest},
        "TRACKED_MODELS": doc["openrouter"]["models"],
        "PROVIDER_DATED_RATES": provider_dated,
        "PROVIDER_STARTS": provider_starts,
        "PROVIDER_SCHEDULES": provider_schedules,
        "PROVIDER_RATES_FETCHED": fetched,
        # Sorted boundaries where any rate changes. Read-time aggregation
        # that re-derives rates from summed tokens must group by these, or
        # its per-component breakdown drifts from the stored per-record cost.
        "RATE_EPOCHS": sorted(
            {end for windows in dated_rates.values() for end, _ in windows}
            | {end for windows in provider_dated.values() for end, _ in windows}
            | set(provider_starts.values())
        ),
        "VENDOR_BARE": vendor_bare,
        "VENDOR_PREFIXES": list(doc["openrouter"]["vendor"]["prefixes"]),
        "VENDOR_HOSTS": vendor_hosts,
        "DEFAULT_RATES": _default_rates(model_rates, vendor_bare,
                                        vendor_hosts, provider_rates),
        "LONG_CONTEXT_MODELS": frozenset(long_context_meters),
        "LONG_CONTEXT_METERS": long_context_meters,
    }


# The raw file at import is the storage boundary, checked for stored shape
# first; load_tables stays shape-agnostic for every other caller.
_RAW = json.loads(PRICING_JSON.read_text(encoding="utf-8"))
_TABLES = load_tables(_stored_document(_RAW))
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
PROVIDER_CONTEXT_LENGTHS = _TABLES["PROVIDER_CONTEXT_LENGTHS"]
TRACKED_MODELS = _TABLES["TRACKED_MODELS"]
PROVIDER_DATED_RATES = _TABLES["PROVIDER_DATED_RATES"]
# Provider-row start: earlier requests from that host use model rates.
PROVIDER_STARTS = _TABLES["PROVIDER_STARTS"]
# Weekly UTC schedules, per provider row, by the index of the history entry
# that carries one. A schedule's windows are not rate epochs: see
# SV-RATE-DATA for how the read-time fold treats a scheduled row.
PROVIDER_SCHEDULES = _TABLES["PROVIDER_SCHEDULES"]
PROVIDER_RATES_FETCHED = _TABLES["PROVIDER_RATES_FETCHED"]
RATE_EPOCHS = _TABLES["RATE_EPOCHS"]
# The Codex long-context meter's membership: dashed names of models-table
# keys or tracked keys, the shape SV-RATE-ESTIMATES' comparison needs.
# pricing re-exports it.
LONG_CONTEXT_MODELS = _TABLES["LONG_CONTEXT_MODELS"]
# The meter's per-model entries: a member key's threshold
# and optional factors, each overriding its global default. pricing
# re-exports them.
LONG_CONTEXT_METERS = _TABLES["LONG_CONTEXT_METERS"]
# The vendor tables (SV-RATE-DATA): bare first-party forms, the configured
# namespaces used to fold transcript ids, and the row host. pricing
# re-exports these loader outputs.
VENDOR_BARE = _TABLES["VENDOR_BARE"]
VENDOR_PREFIXES = _TABLES["VENDOR_PREFIXES"]
VENDOR_HOSTS = _TABLES["VENDOR_HOSTS"]


DEFAULT_RATES = _TABLES["DEFAULT_RATES"]
