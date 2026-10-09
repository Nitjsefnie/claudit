"""Report layout for the hourly pricing refresh."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "ci"))

import refresh_report


def _result(*, vanished=(), sampled=None, notices=()):
    return SimpleNamespace(moves=[], vanished=list(vanished), refusals=[],
                           notices=list(notices), sampled=sampled or {})


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

    output = refresh_report.report("2031-01-01T00:00:00Z", result,
                                   {"model": {"id": "openrouter/model"}})

    assert "  vanished  Fireworks, Io Net, Nebius, NextBit, Reka (rows kept)" in output
    assert "  sampled   Alibaba, DeepSeek (endpoint schedule); " \
           "Sail Research (2 endpoints at one current price)" in output
    model_section = output.split("model (openrouter/model)", 1)[1]
    assert sum(line.startswith("  vanished") for line in model_section.splitlines()) == 1
    assert sum(line.startswith("  sampled") for line in model_section.splitlines()) == 1


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

    output = refresh_report.report("2031-01-01T00:00:00Z", result,
                                   {"alpha": {"id": "openrouter/alpha"},
                                    "beta": {"id": "openrouter/beta"}})

    assert "  delisted from OpenRouter's catalog; row kept, not fetched: " \
           "alpha (openrouter/alpha), beta (openrouter/beta)" in output
    assert "  tag 'wafer/fast' names neither a known region nor a quantization: " \
           "alpha via Fireworks, beta via DeepSeek" in output
    assert "  tag 'wafer/fp7' names neither a known region nor a quantization: " \
           "beta via Reka" in output


def test_vendor_report_groups_missing_first_party_endpoint_notices():
    vendor = SimpleNamespace(
        moves=[], refusals=[],
        notices=[
            "openai/alpha: the vendor lists no first-party endpoint; skipped",
            "openai/beta: the vendor lists no first-party endpoint; skipped",
        ],
    )

    output = refresh_report.vendor_report("2031-01-01T00:00:00Z", vendor)

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
    import refresh_prices

    assert refresh_prices.tag_region(f"provider/{suffix}") == expected
