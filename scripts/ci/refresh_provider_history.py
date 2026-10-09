"""Append token-log and search-rate changes to provider histories."""
from __future__ import annotations

import copy
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

import refresh_alternation
from refresh_selection import Listing
from backend import pricing
from backend.pricing_document import effective_rates

RATE_FIELDS = pricing.RATE_FIELDS


@dataclass
class Move:
    """One provider row the run appends to (old is None for a new host)."""
    model: str
    host: str
    old: dict | None
    new: Listing
    entries_appended: int = 1
    source: str = "sampled"
    # The `fresh` rate of the entry the run appended, when it is not the
    # listing's — a band entry prices the window mean, not the listing.
    entry_fresh: float | None = None


def _entry(stamp: str, listing: Listing) -> dict:
    entry = {"from": stamp, **listing.rates}
    if entry.get("web_search") == 0:
        entry.pop("web_search")
    if listing.discount:
        entry["note"] = _discount_note(listing.discount)
    if listing.schedule:
        entry["schedule"] = listing.schedule
    return entry


def _discount_note(discount: Decimal) -> str:
    return f"{format((discount * 100).normalize(), 'f')}% off"


def _append(model: str, hosts: dict, rows: dict[str, Listing], stamp: str,
            at: datetime, notices: list[str]) -> list[Move]:
    """Append each moved price (a rate or the schedule, compared as a
    whole) to its row, and start a row for each new host, in place; the
    moves made, with an alternating price's notice appended to `notices`
    instead of an entry to the row (SV-RATE-REFRESH). A host whose newest
    entry is dated at or after the detection instant is left untouched with
    a notice: appending after it would refuse the file, and one host's
    damage never blocks the other hosts (SV-RATE-REFRESH)."""
    moves = []
    for host, listing in rows.items():
        history = hosts.get(host)
        if history is None:
            hosts[host] = [_entry(stamp, listing)]
            moves.append(Move(model, host, None, listing))
            continue
        newest = history[-1]
        if (all(effective_rates(newest)[f] == listing.rates[f]
                for f in RATE_FIELDS)
                and newest.get("web_search", 0.0)
                == listing.rates.get("web_search", 0.0)
                and newest.get("schedule") == listing.schedule):
            continue
        newest_from = newest.get("from")
        if newest_from is not None and _price_instant(
                newest_from, f"{model} via {host}") >= at:
            notices.append(
                f"{model} via {host}: the stored newest entry is dated {newest_from}, "
                "not before the detection instant; the host was left untouched")
            continue
        notice = refresh_alternation.notice(
            history, listing.rates, listing.schedule, newest, at,
            f"{model} via {host}")
        if notice:
            notices.append(notice)
            continue
        history.append(_entry(stamp, listing))
        moves.append(Move(model, host, newest, listing))
    return moves


def _price_instant(stamp: object, where: str) -> datetime:
    """Parse an entry timestamp with the pricing loader's rules."""
    return pricing._instant(stamp, where)  # pylint: disable=protected-access


def _new_log_states(model: str, host: str, entries: list[dict],
                    newest: dict) -> list[dict]:
    """The log states after the stored row's newest entry, each one a change
    from the level before it.

    The comparison starts at the newest entry's own rates. On a banded row
    those are the band's time-weighted mean, which no listed state equals, so
    the first candidate is never mistaken for the level already in force —
    and a state that did equal it lies inside the band anyway.
    """
    where = f"{model} via {host}"
    newest_from = newest["from"]
    newest_at = (_price_instant(newest_from, where)
                 if newest_from is not None else None)
    previous = effective_rates(newest)
    additions = []
    for entry in entries:
        entry_at = _price_instant(entry["from"], where)
        if newest_at is not None and entry_at <= newest_at:
            continue
        rates = effective_rates(entry)
        if rates == previous:
            continue
        additions.append(copy.deepcopy(entry))
        previous = rates
    return additions


def _set_search_rate(entry: dict, rate: float) -> None:
    """Set the optional search rate without writing a zero-valued field."""
    if rate:
        entry["web_search"] = rate
    else:
        entry.pop("web_search", None)


# The history floor (SV-RATE-REFRESH): the account's OpenRouter-lane
# records begin here (ormeter's openrouter bucket's oldest record,
# 2026-09-23T21:13:54Z, verified 2026-10-08), and provider rows price only
# such records. Log states dated before the floor price nothing the meters
# hold, so a first-seen row's import starts at the last state in force AT
# the floor and the dropped levels are never imported.
HISTORY_FLOOR = datetime(2026, 9, 23, tzinfo=timezone.utc)


def _floored_log(model: str, host: str, entries: list[dict]) -> list[dict]:
    """A first-seen log-backed host's importable states: everything from
    the last state in force at the history floor onward. The kept
    pre-floor state carries the level the floor's records priced through
    (a record in its window without it would fall back to the model
    row), and states strictly before it price no record any meter holds.
    States dated after the floor are all kept, whatever their age."""
    ats = [_price_instant(entry["from"], f"{model} via {host}")
           for entry in entries]
    kept = bisect_right(ats, HISTORY_FLOOR)
    return entries if kept == 0 else entries[kept - 1:]
