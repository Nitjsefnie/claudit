"""SV-RATE-REFRESH: an oscillating host's price, recorded once as its range.

A band entry carries the price range the host moved inside and the
time-weighted mean over the window that formed it; a listing inside the
range appends nothing. The shape classifier, the mean, the collapse and
the widening are exercised here over synthetic histories.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / "scripts" / "ci"
sys.path.insert(0, str(CI))


def _load():
    """Import scripts/ci/price_band.py by path."""
    path = CI / "price_band.py"
    spec = importlib.util.spec_from_file_location("price_band", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["price_band"] = module
    spec.loader.exec_module(module)
    return module


price_band = _load()

AT = datetime(2031, 1, 8, 0, 0, tzinfo=timezone.utc)
DAYS = 7
WINDOW = AT - timedelta(days=DAYS)

RATE_A = {"fresh": 0.30, "create_5m": 0.30, "create_1h": 0.30,
          "read": 0.010, "output": 0.800}
RATE_B = {"fresh": 0.20, "create_5m": 0.20, "create_1h": 0.20,
          "read": 0.020, "output": 0.700}
RATE_C = {"fresh": 0.40, "create_5m": 0.40, "create_1h": 0.40,
          "read": 0.030, "output": 0.900}


def _entry(at: str | None, rates: dict, **extra) -> dict:
    return {"from": at, **rates, **extra}


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _walk(states: list[dict], start: datetime = WINDOW) -> list[dict]:
    """A history stepping one day from `start` (the window opens by
    default), one entry per state."""
    return [_entry(_stamp(start + timedelta(days=i)), rates)
            for i, rates in enumerate(states)]


def _banded(*levels: dict) -> dict:
    """A band spanning the levels, per field."""
    return {field: [min(level[field] for level in levels),
                    max(level[field] for level in levels)]
            for field in ("fresh", "create_5m", "create_1h", "read", "output")}


# --- the band a single entry carries -----------------------------------------


def test_an_entry_without_a_band_has_none():
    assert price_band.entry_band(_entry(None, RATE_A)) is None


def test_a_banded_entry_reports_its_band():
    band = _banded(RATE_A, RATE_B, RATE_C)
    assert price_band.entry_band(_entry(None, RATE_A, band=band)) == band


@pytest.mark.parametrize("band", [
    "0.2..0.4",                                     # not a mapping
    {"fresh": 0.3},                                 # not a [min, max] pair
    {"fresh": [0.3, 0.2]},                          # min above max
    {"fresh": [0.2, "0.4"]},                        # not a number
    {"fresh": [0.2, True]},                         # a bool is not a rate
    {"fresh": [0.2, 4.0], "debt": [0, 1]},          # not a rate field
])
def test_a_malformed_band_is_refused(band):
    entry = _entry(None, RATE_A, band=band)
    with pytest.raises(ValueError, match="band"):
        price_band.entry_band(entry)


def test_a_missing_band_field_leaves_the_field_unconstrained():
    entry = _entry(None, RATE_A, band={"fresh": [0.2, 0.4]})
    assert price_band.in_band(entry, {**RATE_B, "output": 99.0})


def test_no_band_is_never_in_band():
    assert not price_band.in_band(_entry(None, RATE_A), RATE_A)


# --- in_band ------------------------------------------------------------------


@pytest.mark.parametrize("field,value,expected", [
    ("fresh", 0.20, True),      # exactly min
    ("fresh", 0.40, True),      # exactly max
    ("fresh", 0.19, False),
    ("fresh", 0.41, False),
    ("fresh", 0.30, True),      # strictly inside
    ("output", 0.70, True),
    ("output", 0.69, False),
])
def test_in_band_is_inclusive_at_both_ends(field, value, expected):
    entry = _entry(None, RATE_A, band=_banded(RATE_A, RATE_B, RATE_C))
    assert price_band.in_band(entry, {**RATE_B, field: value}) is expected


def test_every_field_must_lie_inside_its_own_range():
    entry = _entry(None, RATE_A, band=_banded(RATE_A, RATE_B, RATE_C))
    assert not price_band.in_band(entry, {**RATE_B, "output": 0.95})


# --- classify -----------------------------------------------------------------


def test_a_row_that_moved_once_is_stable():
    assert price_band.classify(_walk([RATE_A, RATE_B]), AT, DAYS)["shape"] \
        == "STABLE"


def test_a_row_that_never_returns_is_a_step():
    """Three levels, no return to an earlier one: a genuine repricing."""
    history = _walk([RATE_A, RATE_B, RATE_C])
    assert price_band.classify(history, AT, DAYS)["shape"] == "STEP"


def test_a_slow_repricing_fewer_than_once_a_day_is_a_step():
    """Many levels, but fewer moves than the window has days: a slow
    repricing, not an oscillation."""
    levels = [{**RATE_A, "fresh": 0.20 + 0.05 * i, "create_5m": 0.20 + 0.05 * i,
               "create_1h": 0.20 + 0.05 * i} for i in range(5)]
    verdict = price_band.classify(_walk(levels), AT, DAYS)
    assert verdict["shape"] == "STEP"
    assert verdict["distinct"] == 5
    assert verdict["returns"] == 0


def test_a_row_flipping_between_two_levels_is_a_toggle():
    history = _walk([RATE_A, RATE_B, RATE_A, RATE_B, RATE_A])
    verdict = price_band.classify(history, AT, DAYS)
    assert verdict["shape"] == "TOGGLE"
    assert verdict["changes"] == 4
    assert verdict["distinct"] == 2
    assert verdict["returns"] == 3


def test_a_row_moving_inside_a_wider_range_is_a_band():
    levels = [{**RATE_A, "fresh": 0.20 + 0.01 * i, "create_5m": 0.20 + 0.01 * i,
               "create_1h": 0.20 + 0.01 * i} for i in range(9)]
    verdict = price_band.classify(_walk(levels), AT, DAYS)
    assert verdict["shape"] == "BAND"
    assert verdict["distinct"] == 9
    assert verdict["returns"] == 0


def test_the_classification_window_is_measured_against_at():
    """The same history classifies TOGGLE against an instant inside its
    moves, and STABLE against one before them — never wall clock."""
    history = _walk([RATE_A, RATE_B, RATE_C, RATE_A],
                    start=AT - timedelta(days=3))
    assert price_band.classify(history, AT, DAYS)["shape"] == "TOGGLE"
    assert price_band.classify(history, AT + timedelta(days=30), DAYS)["shape"] \
        == "STABLE", "a month later those moves are outside the window"


def test_the_baseline_is_the_last_entry_before_the_window():
    """The first in-window entry is a change, because the entry it replaces
    is the baseline."""
    history = [_entry(_stamp(AT - timedelta(days=30)), RATE_A),
               _entry(_stamp(AT - timedelta(days=1)), RATE_B)]
    assert price_band.classify(history, AT, DAYS)["changes"] == 1


def test_a_row_with_no_dated_entry_is_stable():
    assert price_band.classify([_entry(None, RATE_A)], AT, DAYS)["shape"] == "STABLE"


# --- time_weighted ------------------------------------------------------------


def test_the_mean_weights_each_level_by_its_duration():
    """A holds six of the eight hours, B two: 6/8·A + 2/8·B."""
    history = [_entry(_stamp(AT - timedelta(hours=8)), RATE_A),
               _entry(_stamp(AT - timedelta(hours=2)), RATE_B)]
    assert price_band.time_weighted(history, AT) == {
        "fresh": 0.275, "create_5m": 0.275, "create_1h": 0.275,
        "read": 0.0125, "output": 0.775}


def test_a_leading_undated_entry_starts_at_the_first_dated_one():
    history = [_entry(None, RATE_C),
               _entry(_stamp(AT - timedelta(hours=4)), RATE_A),
               _entry(_stamp(AT - timedelta(hours=1)), RATE_B)]
    assert price_band.time_weighted(history, AT)["fresh"] == 0.275


def test_a_row_with_no_dated_entry_stands_as_it_is():
    assert price_band.time_weighted([_entry(None, RATE_A)], AT) == RATE_A


# --- collapse -----------------------------------------------------------------


def test_collapse_rewrites_the_row_to_one_banded_entry():
    history = [_entry("2030-06-01T00:00:00Z", RATE_B),
               _entry("2030-09-01T00:00:00Z", RATE_A),
               _entry("2030-12-01T00:00:00Z", RATE_C)]
    (entry,) = price_band.collapse(history, AT)
    assert entry["from"] == "2030-06-01T00:00:00Z", "the row's own start"
    assert entry["band"] == _banded(RATE_B, RATE_A, RATE_C)
    # 92 days at B, 91 at A, 38 at C.
    assert entry["fresh"] == 0.2755656109
    assert set(entry) == {"from", "fresh", "create_5m", "create_1h", "read",
                          "output", "band"}


def test_collapse_keeps_an_undated_start_undated():
    history = [_entry(None, RATE_B), _entry("2030-12-01T00:00:00Z", RATE_A)]
    (entry,) = price_band.collapse(history, AT)
    assert entry["from"] is None


def test_the_band_spans_the_whole_history_not_the_window():
    old = {**RATE_A, "fresh": 9.0, "create_5m": 9.0, "create_1h": 9.0}
    history = [_entry("2020-01-01T00:00:00Z", old), *_walk([RATE_B] * 3)]
    (entry,) = price_band.collapse(history, AT)
    assert entry["band"]["fresh"] == [0.2, 9.0]


def test_a_note_only_survives_when_every_entry_shares_it():
    shared = [_entry("2030-06-01T00:00:00Z", RATE_B, note="40% off"),
              _entry("2030-12-01T00:00:00Z", RATE_A, note="40% off")]
    (entry,) = price_band.collapse(shared, AT)
    assert entry["note"] == "40% off"

    mixed = [_entry("2030-06-01T00:00:00Z", RATE_B, note="40% off"),
             _entry("2030-12-01T00:00:00Z", RATE_A, note="45% off")]
    (entry,) = price_band.collapse(mixed, AT)
    assert "note" not in entry


def test_a_fee_note_the_mean_would_describe_wrongly_refuses_the_collapse():
    """A per-request fee is a real cost the mean cannot carry; dropping one
    silently is the failure issue #469 records."""
    history = [_entry("2030-06-01T00:00:00Z", RATE_B,
                      note="web_search $0.005/request not modelled: per-request, "
                           "unpriceable from token counts"),
               _entry("2030-12-01T00:00:00Z", RATE_A)]
    with pytest.raises(ValueError, match="fee"):
        price_band.collapse(history, AT)


def test_a_scheduled_row_is_refused_rather_than_priced_by_its_mean():
    history = [_entry("2030-06-01T00:00:00Z", RATE_B,
                      schedule=[{"days": ["sunday"], "rates": RATE_C}]),
               _entry("2030-12-01T00:00:00Z", RATE_A)]
    with pytest.raises(ValueError, match="schedule"):
        price_band.collapse(history, AT)


# --- widen --------------------------------------------------------------------


def test_widen_grows_the_band_to_cover_a_level_outside_it():
    entry = _entry("2030-12-01T00:00:00Z", RATE_A, band=_banded(RATE_A, RATE_B))
    outside = {**RATE_C, "fresh": 0.5, "create_5m": 0.5, "create_1h": 0.5}
    widened = price_band.widen(entry, outside, AT)
    assert widened["from"] == "2031-01-08T00:00:00Z"
    assert widened["band"]["fresh"] == [0.2, 0.5]
    assert widened["band"]["read"] == [0.01, 0.03]
    assert widened["band"]["output"] == [0.7, 0.9]
    assert widened["fresh"] == RATE_A["fresh"], "the priced level stands"


def test_widen_leaves_the_band_and_level_of_a_move_inside_it_unchanged():
    entry = _entry("2030-12-01T00:00:00Z", RATE_A,
                   band=_banded(RATE_A, RATE_B, RATE_C))
    widened = price_band.widen(entry, RATE_B, AT)
    assert widened["band"] == entry["band"]
    assert widened["fresh"] == RATE_A["fresh"]


def test_widen_keeps_the_row_s_note():
    entry = _entry("2030-12-01T00:00:00Z", RATE_A, note="40% off",
                   band=_banded(RATE_A, RATE_B, RATE_C))
    assert price_band.widen(entry, RATE_C, AT)["note"] == "40% off"
