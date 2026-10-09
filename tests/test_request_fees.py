"""A legacy web-search note no longer acts as a per-record request fee."""
from __future__ import annotations

import pytest

from backend import pricing
from backend.pricing_load import RATE_FIELDS, load_tables
from tests.refresh_fixture_builders import RATES_B, seed_doc

OLD_NOTE = ("web_search $0.0137/request not modelled: per-request, "
            "unpriceable from token counts")


def _entry(rates: dict, **extra) -> dict:
    return {"from": None, **{field: rates[field] for field in RATE_FIELDS},
            **extra}


def test_legacy_fee_note_is_not_a_rate_or_cost(monkeypatch):
    doc = seed_doc(
        models={"acme/acme-9": [_entry(RATES_B)]},
        providers={"acme/acme-9": {"HostCo": [
            _entry(RATES_B, note=OLD_NOTE)]}},
    )
    tables = load_tables(doc)
    for name, value in tables.items():
        monkeypatch.setattr(pricing, name, value)
    pricing.clear_tier_fallbacks()
    resolved = pricing.resolve("acme/acme-9", provider="HostCo")

    assert not hasattr(resolved, "request_fee")
    assert resolved.rates.get("web_search", 0) == 0
    assert pricing.compute_cost(
        "acme/acme-9", fresh=0, output=0, eph5=0, eph1h=0,
        unsplit_create=0, read=0, web_search_requests=3, res=resolved,
    ) == pytest.approx(0)
