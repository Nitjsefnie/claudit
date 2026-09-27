"""Synthetic model-resolution tests moved out of test_pricing.py."""
from __future__ import annotations

import pytest

from backend import pricing


def test_fable_5_1_and_mythos_5_1_price_identically(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The read ratio and identical prices are checked on synthetic rows."""
    fable_key = "claude-acme-fable-5-1"
    mythos_key = "claude-acme-mythos-5-1"
    fable_rates = dict(zip(pricing.RATE_FIELDS, (8.0, 10.0, 16.0, 0.2, 40.0)))
    mythos_rates = dict(fable_rates)
    monkeypatch.setitem(pricing.MODEL_RATES, fable_key, fable_rates)
    monkeypatch.setitem(pricing.MODEL_RATES, mythos_key, mythos_rates)

    fable = pricing.resolve(fable_key)
    mythos = pricing.resolve(f"anthropic.{mythos_key}[1m]")
    assert fable.key == fable_key and fable.rates is fable_rates
    assert mythos.key == mythos_key and mythos.rates is mythos_rates
    for rates in (fable_rates, mythos_rates):
        assert rates["read"] == pytest.approx(rates["fresh"] * 0.025)
    assert fable.rates == mythos.rates
    fable_cost = pricing.compute_cost(
        fable_key, fresh=0, output=0, eph5=0, eph1h=0,
        unsplit_create=0, read=1_000_000,
    )
    mythos_cost = pricing.compute_cost(
        f"anthropic.{mythos_key}[1m]", fresh=0, output=0,
        eph5=0, eph1h=0, unsplit_create=0, read=1_000_000,
    )
    assert fable_cost == pytest.approx(fable_rates["read"], rel=1e-12)
    assert mythos_cost == pytest.approx(mythos_rates["read"], rel=1e-12)
    assert fable_cost == mythos_cost


def test_opus_5_5_suffix_and_provider_aliases_keep_their_synthetic_row(
        monkeypatch: pytest.MonkeyPatch) -> None:
    older_key = "claude-acme-opus-5"
    newer_key = "claude-acme-opus-5-5"
    rows = {
        older_key: dict(zip(pricing.RATE_FIELDS, (1, 2, 3, 4, 5))),
        newer_key: dict(zip(pricing.RATE_FIELDS, (6, 7, 8, 9, 10))),
    }
    monkeypatch.setattr(pricing, "MODEL_RATES", rows)
    monkeypatch.setattr(pricing, "DATED_RATES", {})

    for variant in (newer_key, f"{newer_key}[1m]", f"anthropic.{newer_key}"):
        result = pricing.resolve(variant)
        assert result.kind == "exact"
        assert result.key == newer_key
        assert result.rates is rows[newer_key]
    older = pricing.resolve(older_key)
    assert older.rates is rows[older_key]
    assert older.rates is not rows[newer_key]
