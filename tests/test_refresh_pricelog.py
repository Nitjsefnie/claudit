"""Synthetic tests for OpenRouter's per-endpoint listed-pricing log."""
from __future__ import annotations

import email.message
import http.client
import importlib.util
import io
import json
import sys
import urllib.error
from datetime import datetime, timezone
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


def _load_logshape():
    """Import scripts/ci/refresh_logshape.py by path."""
    path = CI / "refresh_logshape.py"
    spec = importlib.util.spec_from_file_location("refresh_logshape", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["refresh_logshape"] = module
    spec.loader.exec_module(module)
    return module


pricelog = _load()
logshape = _load_logshape()


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


def _endpoint(host: str, tag: str, rates: dict, *, scheduled: bool = False,
              fee: str | None = None) -> dict:
    pricing = {
        "prompt": _per_token(rates["fresh"]),
        "completion": _per_token(rates["output"]),
        "input_cache_read": _per_token(rates["read"]),
        "discount": 0,
    }
    if rates["create_5m"] != rates["fresh"]:
        pricing["input_cache_write"] = _per_token(rates["create_5m"])
    if fee is not None:
        pricing["web_search"] = fee
    if scheduled:
        pricing["overrides"] = [{"utc_days": ["monday"]}]
    return {"provider_name": host, "tag": tag, "pricing": pricing}


# After every fixture series point, so the default join reads each series whole.
_FETCH_AT = datetime(2026, 9, 15, tzinfo=timezone.utc)


def _joined(endpoints: list[dict], series: list[dict], *, region: str | None = None,
            resolutions: dict | None = None,
            at: datetime | None = None) -> dict:
    parsed = logshape.read_log_payload(_payload(*series))
    listing = {"data": {"endpoints": endpoints}}
    return pricelog.join_listed_pricing(
        listing, parsed, region, resolutions or {}, at or _FETCH_AT)


def _entries(item: dict) -> list[dict]:
    entries = pricelog.entries_for_series(logshape.read_log_payload(_payload(item))[0])
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

    assert pricelog.entries_for_series(logshape.read_log_payload(_payload(item))[0]) is None


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

    logs, catalog = pricelog.read_logs(
        {"synthetic/model": {"id": "synthetic/model"}},
        lambda: {"data": [{"id": "synthetic/model", "canonical_slug": "synthetic/canonical"}]},
        lambda slug: payload,
    )

    assert logs["synthetic/model"].series is None
    assert logs["synthetic/model"].reason
    assert catalog == {"synthetic/model"}, "a damaged log still leaves the catalog readable"


def test_a_raising_models_fetch_leaves_the_catalog_unreadable():
    def _raising():
        raise RuntimeError("boom")

    logs, catalog = pricelog.read_logs(
        {"synthetic/model": {"id": "synthetic/model"}},
        _raising,
        lambda slug: {},
    )

    assert catalog is None, "a failed catalog fetch must refuse, not delist"
    assert logs["synthetic/model"].series is None
    assert logs["synthetic/model"].reason


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


def test_a_host_listing_a_recorded_fee_is_sampled_not_log_backed():
    """The fee note is written by the sampled append path only. A host that
    listed a per-request fee and was log-backed anyway would carry no note
    at all — the silent drop the recorded-fee rule exists to prevent, on
    the other append path. So a fee routes the host to sampling, where the
    note is written, exactly as a split 1h tier already does.
    """
    rates = _rates(0.3, 0.8, 0.01)
    match = _joined([_endpoint("Wafer", "wafer/fp8", rates, fee="0.01")],
                    [_series(rates=rates)])

    assert match["Wafer"].entries is None
    assert "fee" in match["Wafer"].reason


def test_a_free_fee_leaves_the_host_log_backed():
    rates = _rates(0.3, 0.8, 0.01)
    match = _joined([_endpoint("Wafer", "wafer/fp8", rates, fee="0")],
                    [_series(rates=rates)])

    assert match["Wafer"].entries is not None


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


def test_the_sampled_reason_names_band_or_schedule():
    """The overrides' sampled reason names what classified the host: a
    min_prompt_tokens band is not a schedule (issue #851's rewrite; the log
    path reads overrides' presence only, and either kind samples)."""
    rates = _rates(0.3, 0.8)
    banded = _endpoint("Wafer", "wafer/fp8", rates)
    banded["pricing"]["overrides"] = [
        {"min_prompt_tokens": 200000, "prompt": _per_token(0.6),
         "completion": _per_token(1.2)}]
    scheduled = _endpoint("Wafer", "wafer/fp8", rates, scheduled=True)
    mixed = _endpoint("Wafer", "wafer/fp8", rates)
    mixed["pricing"]["overrides"] = [
        {"utc_days": ["monday"], "prompt": _per_token(0.15)},
        {"min_prompt_tokens": 200000, "prompt": _per_token(0.6),
         "completion": _per_token(1.2)}]
    mixed_band_first = _endpoint("Wafer", "wafer/fp8", rates)
    mixed_band_first["pricing"]["overrides"] = [
        {"min_prompt_tokens": 200000, "prompt": _per_token(0.6),
         "completion": _per_token(1.2)},
        {"utc_days": ["monday"], "prompt": _per_token(0.15)}]

    assert _joined([banded], [_series(rates=rates)])["Wafer"].reason == (
        "endpoint lists a long-context band")
    assert _joined([scheduled], [_series(rates=rates)])["Wafer"].reason == (
        "endpoint has a pricing schedule")
    # Order-independent: a utc window is the stronger blocker whichever
    # position it sits in.
    assert _joined([mixed], [_series(rates=rates)])["Wafer"].reason == (
        "endpoint has a pricing schedule")
    assert _joined([mixed_band_first], [_series(rates=rates)])["Wafer"].reason == (
        "endpoint has a pricing schedule")


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


def _with_future_point(item: dict, announced: dict) -> dict:
    for field, value in (("input", announced["fresh"]),
                         ("output", announced["output"]),
                         ("cacheRead", announced["read"]), ("cacheWrite", 0)):
        item[field].append(_point("2099-01-01T00:00:00Z", value))
    return item


def test_a_point_after_the_fetch_instant_is_dropped_for_that_run():
    listed, announced = _rates(0.3, 0.8), _rates(0.2, 0.7)
    item = _with_future_point(_series(rates=listed), announced)

    match = _joined([_endpoint("Wafer", "wafer/fp8", listed)], [item],
                    at=datetime(2026, 9, 16, tzinfo=timezone.utc))

    entries = match["Wafer"].entries
    assert entries is not None
    assert [entry["from"] for entry in entries] == ["2026-09-01T00:00:00Z"]


def test_a_listing_at_the_series_future_state_matches_no_in_force_state():
    listed, announced = _rates(0.3, 0.8), _rates(0.2, 0.7)
    item = _with_future_point(_series(rates=listed), announced)

    match = _joined([_endpoint("Wafer", "wafer/fp8", announced)], [item],
                    at=datetime(2026, 9, 16, tzinfo=timezone.utc))

    assert match["Wafer"].entries is None
    assert "no in-force log state matches the listing" in match["Wafer"].reason


def _add_state(item: dict, at: str, rates: dict) -> dict:
    for field, value in (("input", rates["fresh"]),
                         ("output", rates["output"]),
                         ("cacheRead", rates["read"]),
                         ("cacheWrite", 0 if rates["create_5m"] == rates["fresh"]
                          else rates["create_5m"])):
        item[field].append(_point(at, value))
    return item


def test_a_listing_matching_an_earlier_log_state_names_the_lag_class():
    listed, newer = _rates(0.3, 0.8), _rates(0.2, 0.7)
    item = _add_state(_series(rates=listed), "2026-09-02T00:00:00Z", newer)

    match = _joined([_endpoint("Wafer", "wafer/fp8", listed)], [item],
                    at=datetime(2026, 9, 16, tzinfo=timezone.utc))

    assert match["Wafer"].entries is None
    assert "the listing lags the price log" in match["Wafer"].reason


def test_earlier_state_vectors_holds_every_state_before_the_newest():
    listed, newer = _rates(0.3, 0.8), _rates(0.2, 0.7)
    item = _add_state(_series(rates=listed), "2026-09-02T00:00:00Z", newer)
    series = logshape.read_log_payload(_payload(item))[0]

    earlier = pricelog._earlier_state_vectors(series)  # pylint: disable=protected-access

    assert earlier == {(0.3, 0.3, 0.3, 0.0, 0.8)}


def test_a_series_entirely_after_the_fetch_instant_has_no_state_in_force():
    announced = _rates(0.2, 0.7)
    item = _series(rates=announced)
    for field in ("input", "output", "cacheRead", "cacheWrite"):
        item[field] = [_point("2099-01-01T00:00:00Z", item[field][0]["value"])]

    match = _joined([_endpoint("Wafer", "wafer/fp8", announced)], [item],
                    at=datetime(2026, 9, 16, tzinfo=timezone.utc))

    assert match["Wafer"].entries is None
    assert match["Wafer"].reason


# --- _fetch_json's bounded retry over transient fetch failures (issue #760)


def _fake_open(outcomes: list):
    """An _open seam whose outcomes are consumed in order; an Exception
    outcome is raised, a bytes outcome is served as the JSON body."""
    opened = []

    def _open(url):
        opened.append(url)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return io.BytesIO(outcome)

    return _open, opened


def _fetch_with(monkeypatch, outcomes: list, sleeps: list) -> object:
    monkeypatch.setattr(pricelog, "_open", _fake_open(outcomes)[0])
    monkeypatch.setattr(pricelog, "_sleep", sleeps.append)
    return pricelog._fetch_json(pricelog.MODELS_URL)  # pylint: disable=protected-access


def test_one_catalog_timeout_is_retried_so_the_delisted_skip_survives(monkeypatch):
    """Issue #760: a single read timeout on the models read must not leave
    the catalog unreadable — an unreadable catalog proves nothing, so the
    empty-endpoints refusal stood and the run went red although the model
    was delisted all along."""
    catalog = {"data": [{"id": "synth/model", "canonical_slug": "synth/canonical"}]}
    sleeps: list[float] = []

    logs, catalog_set = pricelog.read_logs(
        {"synthetic/model": {"id": "synth/model"}},
        lambda: _fetch_with(monkeypatch, [TimeoutError("The read operation timed out"),
                                          json.dumps(catalog).encode()], sleeps),
        lambda slug: {},
    )

    assert catalog_set == {"synth/model"}, "one timeout must not leave the catalog unreadable"
    assert logs["synthetic/model"].series is None
    assert sleeps == [pricelog.FETCH_BACKOFF_S], "the retry backs off once, not forever"


def test_a_persistently_failing_fetch_raises_after_bounded_attempts(monkeypatch):
    """The retry changes only how many tries 'unreadable' takes: a fetch
    that fails every attempt still raises."""
    sleeps: list[float] = []
    monkeypatch.setattr(pricelog, "_open", _fake_open(
        [TimeoutError("The read operation timed out")] * 3)[0])
    monkeypatch.setattr(pricelog, "_sleep", sleeps.append)

    with pytest.raises(TimeoutError):
        pricelog._fetch_json(pricelog.MODELS_URL)  # pylint: disable=protected-access

    assert sleeps == [pricelog.FETCH_BACKOFF_S, pricelog.FETCH_BACKOFF_S * 2]


def test_a_client_error_is_never_retried(monkeypatch):
    """A 4xx is the server's answer to the request as sent — a retry asks
    the identical question and gets the identical answer."""
    sleeps: list[float] = []
    _open, opened = _fake_open(
        [urllib.error.HTTPError(pricelog.MODELS_URL, 404, "not found",
                                email.message.Message(), None)])
    monkeypatch.setattr(pricelog, "_open", _open)
    monkeypatch.setattr(pricelog, "_sleep", sleeps.append)

    with pytest.raises(urllib.error.HTTPError):
        pricelog._fetch_json(pricelog.MODELS_URL)  # pylint: disable=protected-access

    assert len(opened) == 1
    assert not sleeps


def test_a_server_error_gets_its_retry(monkeypatch):
    sleeps: list[float] = []

    payload = _fetch_with(
        monkeypatch,
        [urllib.error.HTTPError(pricelog.MODELS_URL, 503, "unavailable",
                                email.message.Message(), None),
         b'{"data": []}'],
        sleeps)

    assert payload == {"data": []}
    assert sleeps == [pricelog.FETCH_BACKOFF_S]


def test_the_final_attempt_still_returns_instead_of_raising(monkeypatch):
    """Both sides of the attempt boundary: exhaustion raises after
    FETCH_ATTEMPTS - 1 backs off, and a success ON the last attempt is
    returned, not mistaken for one failure too many."""
    sleeps: list[float] = []

    payload = _fetch_with(
        monkeypatch,
        [TimeoutError("The read operation timed out"),
         TimeoutError("The read operation timed out"),
         b'{"data": []}'],
        sleeps)

    assert payload == {"data": []}
    assert sleeps == [pricelog.FETCH_BACKOFF_S, pricelog.FETCH_BACKOFF_S * 2]


def test_a_rate_limit_answer_gets_its_retry(monkeypatch):
    """429 is the server asking to try again, the same side of the
    classification as a 5xx; its conjunct is pinned separately so a dropped
    `or exc.code == 429` fails this test instead of reading as 5xx coverage."""
    sleeps: list[float] = []

    payload = _fetch_with(
        monkeypatch,
        [urllib.error.HTTPError(pricelog.MODELS_URL, 429, "too many requests",
                                email.message.Message(), None),
         b'{"data": []}'],
        sleeps)

    assert payload == {"data": []}
    assert sleeps == [pricelog.FETCH_BACKOFF_S]


def test_a_truncated_transfer_is_retried(monkeypatch):
    """A mid-response drop surfaces as http.client.IncompleteRead — the
    transient tuple's HTTPException member; the empty-body case is its
    JSON twin."""
    sleeps: list[float] = []

    payload = _fetch_with(
        monkeypatch,
        [http.client.IncompleteRead(b"x", 8), b'{"data": []}'],
        sleeps)

    assert payload == {"data": []}
    assert sleeps == [pricelog.FETCH_BACKOFF_S]


def test_an_empty_body_is_retried_as_transient(monkeypatch):
    """An empty HTTP 200 is a broken transfer, not a catalog of zero
    models — the same reasoning _catalog_slugs applies to an empty list."""
    sleeps: list[float] = []

    payload = _fetch_with(monkeypatch, [b"", b'{"data": []}'], sleeps)

    assert payload == {"data": []}
    assert sleeps == [pricelog.FETCH_BACKOFF_S]
