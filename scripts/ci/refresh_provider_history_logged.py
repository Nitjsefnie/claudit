from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone

from refresh_common import detection_stamp
from refresh_provider_history import (
    Move, _floored_log, _occurrences, _price_instant,
    _sampled_state, _set_sampled_state,
)
from refresh_provider_history_rewrite import _merge_history, _priced_history, _rewrite
from refresh_selection import Listing


@dataclass
class _LogChange:
    model: str
    host: str
    listing: Listing
    history: list[dict]
    prior_length: int
    original: dict | None
    old_sampled: dict
    move: Move | None = None


def _sampled_label(change: _LogChange) -> str:
    fields = _sampled_state({**change.listing.rates, **change.listing.identity})
    return ", ".join(key for key, value in fields.items()
                     if value != change.old_sampled[key])


def _prepare_log_change(model: str, hosts: dict, host: str,
                        listing: Listing, entries: list[dict], at: datetime,
                        notices: list[str] | None) -> _LogChange:
    stored = hosts.get(host)
    history = list(_occurrences(stored, f"{model} via {host}")) if stored else []
    prior_length = len(history)
    original = history[-1] if history else None
    old_sampled = _sampled_state(original or {})
    change = _LogChange(model, host, listing, history or [], prior_length,
                        original, old_sampled)
    if stored is None:
        additions = _floored_log(model, host, entries)
        change.history = copy.deepcopy(additions)
        change.move = Move(model, host, None, listing, len(additions), "log")
    else:
        rewritten = _rewrite(stored, entries, detection_stamp(at))
        if rewritten is not None:
            if _priced_history(rewritten) != _priced_history(stored):
                change.history = _occurrences(rewritten, f"{model} via {host}")
                change.prior_length = 0
                change.old_sampled = _sampled_state(change.history[-1])
                change.move = Move(model, host, original, listing, len(entries), "log",
                                   "log rewrite from the listed-pricing history")
            elif rewritten != stored and notices is not None:
                notices.append(f"{model} via {host}: log rewrite leaves priced history "
                               "unchanged; stored annotations and grouping kept")
    return change


def _defer_committed_sampled_change(
        change: _LogChange, stamp: str, detected_at: datetime,
        new_sampled: dict, where: str,
        deferred_notices: list[str] | None) -> bool:
    """Defer sampled state blocked by an already committed entry."""
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
                _set_sampled_state(entry, new_sampled)
        deferred_at = change.history[next_append]["from"]
        message = (f"{change.model} via {change.host}: {_sampled_label(change)} change deferred "
                   f"from {stamp} to the next append at {deferred_at}; the "
                   f"entry dated {blocker['from']} is already committed")
    else:
        message = (f"{change.model} via {change.host}: {_sampled_label(change)} change deferred "
                   f"from {stamp} until the next append; the entry dated "
                   f"{blocker['from']} is already committed")
    if deferred_notices is not None:
        deferred_notices.append(message)
    return True


def _carry_sampled_to_detected_entries(
        change: _LogChange, detected_at: datetime, new_sampled: dict,
        where: str) -> bool:
    """Merge sampled state into entries created at its detection instant."""
    index = next((i for i in range(change.prior_length, len(change.history))
                 if (change.history[i].get("from") is not None
                     and _price_instant(
                         change.history[i]["from"], where) == detected_at)), None)
    if index is None:
        return False
    for entry in change.history[index:]:
        if (entry.get("from") is not None
                and _price_instant(entry["from"], where) >= detected_at):
            _set_sampled_state(entry, new_sampled)
    return True


def _defer_sampled_without_predecessor(
        change: _LogChange, stamp: str, new_sampled: dict,
        deferred_notices: list[str] | None) -> None:
    """Carry sampled state to the next append when no predecessor exists."""
    next_append = next((i for i in range(change.prior_length, len(change.history))
                        if change.history[i].get("from") is not None), None)
    if next_append is not None:
        for entry in change.history[next_append:]:
            _set_sampled_state(entry, new_sampled)
        deferred_at = change.history[next_append]["from"]
        message = (f"{change.model} via {change.host}: {_sampled_label(change)} change deferred "
                   f"from {stamp} to the next append at {deferred_at}; no "
                   "predecessor entry exists at detection")
    else:
        message = (f"{change.model} via {change.host}: {_sampled_label(change)} change deferred "
                   f"from {stamp} until the next append; no predecessor entry "
                   "exists at detection")
    if deferred_notices is not None:
        deferred_notices.append(message)


def _insert_sampled_entry(change: _LogChange, insert_at: int, stamp: str,
                          detected_at: datetime, new_sampled: dict,
                          where: str) -> None:
    """Insert sampled state and carry it through later states."""
    previous = change.history[insert_at - 1]
    sampled_entry = {**previous, "from": stamp}
    _set_sampled_state(sampled_entry, new_sampled)
    change.history.insert(insert_at, sampled_entry)
    for entry in change.history[insert_at + 1:]:
        if (entry.get("from") is not None
                and _price_instant(entry["from"], where) > detected_at):
            _set_sampled_state(entry, new_sampled)


def _append_sampled_change(change: _LogChange, at: datetime,
                           deferred_notices: list[str] | None) -> Move | None:
    """Append or defer the listing's sampled-state change."""
    new_sampled = _sampled_state({**change.listing.rates, **change.listing.identity})
    if new_sampled == change.old_sampled:
        return change.move
    stamp = at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    where = f"{change.model} via {change.host}"
    detected_at = _price_instant(stamp, where)
    if _defer_committed_sampled_change(
            change, stamp, detected_at, new_sampled, where, deferred_notices):
        return change.move
    if _carry_sampled_to_detected_entries(
            change, detected_at, new_sampled, where):
        return change.move
    insert_at = next((i for i, entry in enumerate(change.history)
                      if (entry.get("from") is not None
                          and _price_instant(entry["from"], where) > detected_at)),
                     len(change.history))
    if insert_at == 0:
        _defer_sampled_without_predecessor(
            change, stamp, new_sampled, deferred_notices)
        return change.move
    _insert_sampled_entry(change, insert_at, stamp, detected_at,
                          new_sampled, where)
    if change.move is None:
        change.move = Move(change.model, change.host, change.original,
                           change.listing, 1,
                           "context" if new_sampled["context_length"] !=
                           change.old_sampled["context_length"] else "search")
    return change.move


def _append_logged(model: str, hosts: dict, host: str, listing: Listing,
                   entries: list[dict], at: datetime,
                   deferred_notices: list[str] | None = None) -> Move | None:
    """Rewrite a stored log-backed row when its priced history differs.

    First-seen hosts import from the history floor. Sampled fields retain
    their detection-time insert/defer behavior on the resulting history.
    """
    stored = hosts.get(host)
    change = _prepare_log_change(model, hosts, host, listing, entries, at, deferred_notices)
    move = _append_sampled_change(change, at, deferred_notices)
    if move is not None:
        if move.reason:
            candidate = _merge_history(change.history)
            if stored is not None and _priced_history(candidate) == _priced_history(stored):
                if candidate != stored and deferred_notices is not None:
                    deferred_notices.append(
                        f"{model} via {host}: log rewrite leaves priced history "
                        "unchanged; stored annotations and grouping kept")
                return None
            hosts[host] = candidate
        else:
            hosts[host] = [*(stored or []), *change.history[change.prior_length:]]
    return move
