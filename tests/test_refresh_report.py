"""Report layout for the hourly pricing refresh."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from tests.refresh_fixture_builders import RATES_A, _endpoint
from tests.test_provider_rate_refresh import refresh, refresh_prices


def _result(*, vanished=(), sampled=None, notices=()):
    return refresh.Result({}, [], list(vanished), [], list(notices), sampled or {})


def test_report_groups_vanished_and_sampled_hosts_by_model_and_reason():
    result = _result(
        vanished=[("model", host) for host in
                  ("NextBit", "Fireworks", "Reka", "Io Net", "Nebius")],
        sampled={"model": {
            "Sail Research": "2 endpoints at one current price",
            "DeepSeek": "endpoint has a pricing schedule",
            "Alibaba": "endpoint has a pricing schedule",
        }},
    )

    output = refresh.report("2031-01-01T00:00:00Z", result,
                            {"model": {"id": "openrouter/model"}})

    model_section = output.split("model (openrouter/model)", 1)[1].strip("\n")
    assert model_section.splitlines() == [
        "  vanished  Fireworks, Io Net, Nebius, NextBit, Reka (rows kept)",
        "  sampled   Alibaba, DeepSeek (endpoint schedule); "
        "Sail Research (2 endpoints at one current price)",
    ]


def test_report_groups_model_notices_by_shared_message():
    result = _result(notices=[
        "alpha (openrouter/alpha): delisted from OpenRouter's catalog; "
        "row kept, not fetched",
        "beta (openrouter/beta): delisted from OpenRouter's catalog; "
        "row kept, not fetched",
        "alpha via Fireworks: tag 'wafer/fast' names neither a known region "
        "nor a quantization",
        "beta via DeepSeek: tag 'wafer/fast' names neither a known region "
        "nor a quantization",
        "beta via Reka: tag 'wafer/fp7' names neither a known region "
        "nor a quantization",
    ])

    output = refresh.report("2031-01-01T00:00:00Z", result,
                            {"alpha": {"id": "openrouter/alpha"},
                             "beta": {"id": "openrouter/beta"}})

    assert output.split("notices:\n", 1)[1].splitlines() == [
        "  delisted from OpenRouter's catalog; row kept, not fetched: "
        "alpha (openrouter/alpha), beta (openrouter/beta)",
        "  tag 'wafer/fast' names neither a known region nor a quantization: "
        "alpha via Fireworks, beta via DeepSeek",
        "  tag 'wafer/fp7' names neither a known region nor a quantization: "
        "beta via Reka",
    ]


def test_vendor_report_groups_missing_first_party_endpoint_notices():
    vendor = SimpleNamespace(
        moves=[], refusals=[],
        notices=[
            "openai/alpha: the vendor lists no first-party endpoint; skipped",
            "openai/beta: the vendor lists no first-party endpoint; skipped",
        ],
    )

    output = refresh.vendor_report("2031-01-01T00:00:00Z", vendor)

    assert "  the vendor lists no first-party endpoint; skipped: " \
           "openai/alpha, openai/beta" in output
    assert sum("no first-party endpoint" in line
               for line in output.splitlines()) == 1


@pytest.mark.parametrize(("suffix", "expected"), [
    ("global", "global"),
    ("us-east5", "us-east5"),
    ("europe-west4", "europe-west4"),
    ("us-east-1", "us-east-1"),
    ("unknown", None),
])
def test_region_suffixes_include_global_and_area_number_forms(suffix, expected):
    tag = f"provider/{suffix}"
    region = None if suffix in {"global", "unknown"} else suffix
    selected, refused, notices, _ = refresh.listed_rows(
        "synthetic/model",
        {"data": {"endpoints": [_endpoint("Fixture", RATES_A, tag=tag)]}},
        region, {}, {}, datetime(2031, 1, 1, tzinfo=timezone.utc))

    assert refresh_prices.tag_region(tag) == expected
    assert isinstance(refused, dict) and not refused
    assert set(selected) == {"Fixture"}
    if expected is None:
        assert refresh_prices.unknown_suffixes(tag) == [suffix]
        assert any(tag in notice and
                   "names neither a known region nor a quantization" in notice
                   for notice in notices)
    else:
        assert refresh_prices.unknown_suffixes(tag) == []
        assert notices == []
