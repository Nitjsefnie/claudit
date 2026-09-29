"""SV-RATE-REFRESH: first-seen scheduled hosts and whole-week coverage."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend import pricing
from tests.refresh_fixture_builders import _endpoint, _overrides, _per_token
from tests.test_provider_rate_refresh import (
    GLM, NEWCOMER, NOW, RATE_FIELDS, STAMP, V41, Run, refresh)

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday"]
_NO_SATURDAY = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday"]
_ANCHOR = datetime(2026, 1, 5, tzinfo=timezone.utc)
_WEEK_MINUTES = 7 * 24 * 60


def _listed(rates: dict) -> dict:
    return {"prompt": _per_token(rates["fresh"]),
            "completion": _per_token(rates["output"]),
            "input_cache_read": _per_token(rates["read"])}


def _scaled_rates(factor: float) -> dict:
    return {field: NEWCOMER[field] * factor for field in RATE_FIELDS}


def _full_week_schedule(shape: str) -> tuple[list[dict], dict]:
    """The synthetic shapes are those of hand-seeded Alibaba/DeepSeek rows."""
    if shape == "time-split":
        schedule = [
            {"start": 0, "end": 1400, "rates": _scaled_rates(0.5)},
            {"start": 1400, "end": 0, "rates": _scaled_rates(1.5)},
        ]
        return schedule, schedule[0]["rates"]
    if shape == "day-partitioned":
        schedule = [
            {"days": ["saturday", "sunday"], "rates": _scaled_rates(1.75)},
            {"days": _WEEKDAYS, "start": 0, "end": 100,
             "rates": _scaled_rates(0.5)},
            {"days": _WEEKDAYS, "start": 100, "end": 400,
             "rates": _scaled_rates(0.75)},
            {"days": _WEEKDAYS, "start": 400, "end": 600,
             "rates": _scaled_rates(1.0)},
            {"days": _WEEKDAYS, "start": 600, "end": 1000,
             "rates": _scaled_rates(1.25)},
            {"days": _WEEKDAYS, "start": 1000, "end": 0,
             "rates": _scaled_rates(1.5)},
        ]
        return schedule, schedule[1]["rates"]
    raise ValueError(f"unknown schedule shape: {shape}")


def _add_first_seen(run: Run, schedule: list[dict], listed_rates: dict) -> None:
    endpoint = _endpoint("Newcomer", listed_rates)
    endpoint["pricing"]["overrides"] = _overrides(schedule)
    endpoint["pricing"].update(_listed(listed_rates))
    run.endpoints(GLM).append(endpoint)


@pytest.mark.parametrize(
    "shape",
    [pytest.param("time-split", id="alibaba-time-split-shape"),
     pytest.param("day-partitioned", id="deepseek-day-partitioned-shape")],
)
def test_deepseek_v4_pro_0813_hand_seeded_shapes_start_without_manual_seeding(
        tmp_path: Path, capsys: pytest.CaptureFixture[str], shape: str) -> None:
    """The synthetic hand-seeded Alibaba/DeepSeek shapes seed without hand edits."""
    run = Run(tmp_path)
    schedule, active_rates = _full_week_schedule(shape)
    _add_first_seen(run, schedule, active_rates)
    other = _endpoint("OtherNewcomer", NEWCOMER)
    run.endpoints(V41).append(other)
    source_doc = run.doc()

    rc, out, err = run(capsys, now=NOW)

    assert rc == 0, err
    assert not err
    assert run.doc()["providers"][GLM]["Newcomer"] == [
        {"from": STAMP, **active_rates, "schedule": schedule}]
    assert run.doc()["providers"][V41]["OtherNewcomer"] == [
        {"from": STAMP, **NEWCOMER}]
    assert (
        f"{GLM} via Newcomer: first seen inside one of its windows, whose schedule "
        "covers the whole week: no record is ever priced by the entry default, so "
        "the default starts as the listed top-level price") in out + err

    def fetch(model_id: str) -> object:
        return run.payloads[model_id]

    result = refresh.refresh(source_doc, fetch, STAMP)
    move = next(move for move in result.moves
                if move.model == GLM and move.host == "Newcomer")
    assert move.old is None
    assert move.new.schedule == schedule


def test_first_seen_host_with_a_weekly_gap_is_still_refused(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run = Run(tmp_path)
    schedule = [{"days": _WEEKDAYS, "start": 0, "end": 100,
                 "rates": _scaled_rates(0.5)}]
    _add_first_seen(run, schedule, schedule[0]["rates"])

    rc, _, err = run(capsys, now=NOW)

    assert rc != 0
    assert f"{GLM} via Newcomer" in err
    assert "first seen inside one of its windows" in err
    assert "Newcomer" not in run.doc()["providers"][GLM]


@pytest.mark.parametrize("shape", ["time-split", "day-partitioned"])
def test_a_seeded_default_cannot_price_any_minute_of_the_week(
        tmp_path: Path, capsys: pytest.CaptureFixture[str], shape: str) -> None:
    run = Run(tmp_path)
    schedule, active_rates = _full_week_schedule(shape)
    _add_first_seen(run, schedule, active_rates)

    rc, _, err = run(capsys, now=NOW)

    assert rc == 0, err
    entry = run.doc()["providers"][GLM]["Newcomer"][0]
    assert {field: entry[field] for field in RATE_FIELDS} == active_rates
    windows = pricing._schedule(entry["schedule"], "schedule")  # pylint: disable=protected-access
    for minute in range(_WEEK_MINUTES):
        at = _ANCHOR + timedelta(minutes=minute)
        assert pricing._scheduled(windows, at) is not None, minute  # pylint: disable=protected-access


@pytest.mark.parametrize(
    ("schedule", "expected"),
    [
        pytest.param([{"rates": _scaled_rates(1)}], True, id="whole-day-window"),
        pytest.param(_full_week_schedule("time-split")[0], True, id="time-split-week"),
        pytest.param(_full_week_schedule("day-partitioned")[0], True,
                     id="day-partitioned-week"),
        pytest.param([{"days": _WEEKDAYS, "rates": _scaled_rates(1)}], False,
                     id="weekdays-only"),
        pytest.param([{"start": 0, "end": 2359, "rates": _scaled_rates(1)}], False,
                     id="end-exclusive-last-minute"),
        pytest.param([{"days": _NO_SATURDAY, "rates": _scaled_rates(1)}], False,
                     id="no-saturday-window"),
        pytest.param([{"start": 1400, "end": 1401, "rates": _scaled_rates(1)}], False,
                     id="one-minute-window"),
    ],
)
def test_covers_week_checks_every_week_minute(schedule: list, expected: bool) -> None:
    assert refresh.refresh_prices.covers_week(schedule) is expected


def test_first_seen_override_with_explicit_null_days_stores_days_absent(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run = Run(tmp_path)
    rates = _scaled_rates(0.5)
    endpoint = _endpoint("NullDays", rates)
    endpoint["pricing"]["overrides"] = [{"utc_days": None, **_listed(rates)}]
    run.endpoints(GLM).append(endpoint)

    rc, _, err = run(capsys, now=NOW)

    assert rc == 0, err
    history = run.doc()["providers"][GLM]["NullDays"]
    assert history[0]["from"] == STAMP
    window = history[0]["schedule"][0]
    assert "days" not in window


def test_first_seen_override_with_null_time_bounds_stores_bounds_absent(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run = Run(tmp_path)
    rates = _scaled_rates(0.5)
    endpoint = _endpoint("NullTimes", rates)
    endpoint["pricing"]["overrides"] = [
        {"utc_start": None, "utc_end": None, **_listed(rates)}]
    run.endpoints(GLM).append(endpoint)

    rc, _, err = run(capsys, now=NOW)

    assert rc == 0, err
    history = run.doc()["providers"][GLM]["NullTimes"]
    assert history[0]["from"] == STAMP
    window = history[0]["schedule"][0]
    assert "start" not in window
    assert "end" not in window
