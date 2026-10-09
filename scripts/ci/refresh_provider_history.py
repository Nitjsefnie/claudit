"""Append token-log and search-rate changes to provider histories."""
from __future__ import annotations

import copy
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

import price_band
import refresh_alternation
from refresh_selection import Listing
from backend import pricing

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
        if (all(newest[f] == listing.rates[f] for f in RATE_FIELDS)
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
    previous = {field: newest[field] for field in RATE_FIELDS}
    additions = []
    for entry in entries:
        entry_at = _price_instant(entry["from"], where)
        if newest_at is not None and entry_at <= newest_at:
            continue
        rates = {field: entry[field] for field in RATE_FIELDS}
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


# pylint: disable=too-many-locals,too-many-branches,too-many-statements
def _append_logged(model: str, hosts: dict, host: str, listing: Listing,
                   entries: list[dict], at: datetime,
                   deferred_notices: list[str] | None = None) -> Move | None:
    """Append every new log state after the stored row without rewriting it,
    forming or re-forming the row's band as the window decides (issues
    #663, #664, #665; SV-RATE-REFRESH).

    A row whose newest entry carries a band is appended to at most once per
    run: a state inside the band is no news — a Move with nothing in it
    would still bump PRICING_VERSION, write both files and commit, which is
    the churn the band exists to stop — and the states outside it append
    the ONE entry price_band.reform returns: the band re-forms, or the
    price in force is followed when the window no longer oscillates. An
    unbanded row whose window classifies as
    oscillating gets its new states plus the ONE band entry
    price_band.formation returns, dated at the detection instant. Search
    changes share an entry newly created at that instant. A collision with
    committed history is deferred to the next actual append."""
    history = hosts.get(host)
    prior_length = len(history) if history is not None else 0
    original = history[-1] if history else None
    old_search = original.get("web_search", 0.0) if original else 0.0
    move = None
    if history is None:
        additions = _floored_log(model, host, entries)
        history = hosts[host] = copy.deepcopy(additions)
        move = Move(model, host, None, listing, len(additions), "log")
    else:
        additions = _new_log_states(model, host, entries, original)
        if additions:
            # The price log has no web-search field. Carry the last sampled
            # search rate across token-only log entries, preserving the
            # rate's own detection-time epoch.
            if old_search:
                for entry in additions:
                    entry["web_search"] = old_search
            if price_band.entry_band(original) is not None:
                reformed = price_band.reform(history, original, additions, at,
                                             price_band.WINDOW_DAYS)
                if reformed is not None:
                    if old_search:
                        reformed["web_search"] = old_search
                    history.append(reformed)
                    source = "band" if "band" in reformed else "log"
                    move = Move(model, host, original, listing, 1, source,
                                reformed.get("fresh") if source == "band" else None)
            else:
                history.extend(additions)
                move = Move(model, host, original, listing, len(additions), "log")
    if move is not None and move.source != "band":
        formed = price_band.formation(history, at, price_band.WINDOW_DAYS)
        if formed is not None:
            if old_search:
                formed["web_search"] = old_search
            history.append(formed)
            move = Move(model, host, original, listing,
                        move.entries_appended + 1, "band", formed["fresh"])

    new_search = listing.rates.get("web_search", 0.0)
    if new_search == old_search:
        return move

    stamp = at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    where = f"{model} via {host}"
    detected_at = _price_instant(stamp, where)
    committed = [entry for entry in history[:prior_length]
                 if (entry.get("from") is not None
                     and _price_instant(entry["from"], where) >= detected_at)]
    if committed:
        blocker = committed[-1]
        next_append = next((i for i in range(prior_length, len(history))
                            if (history[i].get("from") is not None
                                and _price_instant(history[i]["from"], where)
                                > detected_at)), None)
        if next_append is not None:
            for entry in history[next_append:]:
                if (entry.get("from") is not None
                        and _price_instant(entry["from"], where) > detected_at):
                    _set_search_rate(entry, new_search)
            deferred_at = history[next_append]["from"]
            message = (f"{model} via {host}: web_search change deferred from "
                       f"{stamp} to the next append at {deferred_at}; the "
                       f"entry dated {blocker['from']} is already committed")
        else:
            message = (f"{model} via {host}: web_search change deferred from "
                       f"{stamp} until the next append; the entry dated "
                       f"{blocker['from']} is already committed")
        if deferred_notices is not None:
            deferred_notices.append(message)
        return move

    newly_created_at = next((i for i in range(prior_length, len(history))
                             if (history[i].get("from") is not None
                                 and _price_instant(history[i]["from"], where)
                                 == detected_at)), None)
    if newly_created_at is not None:
        # A token point or band entry created by this run already owns this
        # instant. Combine the search rate there and carry it through any
        # later token states created in the same run.
        for entry in history[newly_created_at:]:
            if (entry.get("from") is not None
                    and _price_instant(entry["from"], where) >= detected_at):
                _set_search_rate(entry, new_search)
        return move

    insert_at = next((i for i, entry in enumerate(history)
                      if (entry.get("from") is not None
                          and _price_instant(entry["from"], where) > detected_at)),
                     len(history))
    previous = history[insert_at - 1] if insert_at else None
    if previous is None:
        next_append = next((i for i in range(prior_length, len(history))
                            if history[i].get("from") is not None), None)
        if next_append is not None:
            for entry in history[next_append:]:
                _set_search_rate(entry, new_search)
            deferred_at = history[next_append]["from"]
            message = (f"{model} via {host}: web_search change deferred from "
                       f"{stamp} to the next append at {deferred_at}; no "
                       "predecessor entry exists at detection")
        else:
            message = (f"{model} via {host}: web_search change deferred from "
                       f"{stamp} until the next append; no predecessor entry "
                       "exists at detection")
        if deferred_notices is not None:
            deferred_notices.append(message)
        return move

    search_entry = _entry(stamp, listing)
    for field in RATE_FIELDS:
        search_entry[field] = previous[field]
    if "band" in previous:
        search_entry["band"] = copy.deepcopy(previous["band"])
    _set_search_rate(search_entry, new_search)
    history.insert(insert_at, search_entry)
    for entry in history[insert_at + 1:]:
        if (entry.get("from") is not None
                and _price_instant(entry["from"], where) > detected_at):
            _set_search_rate(entry, new_search)
    if move is None:
        move = Move(model, host, original, listing, 1, "search")
    return move
