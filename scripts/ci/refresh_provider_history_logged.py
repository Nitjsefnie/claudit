"""Append token-log states and search-rate changes to provider histories."""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone

import price_band
from refresh_provider_history import (
    Move, _entry, _floored_log, _new_log_states, _price_instant,
    _set_search_rate,
)
from refresh_selection import Listing
from backend.pricing_document import effective_rates


@dataclass
class _LogChange:
    """Mutable state for one host's logged and search-rate append."""
    model: str
    host: str
    listing: Listing
    history: list[dict]
    prior_length: int
    original: dict | None
    old_search: float
    move: Move | None = None


def _append_token_states(change: _LogChange, additions: list[dict],
                         at: datetime) -> None:
    """Append the new token states, reforming or forming a band as needed."""
    if not additions:
        return
    if change.old_search:
        for entry in additions:
            entry["web_search"] = change.old_search
    original = change.original
    assert original is not None
    if price_band.entry_band(original) is not None:
        reformed = price_band.reform(
            change.history, original, additions, at, price_band.WINDOW_DAYS)
        if reformed is None:
            return
        if change.old_search:
            reformed["web_search"] = change.old_search
        change.history.append(reformed)
        source = "band" if "band" in reformed else "log"
        change.move = Move(
            change.model, change.host, original, change.listing, 1, source,
            reformed.get("fresh") if source == "band" else None)
        return
    change.history.extend(additions)
    change.move = Move(change.model, change.host, original, change.listing,
                       len(additions), "log")


def _form_band(change: _LogChange, at: datetime) -> None:
    """Append the newly formed band after an unbanded log change."""
    move = change.move
    if move is None or move.source == "band":
        return
    formed = price_band.formation(
        change.history, at, price_band.WINDOW_DAYS)
    if formed is None:
        return
    if change.old_search:
        formed["web_search"] = change.old_search
    change.history.append(formed)
    change.move = Move(
        change.model, change.host, change.original, change.listing,
        move.entries_appended + 1, "band", formed["fresh"])


def _prepare_log_change(model: str, hosts: dict, host: str,
                        listing: Listing, entries: list[dict],
                        at: datetime) -> _LogChange:
    """Build the history state after appending any new logged token states."""
    history = hosts.get(host)
    prior_length = len(history) if history is not None else 0
    original = history[-1] if history else None
    old_search = original.get("web_search", 0.0) if original else 0.0
    change = _LogChange(model, host, listing, history or [], prior_length,
                        original, old_search)
    if history is None:
        additions = _floored_log(model, host, entries)
        change.history = copy.deepcopy(additions)
        hosts[host] = change.history
        change.move = Move(model, host, None, listing, len(additions), "log")
    else:
        assert original is not None
        additions = _new_log_states(model, host, entries, original)
        _append_token_states(change, additions, at)
    _form_band(change, at)
    return change


def _defer_committed_search_change(
        change: _LogChange, stamp: str, detected_at: datetime,
        new_search: float, where: str,
        deferred_notices: list[str] | None) -> bool:
    """Defer a search update blocked by an already committed entry."""
    committed = [entry for entry in change.history[:change.prior_length]
                 if (entry.get("from") is not None
                     and _price_instant(entry["from"], where) >= detected_at)]
    if not committed:
        return False
    blocker = committed[-1]
    next_append = next((i for i in range(change.prior_length, len(change.history))
                        if (change.history[i].get("from") is not None
                            and _price_instant(
                                change.history[i]["from"], where) > detected_at)),
                       None)
    if next_append is not None:
        for entry in change.history[next_append:]:
            if (entry.get("from") is not None
                    and _price_instant(entry["from"], where) > detected_at):
                _set_search_rate(entry, new_search)
        deferred_at = change.history[next_append]["from"]
        message = (f"{change.model} via {change.host}: web_search change deferred "
                   f"from {stamp} to the next append at {deferred_at}; the "
                   f"entry dated {blocker['from']} is already committed")
    else:
        message = (f"{change.model} via {change.host}: web_search change deferred "
                   f"from {stamp} until the next append; the entry dated "
                   f"{blocker['from']} is already committed")
    if deferred_notices is not None:
        deferred_notices.append(message)
    return True


def _carry_search_to_detected_entries(
        change: _LogChange, detected_at: datetime, new_search: float,
        where: str) -> bool:
    """Merge a search update into entries created at its detection instant."""
    index = next((i for i in range(change.prior_length, len(change.history))
                 if (change.history[i].get("from") is not None
                     and _price_instant(
                         change.history[i]["from"], where) == detected_at)), None)
    if index is None:
        return False
    for entry in change.history[index:]:
        if (entry.get("from") is not None
                and _price_instant(entry["from"], where) >= detected_at):
            _set_search_rate(entry, new_search)
    return True


def _defer_search_without_predecessor(
        change: _LogChange, stamp: str, new_search: float,
        deferred_notices: list[str] | None) -> None:
    """Carry a search update to the next append when no predecessor exists."""
    next_append = next((i for i in range(change.prior_length, len(change.history))
                        if change.history[i].get("from") is not None), None)
    if next_append is not None:
        for entry in change.history[next_append:]:
            _set_search_rate(entry, new_search)
        deferred_at = change.history[next_append]["from"]
        message = (f"{change.model} via {change.host}: web_search change deferred "
                   f"from {stamp} to the next append at {deferred_at}; no "
                   "predecessor entry exists at detection")
    else:
        message = (f"{change.model} via {change.host}: web_search change deferred "
                   f"from {stamp} until the next append; no predecessor entry "
                   "exists at detection")
    if deferred_notices is not None:
        deferred_notices.append(message)


def _insert_search_entry(change: _LogChange, insert_at: int, stamp: str,
                         detected_at: datetime, new_search: float,
                         where: str) -> None:
    """Insert a search-only state and carry it through later states."""
    previous = change.history[insert_at - 1]
    search_entry = _entry(stamp, change.listing)
    search_entry.update(effective_rates(previous))
    if "band" in previous:
        search_entry["band"] = copy.deepcopy(previous["band"])
    _set_search_rate(search_entry, new_search)
    change.history.insert(insert_at, search_entry)
    for entry in change.history[insert_at + 1:]:
        if (entry.get("from") is not None
                and _price_instant(entry["from"], where) > detected_at):
            _set_search_rate(entry, new_search)


def _append_search_change(change: _LogChange, at: datetime,
                          deferred_notices: list[str] | None) -> Move | None:
    """Append or defer the listing's search-rate change."""
    new_search = change.listing.rates.get("web_search", 0.0)
    if new_search == change.old_search:
        return change.move
    stamp = at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    where = f"{change.model} via {change.host}"
    detected_at = _price_instant(stamp, where)
    if _defer_committed_search_change(
            change, stamp, detected_at, new_search, where, deferred_notices):
        return change.move
    if _carry_search_to_detected_entries(
            change, detected_at, new_search, where):
        return change.move
    insert_at = next((i for i, entry in enumerate(change.history)
                      if (entry.get("from") is not None
                          and _price_instant(entry["from"], where) > detected_at)),
                     len(change.history))
    if insert_at == 0:
        _defer_search_without_predecessor(
            change, stamp, new_search, deferred_notices)
        return change.move
    _insert_search_entry(change, insert_at, stamp, detected_at,
                         new_search, where)
    if change.move is None:
        change.move = Move(change.model, change.host, change.original,
                           change.listing, 1, "search")
    return change.move


def _append_logged(model: str, hosts: dict, host: str, listing: Listing,
                   entries: list[dict], at: datetime,
                   deferred_notices: list[str] | None = None) -> Move | None:
    """Append every new log state after the stored row without rewriting it.

    A row whose newest entry carries a band is appended to at most once per
    run: a state inside the band is no news, and states outside it append the
    one entry returned by price_band.reform. An unbanded row whose window
    classifies as oscillating gets its new states plus the one band entry
    price_band.formation returns. Search changes share an entry newly created
    at that instant; a collision with committed history is deferred to the
    next actual append (issues #663, #664, #665; SV-RATE-REFRESH).
    """
    change = _prepare_log_change(model, hosts, host, listing, entries, at)
    return _append_search_change(change, at, deferred_notices)
