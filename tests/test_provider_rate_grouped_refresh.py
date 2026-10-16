"""Backfilled timestamp groups survive every hourly append path."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from backend import pricing
from backend.pricing_document import expand_pricing_doc, serialize_pricing_doc
from tests.refresh_fixture_builders import _endpoint, _overrides
from tests.test_provider_rate_backfill import (
    MODEL, RATE_A, RATE_B, RATE_C, BackfillFetches, _entry, _run, _series, backfill,
)

DETECTED = "2031-01-01T00:40:00Z"
AT = datetime(2031, 1, 1, 0, 40, tzinfo=timezone.utc)


def _backfilled(tmp_path, capsys, last_group):
    states = [("2031-01-01T00:00:00Z", RATE_A),
              ("2031-01-01T00:10:00Z", RATE_B),
              ("2031-01-01T00:20:00Z", RATE_A)]
    if last_group:
        states.append(("2031-01-01T00:22:00Z", RATE_B))
    rc, _, err, path, constants, _ = _run(
        tmp_path, capsys, {"Wafer": [_entry(states[0][0], RATE_C)],
                           "OtherHost": [_entry(None, RATE_B)]},
        states, as_of="2031-01-01T00:25:00Z")
    assert rc == 0, err
    grouped = expand_pricing_doc(json.loads(path.read_text()))["providers"][MODEL]["Wafer"]
    assert grouped[0]["from"] == [states[0][0], states[2][0]]
    if last_group:
        assert isinstance(grouped[-1]["from"], list)
    return states, path, constants, grouped


def _listing(states, mode):
    listed = {**(RATE_C if mode in {"logged", "sampled"} else states[-1][1])}
    log_states = [*states, ("2031-01-01T00:30:00Z", RATE_C)] if mode == "logged" else states
    if mode == "search":
        listed["web_search"] = 0.03
    endpoint = _endpoint("Wafer", listed, tag="wafer/fp8")
    schedule = [{"days": ["monday"], "rates": RATE_B}]
    if mode == "schedule":
        endpoint["pricing"]["overrides"] = _overrides(schedule)
    fetches = BackfillFetches(
        [endpoint, _endpoint("OtherHost", RATE_C, tag="other/fp8")],
        {"data": {"series": [_series("Wafer", "wafer", log_states)]}})
    return fetches, listed, schedule


@pytest.mark.parametrize("last_group", [False, True])
@pytest.mark.parametrize("mode", ["quiet", "logged", "search", "sampled", "schedule"])
def test_backfill_serialize_refresh_preserves_groups_with_independent_move(
        tmp_path, capsys, last_group, mode):
    states, path, constants, grouped = _backfilled(tmp_path, capsys, last_group)
    fetches, listed, schedule = _listing(states, mode)
    rc = backfill.hourly.main(
        [], fetch=fetches.fetch_endpoints, fetch_models=fetches.fetch_models,
        fetch_log=fetches.fetch_log, now=AT, pricing_path=path,
        constants_path=constants, vendor=None)
    _, err = capsys.readouterr()
    assert rc == 0, err
    saved = expand_pricing_doc(json.loads(path.read_text()))
    history = saved["providers"][MODEL]["Wafer"]
    assert history[:len(grouped)] == grouped
    assert history[len(grouped):] == ([] if mode == "quiet" else [{
        "from": "2031-01-01T00:30:00Z" if mode == "logged" else DETECTED,
        **listed, **({"schedule": schedule} if mode == "schedule" else {})}])
    assert saved["providers"][MODEL]["OtherHost"] == [
        _entry(None, RATE_B), _entry(DETECTED, RATE_C)]
    assert path.read_text() == serialize_pricing_doc(saved)
    pricing.load_tables(json.loads(path.read_text()))
