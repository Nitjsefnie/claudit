"""Shared rewrite core for log-backed OpenRouter provider histories."""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from typing import cast

from backend.pricing_document import effective_rates
from backend.pricing_rows import flatten_provider_history


def _earlier_start(old_start: str | None, log_start: str) -> str | None:
    if old_start is None:
        return None
    old_at = datetime.fromisoformat(old_start.replace("Z", "+00:00"))
    log_at = datetime.strptime(log_start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return old_start if old_at < log_at else log_start


def _flatten(history: list[dict]) -> list[dict]:
    entries, _ = flatten_provider_history(
        history, "backfill provider history",
        lambda stamp, _: datetime.fromisoformat(cast(str, stamp).replace("Z", "+00:00")))
    return entries


def _merge_history(history: list[dict]) -> list[dict]:
    """Group identical dated states without dropping any of their instants.

    Notes and other priced fields are part of a state. A null start stays
    scalar because the storage format cannot mix null with dated instants.
    """
    merged: list[dict] = []
    states: dict[str, dict] = {}
    for entry in _flatten(history):
        state = {k: v for k, v in entry.items() if k != "from"}
        state.update(effective_rates(entry))
        stamp = entry.get("from")
        key = json.dumps(state, sort_keys=True, allow_nan=False)
        if stamp is not None and key in states:
            previous = states[key]
            instants = previous["from"]
            previous["from"] = ([*instants, stamp] if isinstance(instants, list)
                                else [instants, stamp])
        else:
            current = {"from": stamp, **copy.deepcopy(state)}
            merged.append(current)
            if stamp is not None:
                states[key] = current
    return merged


def _instant(stamp: str | None) -> datetime:
    return (datetime.fromisoformat(stamp.replace("Z", "+00:00")) if stamp is not None
            else datetime.min.replace(tzinfo=timezone.utc))


def _sampled_changes(history: list[dict], as_of: str,
                     field: str) -> list[dict]:
    """Read old sampled field moves strictly before the rewrite cutoff."""
    changes: list[dict] = []
    previous = 0
    for entry in _flatten(history):
        if _instant(entry.get("from")) >= _instant(as_of):
            break
        search = entry.get(field, 0)
        if search != previous:
            changes.append({"from": entry.get("from"), field: search})
            previous = search
    return changes


def _overlay_sampled(entries: list[dict], changes: list[dict],
                     field: str) -> list[dict]:
    """Carry a sampled field across token states, splitting at its moves."""
    if not changes:
        return entries
    events = [(_instant(entry.get("from")), kind, entry)
              for kind, history in enumerate((entries, changes)) for entry in history]
    events.sort(key=lambda event: (event[0], event[1]))
    overlaid: list[dict] = []
    search = 0
    for instant, is_search, entry in events:
        if is_search:
            search = entry[field]
            if _instant(overlaid[-1].get("from")) != instant:
                overlaid.append({**overlaid[-1], "from": entry["from"]})
        else:
            overlaid.append(entry)
        if search:
            overlaid[-1][field] = search
        else:
            overlaid[-1].pop(field, None)
    return overlaid


def _rewrite(history: list[dict], entries: list[dict], as_of: str) -> list[dict] | None:
    kept = [copy.deepcopy(entry) for entry in entries if entry["from"] <= as_of]
    if not kept:
        return None
    kept[0]["from"] = _earlier_start(_flatten(history)[0].get("from"), kept[0]["from"])
    for field in ("web_search", "context_length"):
        kept = _overlay_sampled(kept, _sampled_changes(history, as_of, field), field)
    return _merge_history(kept)
