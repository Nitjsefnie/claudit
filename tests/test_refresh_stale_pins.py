"""Stale resolution tags use the ordinary unpinned selection on both tables."""
from __future__ import annotations

import pytest

from tests.refresh_fixture_builders import _endpoint, seed_doc
from tests.test_refresh_pricelog import _entries, _joined, _series
from tests.vendor_rate_refresh_helpers import NOW, _catalog, _load, _payload, _run

selection = _load("refresh_selection")
MODEL = "moonshotai/kimi-k2.7-code"
KEY = "kimi-k2-7-code"
HOST = "Moonshot AI"
STALE = "moonshotai/int4"
BASE = {"fresh": 0.95, "create_5m": 0.95, "create_1h": 0.95,
        "output": 4.0, "read": 0.19}
FAST = {field: value * 2 for field, value in BASE.items()}


def _endpoints(base="moonshotai", other="moonshotai/highspeed"):
    return [_endpoint(HOST, FAST, tag=other), _endpoint(HOST, BASE, tag=base)]


def _notice(notices):
    notice, = [note for note in notices if "stale pin" in note]
    assert STALE in notice
    assert "listed tags: 'moonshotai', 'moonshotai/highspeed'" in notice
    assert "rule-resolved past a stale pin" in notice
    return notice


@pytest.mark.parametrize("extra", [
    {}, {"select": "cheapest"}, {"select": "cheapest", "ignore": ["context_length"]}])
def test_provider_stale_pin_refreshes_the_bare_endpoint(extra):
    pin = {"tag": STALE, "why": "synthetic retired endpoint", **extra}
    selected, refused, notices, _ = selection.listed_rows(
        KEY, _payload(*_endpoints()), None, {HOST: pin}, {}, NOW)
    assert refused == {}
    assert selected[HOST].tag == "moonshotai"
    assert selected[HOST].rates == BASE
    notice = _notice(notices)
    assert f"{KEY} via {HOST}" in notice
    assert "rule-resolved (bare namespace): took the bare namespace endpoint 'moonshotai'" in notice


@pytest.mark.parametrize("extra", [
    {}, {"select": "cheapest"}, {"select": "cheapest", "ignore": ["context_length"]}])
def test_stale_pin_price_log_selection_agrees(extra):
    pin = {"tag": STALE, **extra}
    endpoints = _endpoints()
    assert selection.selected_endpoint_indices(HOST, endpoints, None, pin, None, NOW) == [1]
    series = [_series(slug="moonshotai", host=HOST, rates=rates) for rates in (FAST, BASE)]
    joined = _joined(endpoints, series, resolutions={HOST: pin})[HOST]
    assert joined.reason is None
    assert joined.entries == _entries(series[1])


def test_vendor_stale_pin_refreshes_the_bare_endpoint():
    doc = seed_doc(resolve={KEY: {"tag": STALE, "why": "synthetic retired endpoint"}})
    result, outcome = _run(doc, _catalog(MODEL), {MODEL: _payload(*_endpoints())})
    assert outcome.refusals == []
    assert result["openrouter"]["models"][KEY]["vendor_host"] == HOST
    notice = _notice(outcome.notices)
    assert MODEL in notice
    assert "bare" in notice and "'moonshotai'" in notice
    chosen = _load("refresh_vendor_rates")._choose(  # pylint: disable=protected-access
        MODEL, _endpoints(), {"tag": STALE}, [])
    assert chosen is not None and chosen[0] == BASE


@pytest.mark.parametrize("table", ["provider", "vendor"])
def test_stale_pin_does_not_hide_multi_price_ambiguity(table):
    endpoints = _endpoints("moonshotai/fp4", "moonshotai/fp8")
    if table == "provider":
        selected, refused, notices, _ = selection.listed_rows(
            KEY, _payload(*endpoints), None, {HOST: {"tag": STALE}}, {}, NOW)
        assert selected == {}
        reason = refused[HOST]
        assert "2 different prices; resolve it" in reason
    else:
        doc = seed_doc(resolve={KEY: {"tag": STALE}})
        _, outcome = _run(doc, _catalog(MODEL), {MODEL: _payload(*endpoints)})
        assert outcome.moves == []
        reason, = outcome.refusals
        notices = outcome.notices
        assert "at 2 prices; record a pin" in reason
    assert "stale" not in reason and "not listed" not in reason
    notice, = [note for note in notices if "stale pin" in note]
    assert STALE in notice and "listed tags: 'moonshotai/fp4', 'moonshotai/fp8'" in notice
    assert "rule-resolved" not in notice


@pytest.mark.parametrize("table", ["provider", "vendor"])
def test_malformed_stale_pin_still_refuses(table):
    pin = {"tag": STALE, "unexpected": True}
    if table == "provider":
        _, refused, _, _ = selection.listed_rows(
            KEY, _payload(*_endpoints()), None, {HOST: pin}, {}, NOW)
        assert "a resolution is keyed on" in refused[HOST]
    else:
        _, outcome = _run(seed_doc(resolve={KEY: pin}), _catalog(MODEL),
                          {MODEL: _payload(*_endpoints())})
        assert len(outcome.refusals) == 1
        assert "the resolve pin is a dict" in outcome.refusals[0]


@pytest.mark.parametrize("table", ["provider", "vendor"])
def test_stale_pin_uses_unique_quantization_without_a_bare_tag(table):
    endpoints = _endpoints("moonshotai/fp8")
    if table == "provider":
        selected, refused, notices, _ = selection.listed_rows(
            KEY, _payload(*endpoints), None, {HOST: {"tag": STALE}}, {}, NOW)
        assert refused == {} and selected[HOST].tag == "moonshotai/fp8"
    else:
        _, outcome = _run(seed_doc(resolve={KEY: {"tag": STALE}}), _catalog(MODEL),
                          {MODEL: _payload(*endpoints)})
        assert outcome.refusals == []
        notices = outcome.notices
    assert any("rule-resolved (unique quantization)" in note for note in notices)


def test_stale_provider_pin_reapplies_the_region_filter():
    endpoints = _endpoints("moonshotai/eu", "moonshotai/us")
    selected, refused, notices, _ = selection.listed_rows(
        KEY, _payload(*endpoints), "eu", {HOST: {"tag": STALE}}, {}, NOW)
    assert refused == {} and selected[HOST].tag == "moonshotai/eu"
    assert any("rule-resolved (data region)" in note for note in notices)
    assert selection.selected_endpoint_indices(
        HOST, endpoints, "eu", {"tag": STALE}, None, NOW) == [1]
