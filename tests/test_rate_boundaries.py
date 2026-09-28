"""Synthetic properties for each model/provider rate timeline."""
from __future__ import annotations

import importlib.util
import re
from datetime import datetime, timedelta, timezone
from inspect import signature

import pytest

from backend import pricing
from backend.api_common import fold_per_model

UTC = timezone.utc


def _rates(fresh: float) -> dict[str, float]:
    return {
        "fresh": fresh,
        "create_5m": fresh * 1.25,
        "create_1h": fresh * 2,
        "read": fresh / 10,
        "output": fresh * 5,
    }


def _install_rate_table(monkeypatch, dated_fixture):
    """Install only synthetic rows, including every resolver branch."""
    model, main_host = dated_fixture.model, "BoundaryHost"
    model_after_start = datetime(2026, 11, 1, tzinfo=UTC)
    provider_start = datetime(2026, 10, 1, tzinfo=UTC)
    provider_cutover = datetime(2026, 12, 1, tzinfo=UTC)
    perma_cutover = datetime(2027, 1, 1, tzinfo=UTC)
    variant_cutover = datetime(2027, 2, 1, tzinfo=UTC)
    model_only_ends = [datetime(2027, 3, 1, tzinfo=UTC),
                       datetime(2027, 4, 1, tzinfo=UTC)]
    free_start = datetime(2027, 5, 1, tzinfo=UTC)
    free_cutover = datetime(2027, 6, 1, tzinfo=UTC)

    model_before, model_middle, model_list = _rates(19), _rates(13), _rates(5)
    main_provider_before, main_provider_list = _rates(31), _rates(7)
    perma_before, perma_list = _rates(29), _rates(11)
    variant_before, variant_list = _rates(37), _rates(17)
    free_before, free_list = _rates(41), _rates(23)
    model_only_before, model_only_middle, model_only_list = (
        _rates(43), _rates(47), _rates(53))
    tier_rates = _rates(59)

    model_rates = {
        model: model_list,
        "acme/snapshot-0731": _rates(3),
        "acme/model-only-9": model_only_list,
    }
    model_dated = {
        model: [(dated_fixture.cutover, model_before),
                (model_after_start, model_middle)],
        "acme/model-only-9": [(model_only_ends[0], model_only_before),
                              (model_only_ends[1], model_only_middle)],
    }
    provider_rates = {
        (model, main_host): main_provider_list,
        ("acme/snapshot-0731", main_host): perma_list,
        ("acme/variant-9", main_host): variant_list,
        ("acme/free-9", main_host): free_list,
    }
    provider_dated = {
        (model, main_host): [(provider_cutover, main_provider_before)],
        ("acme/snapshot-0731", main_host): [(perma_cutover, perma_before)],
        # The provider start and dated end coincide; the result must be one
        # sorted boundary even though both sources name the same instant.
        ("acme/variant-9", main_host): [(variant_cutover, variant_before)],
        ("acme/free-9", main_host): [(free_cutover, free_before)],
    }
    provider_starts = {
        (model, main_host): provider_start,
        ("acme/variant-9", main_host): variant_cutover,
        ("acme/free-9", main_host): free_start,
    }

    monkeypatch.setattr(pricing, "MODEL_RATES", model_rates)
    monkeypatch.setattr(pricing, "DATED_RATES", model_dated)
    monkeypatch.setattr(pricing, "PROVIDER_RATES", provider_rates)
    monkeypatch.setattr(pricing, "PROVIDER_DATED_RATES", provider_dated)
    monkeypatch.setattr(pricing, "PROVIDER_STARTS", provider_starts)
    monkeypatch.setattr(pricing, "PROVIDER_SCHEDULES", {})
    monkeypatch.setattr(pricing, "DEFAULT_RATES", _rates(61))
    monkeypatch.setattr(
        pricing, "_TIER_FALLBACKS",  # pylint: disable=protected-access
        ((re.compile(r"acme/tier"), tier_rates),),
    )
    return {
        "main": (model, main_host),
        "main_boundaries": [dated_fixture.cutover, provider_start,
                            provider_cutover],
        "main_rates": [model_before, model_middle, main_provider_before,
                       main_provider_list],
        "perma": ("acme/snapshot-20270731", main_host),
        "perma_boundaries": [perma_cutover],
        "variant": ("acme/variant-9:nitro", main_host),
        "variant_boundaries": [variant_cutover],
        "free": ("acme/free-9:free", main_host),
        "model_only": ("acme/model-only-9", "NoProviderRow"),
        "model_only_boundaries": model_only_ends,
        "tier": ("acme/tier-99", "NoProviderRow"),
    }


def _rate_boundaries_function():
    spec = importlib.util.find_spec("backend.rate_boundaries")
    assert spec is not None, "backend.rate_boundaries.rate_boundaries is missing"
    from backend.rate_boundaries import rate_boundaries
    return rate_boundaries


def _pair_probes(boundaries: list[datetime]) -> list[list[datetime]]:
    if not boundaries:
        return [[datetime(2025, 1, 1, tzinfo=UTC),
                 datetime(2030, 1, 1, tzinfo=UTC)]]
    intervals = [[boundaries[0] - timedelta(days=1),
                  boundaries[0] - timedelta(microseconds=1)]]
    for left, right in zip(boundaries, boundaries[1:], strict=False):
        intervals.append([left, left + (right - left) / 2,
                          right - timedelta(microseconds=1)])
    intervals.append([boundaries[-1], boundaries[-1] + timedelta(days=1)])
    return intervals


def test_rate_boundaries_partition_every_synthetic_resolution(
        monkeypatch, synthetic_dated_rate):
    """Each returned boundary is a real transition for the synthetic rows."""
    table = _install_rate_table(monkeypatch, synthetic_dated_rate)
    rate_boundaries = _rate_boundaries_function()
    pairs = [table["main"], table["perma"], table["variant"], table["free"],
             table["model_only"], table["tier"]]

    for model, provider in pairs:
        boundaries = rate_boundaries(model, provider)
        assert boundaries == sorted(set(boundaries))
        assert all(value.tzinfo is not None and value.utcoffset() == timedelta(0)
                   for value in boundaries)
        for probes in _pair_probes(boundaries):
            resolved_rates = [pricing.resolve(model, ts, provider).rates
                              for ts in probes]
            assert all(value == resolved_rates[0]
                       for value in resolved_rates[1:]), (model, provider, probes)
        for boundary in boundaries:
            before = pricing.resolve(
                model, boundary - timedelta(microseconds=1), provider).rates
            at = pricing.resolve(model, boundary, provider).rates
            assert before != at, (model, provider, boundary)

    assert rate_boundaries(*table["main"]) == table["main_boundaries"]
    assert rate_boundaries(*table["perma"]) == table["perma_boundaries"]
    assert rate_boundaries(*table["variant"]) == table["variant_boundaries"]
    assert rate_boundaries(*table["free"]) == []
    assert rate_boundaries(*table["model_only"]) == table["model_only_boundaries"]
    assert rate_boundaries(*table["tier"]) == []


def test_fold_prices_each_row_at_its_own_pair_representative(
        monkeypatch, synthetic_dated_rate):
    table = _install_rate_table(monkeypatch, synthetic_dated_rate)
    model, provider = table["main"]
    expected_rates = table["main_rates"]
    rows = [
        (model, provider, index, False, 1, 1_000_000, 0, 0, 0, 0, 0,
         rates["fresh"])
        for index, rates in enumerate(expected_rates)
    ]
    fold_parameters = signature(fold_per_model).parameters
    if "pair_bounds" not in fold_parameters:
        pytest.fail("fold_per_model does not accept the pair boundary map")

    folded = fold_per_model(
        rows, pair_bounds={(model, provider): table["main_boundaries"]})

    assert len(rows) == 4
    assert len(folded) == 1
    assert folded[0]["cost_buckets"]["fresh"] == pytest.approx(
        sum(rate["fresh"] for rate in expected_rates), abs=1e-4)
    assert folded[0]["cost_total"] == pytest.approx(
        sum(rate["fresh"] for rate in expected_rates), abs=1e-4)
