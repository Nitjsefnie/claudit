#!/usr/bin/env python3
"""Detect an alternating OpenRouter provider price, and its notice.

A listing whose rates equal an earlier entry — never the newest — of its
row from the last 7 days is an alternating price: a price flip-flopping
would otherwise be appended on every change, so the run reports it for a
human instead of appending (SV-RATE-REFRESH). A schedule on either side —
the listing's or the row's newest entry's — exempts the row. The lookback
is measured against the detection time, never wall clock.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
# pylint: disable=wrong-import-position
from backend import pricing  # noqa: E402

RATE_FIELDS = pricing.RATE_FIELDS


def notice(history: list[dict], rates: dict, listing_schedule: list | None,
           newest: dict, at: datetime, where: str) -> str | None:
    """The notice for an alternating price, or None when the move appends.

    `history` is the row's stored entries and `rates`/`listing_schedule`
    are the listing's; `newest` is the row's newest entry, `at` the
    detection time and `where` the "model via host" the notice names. An
    earlier entry (never the newest) whose rates the listing repeats
    within the last 7 days is the alternation; the notice tells a human to
    check for an unpublished time-of-day price, and that a genuine return
    to those rates is recorded by hand-appending an entry, which the next
    run then compares against.
    """
    if listing_schedule is not None or newest.get("schedule") is not None:
        return None
    cutoff = at - timedelta(days=7)
    for entry in history[:-1]:
        if ({f: entry[f] for f in RATE_FIELDS} != rates
                or entry["from"] is None):
            continue
        # pylint: disable-next=protected-access
        if pricing._instant(entry["from"], where) > cutoff:
            return (f"alternating price: {where}: the listed rates equal the entry "
                    f"from {entry['from']}: check for an unpublished time-of-day price; a "
                    "genuine return to those rates is recorded by hand-appending "
                    "an entry, which the next run then compares against")
    return None
