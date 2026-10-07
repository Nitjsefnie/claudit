#!/usr/bin/env python3
"""A price band: an oscillating host's range, recorded once (SV-RATE-REFRESH).

A host whose listed price moves inside a range and returns to levels it has
held before is dynamic, not repriced: appending every move is what turns the
hourly refresh into a commit every hour. A band entry records that range once,
in an optional `band` sibling of the five rate fields, and is priced by those
five fields exactly like any other entry — their time-weighted mean over the
window that formed the band, the classifier's trailing `days`: the last entry
dated before it plus every entry in it (issues #663, #665). A listing inside
the band appends nothing. A listing outside it appends ONE entry that re-forms
the band, or follows the price when the window no longer oscillates (reform).
The hourly
log path forms a row's band itself, dated at the detection instant, when an
unbanded row's window starts to oscillate (formation, issue #664). Collapse is
the one-time, human-reviewed whole-history pass (collapse).

This module owns the shape of a band (entry_band), the classification that
decides which rows oscillate (classify), the mean a band is priced by
(time_weighted, form) and the three formations (collapse, formation, reform).
The loaders validate the shape itself — `backend.pricing_load.check_band`,
which this module shares so both spell one shape, mirrored by
src/pricing-loader.js's _checkBand — and then ignore it.
"""
from __future__ import annotations

import copy
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
# The trailing window, in days, every band formation measures over — the
# collapse's default and the hourly path's window.
WINDOW_DAYS = 7.0


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


def _window_levels(history: list[dict], at: datetime, days: float) -> list[dict]:
    """The levels the trailing `days` before `at` observed: the last entry
    dated before the window plus every entry inside it — the basis of every
    band and mean (issue #663). The level in force at the window's open may
    be the row's leading UNDATED entry ("of all time"): with nothing dated
    before the window, it is the baseline. A row with no dated entry at all
    contributes its undated entries, whose window is the whole (unobserved)
    history."""
    dated = [entry for entry in history if entry.get("from") is not None]
    if not dated:
        return list(history)
    since = at - timedelta(days=days)
    before = [entry for entry in dated if _instant(entry["from"]) < since]
    inside = [entry for entry in dated if _instant(entry["from"]) >= since]
    baseline = before[-1:]
    if not baseline and history and history[0].get("from") is None:
        baseline = [history[0]]
    return baseline + inside


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


def time_weighted(history: list[dict], at: datetime,
                  since: datetime | None = None) -> dict:
    """The five fields' time-weighted mean over a row's dated window.

    Each level holds from its own `from` until its successor's, and the last
    until `at`. A leading entry with no `from` is "of all time": without a
    `since` it starts nothing — the window opens at the first DATED entry —
    and a row with no dated entry at all contributes no duration, so it
    stands as it is. With a `since`, that undated entry is the level in
    force at the window's open when nothing dated precedes it, and it
    weighs from `since`. A `since` also clips every dated level's weight to
    the window it bounds: a level that predates it held at the window's
    open, so it weighs only from there — the mean integrates over the
    window, not before it (issue #663).
    """
    dated = [(_instant(entry["from"]), entry) for entry in history
             if entry.get("from") is not None]
    if not dated:
        return {field: history[-1][field] for field in RATE_FIELDS}
    if since is not None and history and history[0].get("from") is None \
            and dated[0][0] > since:
        # The leading undated entry is the level in force at the window's
        # open: it enters the mean dated at `since` (issue #663). Superseded
        # before the window, it weighs nothing, as before.
        dated.insert(0, (since, history[0]))
    if since is not None:
        dated = [(max(instant, since), entry) for instant, entry in dated]
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


def form(levels: list[dict], at: datetime, since: datetime) -> dict:
    """The band a window's levels form: the range they span, and the five
    rates the mean prices by — time_weighted over the levels bounded by
    `since`, whose last level holds until `at`, so the mean follows the
    price in force and weighs only what the window saw."""
    return {"band": {field: [min(entry[field] for entry in levels),
                             max(entry[field] for entry in levels)]
                     for field in RATE_FIELDS},
            **time_weighted(levels, at, since)}


def collapse(history: list[dict], at: datetime, days: float) -> list[dict]:
    """A row rewritten to the one band entry the window that classified it
    leaves (issue #663).

    The row keeps its own start (an entry with no `from` keeps none), and
    its five rates and `band` come from the window's levels — the last entry
    dated before the trailing `days` before `at` plus every entry inside it
    — not the whole history: an old level far from the oscillation neither
    widens the band nor pulls the mean. A note survives only when every
    WINDOW entry shares one (the mean prices them, not the rest): the mean
    is not any one listed price, so a discount note describing one of them
    would be false. A scheduled row, or a row recording a per-request fee
    anywhere in it, is refused rather than collapsed, because neither a
    window price nor a recorded cost survives a mean that drops entries.
    """
    if any("schedule" in entry for entry in history):
        raise ValueError("a scheduled row cannot be collapsed to a mean: a "
                         "window's price is not the level a mean prices")
    if any("/request" in entry.get("note", "") for entry in history):
        raise ValueError("a row recording a per-request fee cannot be "
                         "collapsed to a mean: the mean is not a listed price "
                         "the fee applies to")
    levels = _window_levels(history, at, days)
    band = {field: [min(entry[field] for entry in levels),
                    max(entry[field] for entry in levels)]
            for field in RATE_FIELDS}
    collapsed = {"from": history[0]["from"],
                 **time_weighted(levels, at, at - timedelta(days=days)),
                 "band": band}
    notes = {entry.get("note", "") for entry in levels}
    if len(notes) == 1 and (note := notes.pop()):
        collapsed["note"] = note
    return [collapsed]


def formation(history: list[dict], at: datetime, days: float) -> dict | None:
    """The band entry an unbanded row's window forms, or None.

    Issue #664: the hourly log path forms a row's band itself, so a host
    that starts oscillating after any collapse stops committing every
    in-range move without a rerun of the collapse script. The row
    classifies over the trailing `days` (its new states already in it);
    TOGGLE or BAND appends ONE band entry dated at the detection instant —
    every earlier level stays an actual entry, so a record priced before
    the detection instant prices at the level that held there. A newest
    entry dated at or after `at` forms nothing: the loaders refuse an
    entry not strictly after its predecessor.
    """
    if classify(history, at, days)["shape"] not in OSCILLATING:
        return None
    if history and history[-1].get("from") is not None \
            and _instant(history[-1]["from"]) >= at:
        return None
    return {"from": detection_stamp(at),
            **form(_window_levels(history, at, days), at,
                   at - timedelta(days=days))}


def reform(history: list[dict], newest: dict, additions: list[dict],
           at: datetime, days: float) -> dict | None:
    """The ONE entry an escape appends to a banded row, or None when every
    new state lies inside the band — an in-band state is no news, and a
    Move with no entries in it would still bump PRICING_VERSION, write
    both files and commit.

    Every state outside the band is answered by one entry, whatever its
    direction: two escapes in opposite directions inside one window are
    one entry. The window with the new states folded in decides the
    entry's shape (issue #665):

    - still oscillating: the band RE-FORMS — the range the window's levels
      span, priced by their time-weighted mean — dated at the last
      escape's change point. The mean's window is the classifier's: it
      opens at the window's start and ends at `at`, where the level in
      force holds until it, so the re-formed row tracks the price it
      names;
    - the window no longer oscillating (STEP or STABLE): the entry
      carries the state now in force instead, dated at its own change
      point, so the row follows the price.

    The entry is APPENDED, so an undated banded entry keeps its
    `from: None` and the row still covers records from before the escape:
    only a row whose FIRST entry names an instant stops existing before
    it. The re-formed entry carries the banded entry's note and schedule;
    the follow-the-price one carries the state's own.
    """
    outside = [entry for entry in additions
               if not in_band(newest, {f: entry[f] for f in RATE_FIELDS})]
    if not outside:
        return None
    whole = list(history) + list(additions)
    if classify(whole, at, days)["shape"] in OSCILLATING:
        moved_at = _instant(outside[-1]["from"])
        entry = {"from": detection_stamp(moved_at),
                 **form(_window_levels(whole, at, days), at,
                        at - timedelta(days=days))}
        for key in ("note", "schedule"):
            if key in newest:
                entry[key] = newest[key]
        return entry
    return copy.deepcopy(additions[-1])
