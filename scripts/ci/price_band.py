#!/usr/bin/env python3
"""A price band: an oscillating host's range, recorded once (SV-RATE-REFRESH).

A host whose listed price moves inside a range and returns to levels it has
held before is dynamic, not repriced: appending every move is what turns the
hourly refresh into a commit every hour. A band entry records that range once,
in an optional `band` sibling of the five rate fields, and is priced by those
five fields exactly like any other entry — their time-weighted mean over the
window that formed the band. A listing inside the band appends nothing; one
outside it widens the band (widen). Collapse is the one-time, human-reviewed
rewrite that forms a band's first entry (collapse); the hourly refresh never
rewrites one, it only widens.

This module owns the shape of a band (entry_band), the classification that
decides which rows oscillate (classify), the mean a band is priced by
(time_weighted) and the two rewrites (collapse, widen). The loaders validate
the shape itself — `backend.pricing_load.check_band`, which this module shares
so both spell one shape, mirrored by src/pricing-loader.js's _checkBand — and
then ignore it.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
# pylint: disable=wrong-import-position
sys.path.insert(0, str(Path(__file__).resolve().parent))
from refresh_common import detection_stamp  # noqa: E402
from backend import pricing  # noqa: E402
from backend.pricing_load import check_band  # noqa: E402

RATE_FIELDS = pricing.RATE_FIELDS
# The shapes a row's price moves can take over a window (classify).
SHAPES = ("STABLE", "STEP", "TOGGLE", "BAND")
# The two that oscillate: a row to record as a band, once, by hand.
OSCILLATING = ("TOGGLE", "BAND")
_WHERE = "a provider row entry"


def entry_band(entry: dict) -> dict | None:
    """The band an entry carries, checked, or None when it carries none.

    The shape is the one `backend.pricing_load.check_band` validates, so
    this module and the backend loader never disagree about what a band is.
    """
    if "band" not in entry:
        return None
    return check_band(entry["band"], "band")


def in_band(entry: dict, rates: dict) -> bool:
    """Whether every rate `rates` names lies inside the entry's band.

    False for an entry with no band — an unbanded row is never inside one,
    which is what keeps a STEP row on the refresh's append-every-move path.
    A field the band does not name is unconstrained.
    """
    band = entry_band(entry)
    if band is None:
        return False
    return all(span[0] <= rates[field] <= span[1] for field, span in band.items())


def _instant(stamp: object) -> datetime:
    return pricing._instant(stamp, _WHERE)  # pylint: disable=protected-access


def _level(entry: dict) -> tuple:
    """A row's state as one comparable value: its five rates and its schedule."""
    return (tuple(entry.get(field) for field in RATE_FIELDS),
            json.dumps(entry.get("schedule"), sort_keys=True))


def classify(history: list[dict], at: datetime, days: float) -> dict:
    """The shape of a row's price over the `days` before `at`, never wall clock.

    STABLE moved at most once; STEP never returned to a level it had held
    and either stayed at three levels or moved less than once a day; TOGGLE
    returned within three levels; BAND moved through four or more. The
    baseline is the last entry dated before the window, so the first
    in-window entry counts as a change.
    """
    if days <= 0:
        raise ValueError(f"the classification window is {days} days; it must be positive")
    since = at - timedelta(days=days)
    dated = [entry for entry in history if entry.get("from") is not None]
    inside = [entry for entry in dated if _instant(entry["from"]) >= since]
    before = [entry for entry in dated if _instant(entry["from"]) < since]
    levels = [_level(entry) for entry in before[-1:] + inside]
    changes = max(0, len(levels) - 1)
    seen = set(levels[:1])
    returns = 0
    for level in levels[1:]:
        if level in seen:
            returns += 1
        seen.add(level)
    distinct = len(set(levels))
    if changes <= 1:
        shape = "STABLE"
    elif returns == 0 and (distinct <= 3 or changes / days < 1):
        shape = "STEP"
    elif distinct <= 3:
        shape = "TOGGLE"
    else:
        shape = "BAND"
    return {"shape": shape, "changes": changes, "distinct": distinct,
            "returns": returns}


def time_weighted(history: list[dict], at: datetime) -> dict:
    """The five fields' time-weighted mean over a row's dated window.

    Each level holds from its own `from` until its successor's, and the last
    until `at`. A leading entry with no `from` is "of all time" and starts
    nothing: the window opens at the first DATED entry, and a row with no
    dated entry at all contributes no duration, so it stands as it is.
    """
    dated = [(_instant(entry["from"]), entry) for entry in history
             if entry.get("from") is not None]
    if not dated:
        return {field: history[-1][field] for field in RATE_FIELDS}
    total = (at - dated[0][0]).total_seconds()
    if total <= 0:
        return {field: dated[0][1][field] for field in RATE_FIELDS}
    mean = {}
    for field in RATE_FIELDS:
        area = sum(entry[field] * max(0.0, (end - start).total_seconds())
                   for start, end, entry in
                   ((dated[i][0], dated[i + 1][0] if i + 1 < len(dated) else at,
                     dated[i][1]) for i in range(len(dated))))
        mean[field] = round(area / total, 10)
    return mean


def collapse(history: list[dict], at: datetime) -> list[dict]:
    """A row rewritten to the one band entry that stands for its whole history.

    The row keeps its own start (an entry with no `from` keeps none), its
    five rates become the time-weighted mean over its history, and `band`
    spans every level the history held — the whole of it, not just the
    window that classified the row. A note survives only when every entry
    shares one: the mean is not any one listed price, so a discount note
    describing one of them would be false. A scheduled or fee-recording row
    is refused rather than collapsed, because neither a window price nor a
    per-request fee survives a mean.
    """
    if any("schedule" in entry for entry in history):
        raise ValueError("a scheduled row cannot be collapsed to a mean: a "
                         "window's price is not the level a mean prices")
    if any("/request" in entry.get("note", "") for entry in history):
        raise ValueError("a row recording a per-request fee cannot be "
                         "collapsed to a mean: the mean is not a listed price "
                         "the fee applies to")
    band = {field: [min(entry[field] for entry in history),
                    max(entry[field] for entry in history)]
            for field in RATE_FIELDS}
    collapsed = {"from": history[0]["from"], **time_weighted(history, at),
                 "band": band}
    notes = {entry.get("note", "") for entry in history}
    if len(notes) == 1 and (note := notes.pop()):
        collapsed["note"] = note
    return [collapsed]


def widen(entry: dict, rates: dict, at: datetime) -> dict:
    """A banded entry re-formed to also cover `rates`.

    The band grows to `[min(old_min, new), max(old_max, new)]` per field it
    names, and the five rates become the time-weighted mean over the extended
    window: the entry's own level up to the state arriving at `at`, which has
    no measured duration of its own yet. The priced level therefore stands
    and the entry's note and schedule carry over, so a widened band widens
    what a listing may cost without repricing what a record already cost.
    """
    band = entry_band(entry)
    if band is None:
        raise ValueError("widen needs a banded entry; this one carries none")
    stamp = detection_stamp(at)
    widened = {"from": stamp,
               **time_weighted([entry, {"from": stamp, **rates}], at),
               "band": {field: [min(span[0], rates[field]), max(span[1], rates[field])]
                        for field, span in band.items()}}
    for key in ("note", "schedule"):
        if key in entry:
            widened[key] = entry[key]
    return widened
