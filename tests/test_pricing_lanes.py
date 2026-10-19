"""Codex and Kimi models priced by claudit's table."""
from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from backend import pricing
from tests import gpt56_cuts

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


@pytest.mark.parametrize("model,instant,rates", [
    ("gpt-5.6-sol", datetime.fromisoformat("2026-08-21T19:39:59+00:00"), (5.00, 6.25, 0.50, 30.00)),
    ("gpt-5.6-terra", datetime.fromisoformat("2026-07-30T18:11:59+00:00"), (2.50, 3.125, 0.25, 15.00)),
    ("gpt-5.6-luna", datetime.fromisoformat("2026-07-30T18:11:59+00:00"), (1.00, 1.25, 0.10, 6.00)),

    # The window ends are end-EXCLUSIVE: at the cut instant itself the
    # window is over and the next rate (list) applies. Cuts referenced
    # from tests/gpt56_cuts.py so the boundary moves with the table.
    ("gpt-5.6-sol", gpt56_cuts.AUG21_CUT, (4.00, 5.00, 0.40, 20.00)),
    ("gpt-5.6-terra", gpt56_cuts.JUL30_CUT, (2.00, 2.50, 0.20, 12.00)),
    ("gpt-5.6-luna", gpt56_cuts.JUL30_CUT, (0.20, 0.25, 0.02, 1.20)),
], ids=["sol-before-cut", "terra-before-cut", "luna-before-cut",
        "sol-at-cut", "terra-at-cut", "luna-at-cut"])
def test_gpt56_dated_windows_and_exclusive_cuts(model, instant, rates):
    r = pricing.rate_for(model, instant)
    fresh, create, read, output = rates
    assert (r["fresh"], r["create_5m"], r["create_1h"], r["read"], r["output"]) == (
        fresh, create, create, read, output)
