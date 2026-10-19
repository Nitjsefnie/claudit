from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from tests.refresh_fixture_builders import RATES_A, _endpoint
from tests.test_provider_rate_refresh import refresh, refresh_prices
from tests.vendor_rate_refresh_helpers import vendor as vendor_refresh


def _result(*, vanished=(), sampled=None, notices=()):
    return refresh.Result({}, [], list(vanished), [], list(notices), sampled or {})


def test_context_and_search_move_reports_both_sampled_fields():
    from decimal import Decimal  # pylint: disable=import-outside-toplevel
    listing = refresh.Listing('fixture', {'context_length': 786432},
                              {**RATES_A, 'web_search': 0.02}, None, Decimal(0))
    move = refresh.Move('model', 'Fixture', {
        **RATES_A, 'web_search': 0.01, 'context_length': 131072},
        listing, source='context')
    result = refresh.Result({}, [move], [], [], [], {})
    output = refresh.report('2031-01-01T00:00:00Z', result, {'model': {'id': 'model'}})
    assert 'context_length 131072 → 786432, sampled at detection' in output
    assert 'web_search 0.01 → 0.02, sampled at detection' in output


def test_vendor_context_only_move_has_a_commit_count_and_window():
    outcome = vendor_refresh.VendorOutcome(moves=[vendor_refresh.VendorMove(
        'vendor/model', 'model', context_length=786432)])
    output = refresh.vendor_report('2031-01-01T00:00:00Z', outcome)
    assert 'context_length 786432' in output
    assert 'vendor table: 1 updated' in refresh.commit_message(_result(), output, outcome)


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


def test_report_drops_the_standing_blocks_for_the_commit_body():
    """the vanished and sampled blocks are standing state — the
    run's own stdout report keeps them (the default above), the commit body
    does not, and a model section left empty by the drop leaves no header."""
    result = _result(
        vanished=[("model", host) for host in ("Fireworks", "Nebius")],
        sampled={"model": {"DeepSeek": "endpoint has a pricing schedule"}},
    )

    body = refresh.report("2031-01-01T00:00:00Z", result,
                          {"model": {"id": "openrouter/model"}},
                          standing=False)

    assert not any(line.startswith(("  vanished  ", "  sampled   "))
                   for line in body.splitlines())
    assert "model (openrouter/model)" not in body


def test_report_keeps_notices_when_the_standing_blocks_drop():
    result = _result(notices=[_delisted("alpha (openrouter/alpha)")])

    body = refresh.report("2031-01-01T00:00:00Z", result,
                          {"alpha": {"id": "openrouter/alpha"}},
                          standing=False)

    assert "notices:" in body
    assert _DELISTED_MSG in body


def test_vanished_state_groups_hosts_per_model_in_order():
    assert refresh.vanished_state([
        ("b-model", "Zeta"), ("a-model", "Host2"),
        ("a-model", "Host1"), ("b-model", "Alpha"),
    ]) == {"a-model": ["Host1", "Host2"], "b-model": ["Alpha", "Zeta"]}
    assert refresh.vanished_state([]) == {}


def test_sampled_state_records_the_display_classification():
    """The recorded classification per sampled host is the short display
    reason the report's sampled line groups by, so a changed
    classification is a diff of the state file; a model with
    no sampled hosts leaves no empty entry behind."""
    assert refresh.sampled_state({
        "model": {
            "Baidu": "no in-force log state matches the listing",
            "Wafer": "the listing lags the price log",
        },
        "quiet": {},
    }) == {"model": {"Baidu": "no matching log state",
                     "Wafer": "listing lags log"}}
    assert refresh.sampled_state({}) == {}


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


# A raw notice is "<subject>: <message>"; a rendered line is
# "<message>: <subjects>". Both spellings are built from these so the
# fixtures cannot drift from the renderer's shape.
_DELISTED_MSG = "delisted from OpenRouter's catalog; row kept, not fetched"
_TAG_MSG = "tag 'wafer/fast' names neither a known region nor a quantization"
_NO_ENDPOINT_MSG = "the vendor lists no first-party endpoint; skipped"


def _delisted(subject: str) -> str:
    return f"{subject}: {_DELISTED_MSG}"


def test_notice_lines_render_groups_and_pass_through_the_rest():
    """The baseline unit is the rendered line: grouped subjects join one
    line, a notice `_notice_parts` does not recognise passes through
    verbatim, and exact duplicates collapse ."""
    assert refresh.notice_lines([
        _delisted("alpha (openrouter/alpha)"),
        _delisted("beta (openrouter/beta)"),
        f"alpha via Fireworks: {_TAG_MSG}",
        "rule-resolved: took the bare namespace endpoint",
        "rule-resolved: took the bare namespace endpoint",
    ]) == [
        f"{_DELISTED_MSG}: alpha (openrouter/alpha), beta (openrouter/beta)",
        f"{_TAG_MSG}: alpha via Fireworks",
        "rule-resolved: took the bare namespace endpoint",
    ]
    assert refresh.notice_lines([]) == []


@pytest.mark.parametrize("notices, previous, models, absent, present", [
    ([_delisted("alpha (openrouter/alpha)"),
      f"beta (openrouter/beta): {_TAG_MSG}"],
     frozenset({f"{_DELISTED_MSG}: alpha (openrouter/alpha)"}),
     {"alpha": {"id": "openrouter/alpha"}, "beta": {"id": "openrouter/beta"}},
     "delisted from OpenRouter's catalog", f"{_TAG_MSG}: beta (openrouter/beta)"),
    (["rule-resolved: took the bare namespace endpoint",
      "rule-resolved: the data region leaves 'azure'"],
     frozenset({"rule-resolved: took the bare namespace endpoint"}), {},
     "took the bare namespace endpoint", "the data region leaves 'azure'"),
], ids=["grouped", "passthrough"])
def test_report_omits_a_notice_line_the_baseline_carries(
        notices, previous, models, absent, present):
    output = refresh.report("2031-01-01T00:00:00Z", _result(notices=notices),
                            models, previous)

    assert absent not in output
    assert present in output


def test_report_omits_the_notices_header_when_every_line_is_unchanged():
    result = _result(notices=[_delisted("alpha (openrouter/alpha)")])
    previous = frozenset({f"{_DELISTED_MSG}: alpha (openrouter/alpha)"})

    output = refresh.report("2031-01-01T00:00:00Z", result,
                            {"alpha": {"id": "openrouter/alpha"}}, previous)

    assert "notices:" not in output


def test_a_changed_notice_line_still_appears():
    """A grouped line that gains a subject differs from the carried one, so
    it appears in full — the changed notice is never dropped."""
    result = _result(notices=[
        _delisted("alpha (openrouter/alpha)"),
        _delisted("beta (openrouter/beta)"),
    ])
    previous = frozenset({f"{_DELISTED_MSG}: alpha (openrouter/alpha)"})

    output = refresh.report("2031-01-01T00:00:00Z", result,
                            {"alpha": {"id": "openrouter/alpha"},
                             "beta": {"id": "openrouter/beta"}}, previous)

    assert (f"{_DELISTED_MSG}: alpha (openrouter/alpha), "
            f"beta (openrouter/beta)") in output


def test_vendor_report_omits_an_unchanged_notice_line():
    vendor = SimpleNamespace(
        moves=[], refusals=[],
        notices=[
            f"openai/alpha: {_NO_ENDPOINT_MSG}",
            f"openai/beta: {_NO_ENDPOINT_MSG}",
        ])
    previous = frozenset({f"{_NO_ENDPOINT_MSG}: openai/alpha, openai/beta"})

    output = refresh.vendor_report("2031-01-01T00:00:00Z", vendor, previous)

    assert "no first-party endpoint" not in output
    assert "notices:" not in output, "an all-suppressed vendor section is bare"


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
