"""Codex and Kimi models priced by claudit's table (D4, D5, D6).

Per SV-TEST-DATA the assertions read each row's rates from the loaded
tables at run time; what is pinned is exact resolution and the pricing
ARITHMETIC, never a committed rate value.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from backend import pricing

# The lane ids this repo serves; the rates live in the table.
LANE_MODELS = (
    "kimi-k3", "kimi-k2-7-code", "kimi-k2-6",
    "gpt-6-astra", "gpt-6-sol", "gpt-6-luna",
    "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
)


@pytest.mark.parametrize("model", LANE_MODELS)
def test_lane_model_resolves_exact_at_its_list_rate(model):
    r = pricing.resolve(model)
    assert r.kind == "exact"
    assert r.rates is pricing._list_rates(r.key or model)  # pylint: disable=protected-access


def test_flat_create_prices_identically_under_any_declared_ttl(monkeypatch):
    model = "acme/flat-create-9"
    rates = {"fresh": 2.6, "create_5m": 6.5, "create_1h": 6.5,
             "read": 0.26, "output": 13.0}
    monkeypatch.setitem(pricing.MODEL_RATES, model, rates)
    kw: dict[str, Any] = {"fresh": 0, "output": 0, "read": 0}
    as_5m = pricing.compute_cost(model, eph5=1_000_000, eph1h=0, unsplit_create=0, **kw)
    as_1h = pricing.compute_cost(model, eph5=0, eph1h=1_000_000, unsplit_create=0, **kw)
    undeclared = pricing.compute_cost(model, eph5=0, eph1h=0, unsplit_create=1_000_000, **kw)
    assert rates["create_5m"] == rates["create_1h"]
    assert as_5m == as_1h == undeclared == pytest.approx(
        rates["create_1h"], rel=1e-12)


def test_long_context_doubles_input_side_and_raises_output_by_half(
        monkeypatch: pytest.MonkeyPatch) -> None:
    model = "gpt-6-sol"
    rates = {"fresh": 2.0, "create_5m": 3.0, "create_1h": 4.0,
             "read": 5.0, "output": 6.0}
    monkeypatch.setattr(pricing, "MODEL_RATES", {model: rates})
    monkeypatch.setattr(pricing, "DATED_RATES", {})
    kw: dict[str, Any] = {"fresh": 1, "output": 1, "eph5": 0,
                          "eph1h": 1, "unsplit_create": 0, "read": 1}
    base = pricing.compute_cost(model, **kw)
    long = pricing.compute_cost(model, long_context=True, **kw)
    scale = 1_000_000
    assert base == pytest.approx(
        (rates["fresh"] + rates["output"] + rates["create_1h"]
         + rates["read"]) / scale, rel=1e-12)
    assert long == pytest.approx(
        (2 * (rates["fresh"] + rates["create_1h"] + rates["read"])
         + 1.5 * rates["output"]) / scale, rel=1e-12)


def test_long_context_multiplier_applies_to_5m_cache_writes(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The long-context meter multiplies the whole input side, the 5m
    cache-write bucket included: eph5 prices at create_5m x 2x, not at
    the unsplit create_1h rate."""
    model = "gpt-6-sol"
    rates = {"fresh": 2.0, "create_5m": 3.0, "create_1h": 4.0,
             "read": 5.0, "output": 6.0}
    monkeypatch.setattr(pricing, "MODEL_RATES", {model: rates})
    monkeypatch.setattr(pricing, "DATED_RATES", {})
    create_5m = rates["create_5m"]
    kw: dict[str, Any] = {"fresh": 0, "output": 0, "eph5": 1,
                          "eph1h": 0, "unsplit_create": 0, "read": 0}
    base = pricing.compute_cost(model, **kw)
    long = pricing.compute_cost(model, long_context=True, **kw)
    assert base == pytest.approx(create_5m / 1_000_000, rel=1e-12)
    assert long == pytest.approx(2 * create_5m / 1_000_000, rel=1e-12)


def test_long_context_defaults_off_for_every_existing_caller(
        monkeypatch: pytest.MonkeyPatch) -> None:
    model = "claude-opus-5"
    rates = {"fresh": 2.0, "create_5m": 3.0, "create_1h": 4.0,
             "read": 5.0, "output": 6.0}
    monkeypatch.setattr(pricing, "MODEL_RATES", {model: rates})
    monkeypatch.setattr(pricing, "DATED_RATES", {})
    kw: dict[str, Any] = {"fresh": 1, "output": 0, "eph5": 0,
                          "eph1h": 0, "unsplit_create": 0, "read": 0}
    assert pricing.compute_cost(model, **kw) == pricing.compute_cost(
        model, long_context=False, **kw)


@pytest.mark.parametrize("model,before,rates", [
    # Windows copied from codexmeter backend/pricing.py DATED_RATES.
    ("gpt-5.6-sol", "2026-08-21T19:39:59+00:00", (5.00, 6.25, 0.50, 30.00)),
    ("gpt-5.6-terra", "2026-07-30T18:11:59+00:00", (2.50, 3.125, 0.25, 15.00)),
    ("gpt-5.6-luna", "2026-07-30T18:11:59+00:00", (1.00, 1.25, 0.10, 6.00)),
])
def test_gpt56_dated_windows_survive_the_port(model, before, rates):
    r = pricing.rate_for(model, datetime.fromisoformat(before))
    fresh, create, read, output = rates
    assert (r["fresh"], r["create_5m"], r["create_1h"], r["read"], r["output"]) == (
        fresh, create, create, read, output)


@pytest.mark.parametrize("model,cut,list_rates", [
    # The window ends are end-EXCLUSIVE: at the cut instant itself the
    # window is over and the next rate (list) applies. Cuts referenced
    # from pricing.py so the boundary moves with the table.
    ("gpt-5.6-sol", pricing.AUG21_CUT, (4.00, 5.00, 0.40, 20.00)),
    ("gpt-5.6-terra", pricing.JUL30_CUT, (2.00, 2.50, 0.20, 12.00)),
    ("gpt-5.6-luna", pricing.JUL30_CUT, (0.20, 0.25, 0.02, 1.20)),
])
def test_gpt56_windows_are_end_exclusive_at_the_cut(model, cut, list_rates):
    r = pricing.rate_for(model, cut)
    fresh, create, read, output = list_rates
    assert (r["fresh"], r["create_5m"], r["create_1h"], r["read"], r["output"]) == (
        fresh, create, create, read, output)
