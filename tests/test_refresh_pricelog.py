"""Synthetic tests for OpenRouter's per-endpoint listed-pricing log."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from tests.refresh_fixture_builders import _per_token

# Pylint cannot follow this test-only path injection to the script's return
# annotations; pyright and the executed assertions validate the list access.
# pylint: disable=unsubscriptable-object,not-an-iterable
ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / "scripts" / "ci"
sys.path.insert(0, str(CI))


def _load():
    """Import scripts/ci/refresh_pricelog.py by path."""
    path = CI / "refresh_pricelog.py"
    spec = importlib.util.spec_from_file_location("refresh_pricelog", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["refresh_pricelog"] = module
    spec.loader.exec_module(module)
    return module


pricelog = _load()


def _point(at: str, value: float | None) -> dict:
    return {"at": at, "value": value}


def _series(*, slug: str = "wafer", host: str = "Wafer",
            rates: dict | None = None, schedule: list | None = None) -> dict:
    current = rates or {"fresh": 0.3, "create_5m": 0.3,
                        "create_1h": 0.3, "read": 0.01, "output": 0.8}
    series = {
        "endpointId": f"synthetic-{slug}", "providerName": host,
        "providerSlug": slug,
        "input": [_point("2026-09-01T00:00:00Z", current["fresh"])],
        "output": [_point("2026-09-01T00:00:00Z", current["output"])],
        "cacheRead": [_point("2026-09-01T00:00:00Z", current["read"])],
        "cacheWrite": [_point("2026-09-01T00:00:00Z", current["create_5m"]
                              if current["create_5m"] != current["fresh"] else 0)],
        "discount": [],
    }
    if schedule is not None:
        series["schedule"] = schedule
    return series


def _payload(*series: dict) -> dict:
    return {"data": {"series": list(series)}}


def _rates(fresh: float, output: float, read: float = 0,
           write: float | None = None) -> dict:
    create = fresh if write is None else write
    return {"fresh": fresh, "create_5m": create, "create_1h": create,
            "read": read, "output": output}


def _endpoint(host: str, tag: str, rates: dict, *, scheduled: bool = False) -> dict:
    pricing = {
        "prompt": _per_token(rates["fresh"]),
        "completion": _per_token(rates["output"]),
        "input_cache_read": _per_token(rates["read"]),
        "discount": 0,
    }
    if rates["create_5m"] != rates["fresh"]:
        pricing["input_cache_write"] = _per_token(rates["create_5m"])
    if scheduled:
        pricing["overrides"] = [{"utc_days": ["monday"]}]
    return {"provider_name": host, "tag": tag, "pricing": pricing}


def _joined(endpoints: list[dict], series: list[dict], *, region: str | None = None,
            resolutions: dict | None = None) -> dict:
    parsed = pricelog.read_log_payload(_payload(*series))
    listing = {"data": {"endpoints": endpoints}}
    return pricelog.join_listed_pricing(listing, parsed, region, resolutions or {})


def _entries(item: dict) -> list[dict]:
    entries = pricelog.entries_for_series(pricelog.read_log_payload(_payload(item))[0])
    if entries is None:
        raise AssertionError("synthetic series should have a complete state")
    return entries


def test_a_series_becomes_change_entries_with_rounding_and_discount_notes():
    item = _series()
    item["input"] = [
        _point("2026-09-01T00:00:00.100Z", 0.1),
        _point("2026-09-01T00:00:01.100Z", 0.2),
        _point("2026-09-01T00:00:03Z", 0.3),
    ]
    item["output"] = [
        _point("2026-09-01T00:00:00.500Z", 0.5),
        _point("2026-09-01T00:00:00.800Z", 0.6000000000000001),
        _point("2026-09-01T00:00:01.100Z", 0.6000000000000001),
        _point("2026-09-01T00:00:03Z", 0.6000000000000001),
    ]
    item["cacheRead"] = [
        _point("2026-09-01T00:00:00.500Z", None),
        _point("2026-09-01T00:00:01Z", 0.01),
    ]
    item["cacheWrite"] = [
        _point("2026-09-01T00:00:00.500Z", None),
        _point("2026-09-01T00:00:01Z", 0),
        _point("2026-09-01T00:00:03Z", 0.05000000000000001),
    ]
    item["discount"] = [
        _point("2026-09-01T00:00:00.500Z", 0.1),
        _point("2026-09-01T00:00:01Z", 0.25),
        _point("2026-09-01T00:00:02Z", 0.5),
    ]

    entries: list[dict] = _entries(item)

    assert len(entries) == 3
    assert [entry["from"] for entry in entries] == [
        "2026-09-01T00:00:00Z", "2026-09-01T00:00:01Z", "2026-09-01T00:00:03Z"]
    assert {key: entries[0][key] for key in ("fresh", "create_5m", "create_1h",
                                             "read", "output")} == _rates(0.1, 0.6)
    assert entries[0]["note"] == "10% off"
    assert entries[1]["note"] == "25% off"
    assert entries[2]["note"] == "50% off"
    assert entries[2]["create_5m"] == 0.05
    assert entries[2]["create_1h"] == 0.05
    assert entries[1]["read"] == 0.01


def test_generated_log_entry_timestamp_zero_pads_the_year():
    item = _series()
    for field in ("input", "output", "cacheRead", "cacheWrite"):
        item[field] = [_point("0001-01-01T00:00:00Z", item[field][0]["value"])]

    entries = _entries(item)

    assert entries
    assert entries[0]["from"] == "0001-01-01T00:00:00Z"


def test_a_series_waits_until_input_and_output_both_exist():
    item = _series()
    item["input"] = [_point("2026-09-01T00:00:00Z", 0.2)]
    item["output"] = [_point("2026-09-01T00:00:03Z", 0.7)]

    entries: list[dict] = _entries(item)

    assert len(entries) == 1
    assert entries[0]["from"] == "2026-09-01T00:00:03Z"
    assert entries[0]["fresh"] == 0.2
    assert entries[0]["output"] == 0.7


@pytest.mark.parametrize("field", ["input", "output"])
def test_null_input_or_output_makes_a_series_unusable(field: str):
    item = _series()
    item[field].append(_point("2026-09-02T00:00:00Z", None))

    assert pricelog.entries_for_series(pricelog.read_log_payload(_payload(item))[0]) is None


@pytest.mark.parametrize(
    "damage",
    ["missing-data", "series-not-list", "bad-at", "unordered", "negative",
     "nonfinite", "wrong-type"],
)
def test_unrecognised_log_payload_is_unavailable_with_a_reason(damage: str):
    valid = _payload(_series())
    if damage == "missing-data":
        payload = {}
    elif damage == "series-not-list":
        payload = {"data": {"series": {}}}
    else:
        bad_series = _series()
        if damage == "bad-at":
            bad_series["input"][0]["at"] = "2026-09-01T00:00:00+00:00"
        elif damage == "unordered":
            bad_series["input"].append(_point("2026-08-31T23:59:59Z", 0.2))
        elif damage == "negative":
            bad_series["input"][0]["value"] = -0.1
        elif damage == "nonfinite":
            bad_series["input"][0]["value"] = float("inf")
        else:
            bad_series["providerSlug"] = 7
        payload = _payload(bad_series)
    assert valid["data"]["series"]

    result = pricelog.read_logs(
        {"synthetic/model": {"id": "synthetic/model"}},
        lambda: {"data": [{"id": "synthetic/model", "canonical_slug": "synthetic/canonical"}]},
        lambda slug: payload,
    )["synthetic/model"]

    assert result.series is None
    assert result.reason


def test_one_endpoint_with_one_matching_series_is_log_backed():
    rates = _rates(0.3, 0.8, 0.01)
    match = _joined([_endpoint("Wafer", "wafer/fp8", rates)],
                    [_series(rates=rates)])

    assert match["Wafer"].entries is not None
    assert len(match["Wafer"].entries) == 1


def test_region_filter_selects_one_of_two_distinct_endpoint_histories():
    us_rates, eu_rates = _rates(0.3, 0.8), _rates(0.4, 0.9)
    match = _joined(
        [_endpoint("Wafer", "wafer/us", us_rates),
         _endpoint("Wafer", "wafer/eu", eu_rates)],
        [_series(rates=us_rates), _series(slug="wafer", rates=eu_rates)], region="us")

    assert match["Wafer"].entries is not None
    assert match["Wafer"].entries[0]["fresh"] == 0.3


def test_two_endpoints_at_the_same_price_are_sampled_as_ambiguous():
    rates = _rates(0.3, 0.8)
    match = _joined([_endpoint("Wafer", "wafer/fp8", rates),
                     _endpoint("Wafer", "wafer/fp16", rates)],
                    [_series(rates=rates), _series(rates=rates)])

    assert match["Wafer"].entries is None
    assert match["Wafer"].reason


def test_a_series_without_a_matching_listed_endpoint_is_sampled():
    listed, unlisted = _rates(0.3, 0.8), _rates(0.9, 1.1)
    match = _joined([_endpoint("Wafer", "wafer/fp8", listed)],
                    [_series(rates=unlisted)])

    assert match["Wafer"].entries is None
    assert match["Wafer"].reason


@pytest.mark.parametrize("scheduled", ["endpoint", "series"])
def test_scheduled_endpoint_or_series_is_sampled(scheduled: str):
    rates = _rates(0.3, 0.8)
    item = _series(rates=rates, schedule=[{"windows": []}] if scheduled == "series" else None)
    endpoints = [_endpoint("Wafer", "wafer/fp8", rates, scheduled=scheduled == "endpoint")]

    match = _joined(endpoints, [item])

    assert match["Wafer"].entries is None
    assert match["Wafer"].reason


@pytest.mark.parametrize("pin", [
    {"select": "cheapest"},
    {"tag": "wafer/fp8", "select": "cheapest"},
], ids=["bare", "tag-and-cheapest"])
def test_cheapest_resolution_is_always_sampled(pin):
    rates = _rates(0.3, 0.8)
    match = _joined([_endpoint("Wafer", "wafer/fp8", rates),
                     _endpoint("Wafer", "wafer/fp8", _rates(0.4, 0.9))],
                    [_series(rates=rates), _series(rates=_rates(0.4, 0.9))],
                    resolutions={"Wafer": pin})

    assert match["Wafer"].entries is None
    assert match["Wafer"].reason


def test_tag_pin_selects_one_endpoint_for_log_backed_history():
    rates, other = _rates(0.3, 0.8), _rates(0.4, 0.9)
    match = _joined([_endpoint("Wafer", "wafer/fp8", rates),
                     _endpoint("Wafer", "wafer/fp16", other)],
                    [_series(rates=rates), _series(rates=other)],
                    resolutions={"Wafer": {"tag": "wafer/fp8"}})

    assert match["Wafer"].entries is not None
    assert match["Wafer"].entries[0]["fresh"] == 0.3
