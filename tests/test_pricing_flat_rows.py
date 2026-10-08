"""A row without a dated window prices flat across time, split from
test_pricing.py to keep that module under its size ceiling — relocation
only, no assertion changed. The property is the ROW's shape, so it is
driven through a synthetic row (SV-TEST-DATA)."""
from __future__ import annotations

from datetime import datetime, timezone

from backend import pricing

UTC = timezone.utc

# --- rows without a dated window price flat across time ---------------------
# Sonnet 5's launch price was announced as introductory through
# 2026-08-31, but it was made the standard price and the 2026-09-01 rise
# was cancelled — there is no cutover in the row. That is a property of
# the ROW's shape, so the behaviour is driven through a synthetic row
# (SV-TEST-DATA) instead of pinning sonnet 5's committed values.

UTC = timezone.utc

_FLAT_RATES = {"fresh": 3.21, "create_5m": 4.01, "create_1h": 6.42,
               "read": 0.32, "output": 16.1}


def test_a_row_without_a_dated_window_prices_flat_across_time(monkeypatch):
    rates = dict(_FLAT_RATES)
    monkeypatch.setitem(pricing.MODEL_RATES, "acme/flat-9", rates)
    for ts in (None, datetime(2026, 7, 21, tzinfo=UTC),
               datetime(2026, 9, 1, tzinfo=UTC),
               datetime(2027, 1, 1, tzinfo=UTC)):
        assert pricing.rate_for("acme/flat-9", ts=ts) is rates


def test_a_row_without_a_dated_window_costs_the_same_at_any_time(monkeypatch):
    rates = dict(_FLAT_RATES)
    monkeypatch.setitem(pricing.MODEL_RATES, "acme/flat-9", rates)
    costs = [pricing.compute_cost(
        "acme/flat-9", fresh=1_000_000, output=0, eph5=0, eph1h=0,
        unsplit_create=0, read=0, ts=ts,
    ) for ts in (None, datetime(2026, 7, 21, tzinfo=UTC),
                 datetime(2026, 9, 1, tzinfo=UTC),
                 datetime(2027, 1, 1, tzinfo=UTC))]
    assert costs == [rates["fresh"]] * 4
