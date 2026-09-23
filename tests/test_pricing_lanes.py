"""Codex and Kimi models priced by claudit's table (D4, D5, D6)."""
from datetime import datetime
from typing import Any

import pytest

from backend import pricing

# (fresh, cache write, cached read, output), USD per 1M tokens.
LIST = {
    "kimi-k3":        (3.00, 0.00, 0.30, 15.00),
    "kimi-k2-7-code": (0.95, 0.00, 0.19, 4.00),
    "kimi-k2-6":      (0.95, 0.00, 0.16, 4.00),
    "gpt-6-astra":    (10.00, 12.50, 1.00, 50.00),
    "gpt-6-sol":      (2.00, 2.50, 0.20, 10.00),
    "gpt-6-luna":     (0.10, 0.125, 0.01, 0.50),
    "gpt-5.6-sol":    (4.00, 5.00, 0.40, 20.00),
    "gpt-5.6-terra":  (2.00, 2.50, 0.20, 12.00),
    "gpt-5.6-luna":   (0.20, 0.25, 0.02, 1.20),
}


@pytest.mark.parametrize("model", sorted(LIST))
def test_lane_model_resolves_exact_at_its_list_rate(model):
    fresh, create, read, output = LIST[model]
    r = pricing.resolve(model)
    assert r.kind == "exact"
    assert r.rates["fresh"] == fresh
    assert r.rates["read"] == read
    assert r.rates["output"] == output
    # D4: one write rate, whatever TTL the record does or does not declare.
    assert r.rates["create_5m"] == r.rates["create_1h"] == create


def test_flat_create_prices_identically_under_any_declared_ttl():
    kw: dict[str, Any] = {"fresh": 0, "output": 0, "read": 0}
    as_5m = pricing.compute_cost("gpt-6-sol", eph5=1_000_000, eph1h=0, unsplit_create=0, **kw)
    as_1h = pricing.compute_cost("gpt-6-sol", eph5=0, eph1h=1_000_000, unsplit_create=0, **kw)
    undeclared = pricing.compute_cost("gpt-6-sol", eph5=0, eph1h=0, unsplit_create=1_000_000, **kw)
    assert as_5m == as_1h == undeclared == pytest.approx(2.50)


def test_long_context_doubles_input_side_and_raises_output_by_half():
    kw: dict[str, Any] = {"fresh": 1_000_000, "output": 1_000_000, "eph5": 0,
                          "eph1h": 0, "unsplit_create": 1_000_000, "read": 1_000_000}
    base = pricing.compute_cost("gpt-6-sol", **kw)
    long = pricing.compute_cost("gpt-6-sol", long_context=True, **kw)
    assert base == pytest.approx(2.00 + 10.00 + 2.50 + 0.20)
    assert long == pytest.approx(2 * (2.00 + 2.50 + 0.20) + 1.5 * 10.00)


def test_long_context_multiplier_applies_to_5m_cache_writes():
    """The long-context meter multiplies the whole input side, the 5m
    cache-write bucket included: eph5 prices at create_5m x 2x, not at
    the unsplit create_1h rate."""
    kw: dict[str, Any] = {"fresh": 0, "output": 0, "eph5": 1_000_000,
                          "eph1h": 0, "unsplit_create": 0, "read": 0}
    base = pricing.compute_cost("gpt-6-sol", **kw)
    long = pricing.compute_cost("gpt-6-sol", long_context=True, **kw)
    assert base == pytest.approx(2.50)
    assert long == pytest.approx(2 * 2.50)


def test_long_context_defaults_off_for_every_existing_caller():
    kw: dict[str, Any] = {"fresh": 1_000_000, "output": 0, "eph5": 0,
                          "eph1h": 0, "unsplit_create": 0, "read": 0}
    assert pricing.compute_cost("claude-opus-5", **kw) == pricing.compute_cost(
        "claude-opus-5", long_context=False, **kw)


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
