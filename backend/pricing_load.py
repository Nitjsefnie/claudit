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
    PROVIDER_RATES: dict[tuple[str, str], dict]
    PROVIDER_DATED_RATES: dict[tuple[str, str], Windows]
    PROVIDER_STARTS: dict[tuple[str, str], datetime]
    PROVIDER_SCHEDULES: dict[tuple[str, str], dict[int, list[ScheduleWindow]]]
    PROVIDER_RATES_FETCHED: datetime
    RATE_EPOCHS: list[datetime]
    # The Codex long-context meter's membership (pricing.json's
    # long_context_models): dashed keys of the models table, compared
    # against a record's normalised model id.
    LONG_CONTEXT_MODELS: frozenset[str]


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
    members = doc.get("long_context_models", [])
    if (not isinstance(members, list)
            or not all(isinstance(k, str) and k for k in members)
            or len(set(members)) != len(members)):
        raise ValueError(
            "long_context_models: not a list of distinct non-empty keys")
    unknown = [k for k in members if k not in doc["models"]]
    if unknown:
        raise ValueError(
            f"long_context_models: {unknown} name no models-table key")
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
        "LONG_CONTEXT_MODELS": frozenset(members),
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
# The Codex long-context meter's membership: dashed models-table keys, the
# shape SV-RATE-ESTIMATES' comparison needs. pricing re-exports it.
LONG_CONTEXT_MODELS = _TABLES["LONG_CONTEXT_MODELS"]

DEFAULT_RATES = MODEL_RATES["claude-opus-4-7"]
