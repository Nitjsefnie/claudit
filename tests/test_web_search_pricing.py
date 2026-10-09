"""Per-search pricing uses explicit rates and transcript request counts."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend import pricing
from backend.pricing_load import RATE_FIELDS, load_tables
from tests.refresh_fixture_builders import RATES_B, seed_doc

UTC = timezone.utc

# Synthetic price, deliberately unlike the deployed listing (SV-TEST-DATA).
SEARCH_RATE = 0.0137
SEARCH_RATE_2 = 0.045
TOKEN_RATES = {"fresh": 2.0, "create_5m": 2.5, "create_1h": 4.0,
               "read": 0.2, "output": 10.0}
CUTOVER = "2026-08-01T00:00:00Z"
FEE_NOTE = ("web_search $0.0137/request not modelled: per-request, "
            "unpriceable from token counts")


def _entry(rates: dict, **extra) -> dict:
    return {"from": None, **{field: rates[field] for field in RATE_FIELDS},
            **extra}


def _provider_doc(entries: list[dict]) -> dict:
    return seed_doc(
        models={"acme/acme-9": [_entry(TOKEN_RATES)]},
        providers={"acme/acme-9": {"HostCo": entries}},
        fetched="2026-09-24T22:03:13Z",
    )


def _install(monkeypatch, tables: dict) -> None:
    for name in (
        "MODEL_RATES", "DATED_RATES", "PROVIDER_RATES",
        "PROVIDER_DATED_RATES", "PROVIDER_STARTS", "PROVIDER_SCHEDULES",
        "VENDOR_BARE", "VENDOR_HOSTS", "VENDOR_PREFIXES",
    ):
        monkeypatch.setattr(pricing, name, tables[name])
    pricing.clear_tier_fallbacks()


def _cost(*, requests: int | None, res=None) -> float:
    return pricing.compute_cost(
        "acme/acme-9", fresh=1_000_000, output=0, eph5=0, eph1h=0,
        unsplit_create=0, read=0, ts=None,
        adjustments=pricing.CostAdjustments(web_search_requests=requests),
        res=res,
    )


def test_provider_search_rate_is_validated_and_costs_each_search(monkeypatch):
    tables = load_tables(_provider_doc([
        _entry(RATES_B, web_search=SEARCH_RATE),
    ]))
    _install(monkeypatch, tables)
    res = pricing.resolve("acme/acme-9", None, "HostCo")

    assert res.rates["web_search"] == SEARCH_RATE
    assert _cost(requests=3, res=res) == pytest.approx(2.0 + 3 * SEARCH_RATE)
    assert _cost(requests=None, res=res) == pytest.approx(2.0)
    assert _cost(requests=0, res=res) == pytest.approx(2.0)
    assert not hasattr(res, "request_fee")


@pytest.mark.parametrize("value", [-0.1, True, float("inf"), float("nan")])
def test_provider_search_rate_rejects_invalid_values(value):
    with pytest.raises(ValueError, match="acme/acme-9 via HostCo"):
        load_tables(_provider_doc([
            _entry(RATES_B, web_search=value),
        ]))


def test_vendor_search_rate_is_loaded_for_the_bare_model(monkeypatch):
    key, host = "anthropic/acme-9", "VendorHost"
    doc = seed_doc(
        tracked={key: {"id": "anthropic/acme-9",
                       "vendor_host": host}},
        providers={key: {host: [_entry(
            RATES_B,
            web_search=SEARCH_RATE)]}},
    )
    tables = load_tables(doc)
    _install(monkeypatch, tables)

    res = pricing.resolve("acme-9")
    assert res.rates["web_search"] == SEARCH_RATE
    assert pricing.compute_cost(
        "acme-9", fresh=0, output=0, eph5=0, eph1h=0,
        unsplit_create=0, read=0,
        adjustments=pricing.CostAdjustments(web_search_requests=2), res=res,
    ) == pytest.approx(2 * SEARCH_RATE)


def test_old_web_search_fee_note_does_not_become_a_per_record_charge(
        monkeypatch):
    tables = load_tables(_provider_doc([
        _entry(RATES_B, note=FEE_NOTE),
    ]))
    _install(monkeypatch, tables)
    res = pricing.resolve("acme/acme-9", None, "HostCo")

    assert res.rates.get("web_search", 0) == 0
    assert _cost(requests=None, res=res) == pytest.approx(2.0)


def test_search_only_move_adds_a_resolve_epoch_without_changing_tokens(
        monkeypatch):
    older = _entry(TOKEN_RATES, web_search=SEARCH_RATE)
    newer = {**_entry(TOKEN_RATES, web_search=SEARCH_RATE_2),
             "from": CUTOVER}
    tables = load_tables(_provider_doc([older, newer]))
    _install(monkeypatch, tables)
    before = datetime(2026, 7, 1, tzinfo=UTC)
    after = datetime(2026, 8, 2, tzinfo=UTC)
    old_res = pricing.resolve("acme/acme-9", before, "HostCo")
    new_res = pricing.resolve("acme/acme-9", after, "HostCo")

    assert {k: v for k, v in old_res.rates.items() if k != "web_search"} == \
        {k: v for k, v in new_res.rates.items() if k != "web_search"}
    assert old_res.rates["web_search"] == SEARCH_RATE
    assert new_res.rates["web_search"] == SEARCH_RATE_2
    assert tables["RATE_EPOCHS"] == [datetime(2026, 8, 1, tzinfo=UTC)]
    assert _cost(requests=2, res=old_res) == pytest.approx(
        TOKEN_RATES["fresh"] + 2 * SEARCH_RATE)
    assert _cost(requests=2, res=new_res) == pytest.approx(
        TOKEN_RATES["fresh"] + 2 * SEARCH_RATE_2)
