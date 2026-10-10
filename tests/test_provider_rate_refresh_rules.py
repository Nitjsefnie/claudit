"""#877: the mechanical multi-price host shapes resolve by rule.

Four rules, beneath an explicit resolve entry (which keeps precedence):
a bare namespace endpoint beside service tiers; a unique quantization
beside throughput tiers; the configured data region; and the cheapest of
one quantization's price twins. Each branch and its negative space is
driven through listed_rows, and the live Alibaba shape end to end.
"""
from __future__ import annotations

import pytest

from tests.refresh_fixture_builders import RATES_A, RATES_B, _endpoint
from tests.test_provider_rate_refresh import (
    NOW,
    _openrouter_template,
    Run,
    STAMP,
    refresh,
)

HOST = "Fixture"
MODEL = "synthetic/model"

# A third price vector, distinct from RATES_A and RATES_B on every field.
RATES_C = {"fresh": 3.0, "create_5m": 3.75, "create_1h": 6.0,
           "read": 0.3, "output": 15.0}


def _select(listings, *, region=None, resolutions=None, stored=None):
    """One host's listing through listed_rows, as the refresh drives it."""
    endpoints = [_endpoint(HOST, rates, tag=tag) for tag, rates in listings]
    return refresh.listed_rows(
        MODEL, {"data": {"endpoints": endpoints}}, region, resolutions or {},
        {HOST: stored} if stored else {}, NOW)


def _rules(notices, rule):
    return [note for note in notices
            if "rule-resolved" in note and f"({rule})" in note]


# --- the bare namespace rule -------------------------------------------------


def test_a_bare_namespace_wins_over_service_tiers():
    selected, refused, notices, _ = _select(
        [("fixture", RATES_A), ("fixture/fast", RATES_B), ("fixture/flex", RATES_C)])
    assert refused == {}
    assert selected[HOST].tag == "fixture"
    assert selected[HOST].rates == RATES_A
    assert _rules(notices, "bare namespace")


def test_a_bare_namespace_wins_even_when_a_tier_is_cheaper():
    """Shape, not price: a cheaper throughput tier never becomes the row."""
    cheaper = {**RATES_A, "read": 0.01, "fresh": 0.05, "output": 0.2}
    selected, refused, _, _ = _select(
        [("fixture", RATES_A), ("fixture/fast", cheaper)])
    assert refused == {}
    assert selected[HOST].tag == "fixture"


def test_a_bare_namespace_beside_a_quantization_is_refused():
    """A quantization sibling is another offering, not a service tier, so
    the bare rule must not fire and the shape still needs a human."""
    selected, refused, _, _ = _select(
        [("fixture", RATES_A), ("fixture/fp4", RATES_B)])
    assert selected == {}
    assert set(refused) == {HOST}
    assert "resolve it in openrouter.models" in refused[HOST]


def test_two_quantizations_beside_a_bare_namespace_are_refused():
    selected, refused, _, _ = _select(
        [("fixture", RATES_A), ("fixture/fp4", RATES_B), ("fixture/fp8", RATES_C)])
    assert selected == {}
    assert set(refused) == {HOST}


def test_two_bare_prices_are_refused():
    """Two price groups both carrying the bare tag: nothing says which one
    the account gets, and no rule may pick one."""
    selected, refused, _, _ = _select([("fixture", RATES_A), ("fixture", RATES_B)])
    assert selected == {}
    assert set(refused) == {HOST}


# --- the unique quantization rule --------------------------------------------


def test_a_unique_quantization_wins_over_a_throughput_tier():
    selected, refused, notices, _ = _select(
        [("fixture/fp4", RATES_A), ("fixture/fast", RATES_B)])
    assert refused == {}
    assert selected[HOST].tag == "fixture/fp4"
    assert selected[HOST].rates == RATES_A
    assert _rules(notices, "unique quantization")


def test_a_unique_quantization_wins_even_when_the_tier_is_cheaper():
    cheaper = {**RATES_A, "read": 0.01, "fresh": 0.05, "output": 0.2}
    selected, refused, _, _ = _select(
        [("fixture/fp4", RATES_A), ("fixture/fast", cheaper)])
    assert refused == {}
    assert selected[HOST].tag == "fixture/fp4"


def test_a_unique_quantization_admits_a_tier_at_its_own_price():
    """One listing, two tags: a throughput tier at the quantization's exact
    price rides in that group, so the quantization endpoint is still the
    one the rule takes."""
    selected, refused, notices, _ = _select(
        [("fixture/fp4", RATES_A), ("fixture/fast", RATES_A),
         ("fixture/flex", RATES_B)])
    assert refused == {}
    assert selected[HOST].tag == "fixture/fp4"
    assert selected[HOST].rates == RATES_A
    assert _rules(notices, "unique quantization")


def test_a_quantization_group_admits_no_region_spelling():
    """The relaxed group is quantization-plus-tiers only: a region spelling
    at the same price is another offering, so the shape refuses."""
    selected, refused, _, _ = _select(
        [("fixture/fp4", RATES_A), ("fixture/swedencentral", RATES_A),
         ("fixture/flex", RATES_B)])
    assert selected == {}
    assert set(refused) == {HOST}


def test_a_quantization_beside_a_region_spelling_is_refused():
    """Without a global endpoint in the namespace, an unclassifiable
    sibling is not read as a region: the shape refuses."""
    selected, refused, _, _ = _select(
        [("fixture/fp4", RATES_A), ("fixture/swedencentral", RATES_B)])
    assert selected == {}
    assert set(refused) == {HOST}


def test_a_quantization_beside_a_global_endpoint_is_refused():
    """A global endpoint is not a throughput tier: the quantization rule
    needs a tier beside it, so this shape keeps refusing."""
    selected, refused, _, _ = _select(
        [("fixture/fp4", RATES_A), ("fixture/global", RATES_B)])
    assert selected == {}
    assert set(refused) == {HOST}


def test_a_quantization_beside_a_bare_namespace_is_refused():
    selected, refused, _, _ = _select(
        [("fixture/fp4", RATES_A), ("fixture", RATES_B)])
    assert selected == {}
    assert set(refused) == {HOST}


# --- the data region rule ----------------------------------------------------


def test_a_named_data_region_leaves_one_endpoint_rule_resolved():
    selected, refused, notices, _ = _select(
        [("fixture", RATES_A), ("fixture/us", RATES_B)], region="us")
    assert refused == {}
    assert selected[HOST].tag == "fixture/us"
    assert selected[HOST].rates == RATES_B
    assert _rules(notices, "data region")


def test_the_global_region_takes_an_explicit_global_endpoint():
    selected, refused, notices, _ = _select(
        [("fixture/global", RATES_A), ("fixture/europe", RATES_B)])
    assert refused == {}
    assert selected[HOST].tag == "fixture/global"
    assert selected[HOST].rates == RATES_A
    assert _rules(notices, "data region")


def test_an_explicit_global_beats_a_bare_namespace_alias():
    selected, refused, notices, _ = _select(
        [("fixture/global", RATES_A), ("fixture", RATES_B)])
    assert refused == {}
    assert selected[HOST].tag == "fixture/global"
    assert selected[HOST].rates == RATES_A
    assert _rules(notices, "data region")


def test_a_bare_namespace_beats_an_unrecognised_region_spelling():
    """The live Azure shape: the bare namespace endpoint beside a region
    spelled in a form the vocabulary does not carry (azure/swedencentral)."""
    selected, refused, notices, _ = _select(
        [("fixture", RATES_A), ("fixture/swedencentral", RATES_B)])
    assert refused == {}
    assert selected[HOST].tag == "fixture"
    assert selected[HOST].rates == RATES_A
    assert _rules(notices, "data region")


def test_a_global_endpoint_and_its_equal_price_alias_are_interchangeable():
    selected, refused, notices, _ = _select(
        [("fixture/global", RATES_A), ("fixture", RATES_A),
         ("fixture/europe", RATES_B)])
    assert refused == {}
    assert selected[HOST].tag == "fixture/global"
    assert "interchangeable" in " ".join(notices)


def test_a_region_spelling_never_hides_a_quantization_price():
    """The global preference covers region spellings only: a quantization
    price in the same namespace still refuses."""
    selected, refused, notices, _ = _select(
        [("fixture/global", RATES_A), ("fixture/fp8", RATES_B)])
    assert selected == {}
    assert set(refused) == {HOST}
    assert not _rules(notices, "data region")


def test_two_global_prices_are_refused():
    selected, refused, _, _ = _select(
        [("fixture/global", RATES_A), ("fixture/global", RATES_B)])
    assert selected == {}
    assert set(refused) == {HOST}


def test_a_lone_bare_endpoint_is_not_a_rule_resolution():
    """Nothing was filtered or preferred: the single endpoint needs no rule
    and the report stays quiet about it."""
    selected, refused, notices, _ = _select([("fixture", RATES_A)])
    assert refused == {}
    assert selected[HOST].tag == "fixture"
    assert not any("rule-resolved" in note for note in notices)


# --- the same-quantization rule ----------------------------------------------


@pytest.mark.parametrize("cheap, dear", [
    pytest.param({**RATES_A, "read": 0.05, "fresh": 9.0, "output": 90.0},
                 {**RATES_A, "read": 0.5, "fresh": 1.0, "output": 1.0},
                 id="cache-read-decides"),
    pytest.param({**RATES_A, "read": 0.1, "fresh": 0.5, "output": 9.0},
                 {**RATES_A, "read": 0.1, "fresh": 5.0, "output": 1.0},
                 id="input-decides"),
    pytest.param({**RATES_A, "read": 0.1, "fresh": 1.0, "output": 2.0},
                 {**RATES_A, "read": 0.1, "fresh": 1.0, "output": 3.0},
                 id="output-decides"),
])
def test_same_quantization_takes_the_cheapest_by_read_then_input_then_output(
        cheap, dear):
    selected, refused, notices, _ = _select(
        [("fixture/fp8", cheap), ("fixture/fp8", dear)])
    assert refused == {}
    assert selected[HOST].rates == cheap
    assert _rules(notices, "same quantization")


def test_same_quantization_resolves_beside_a_throughput_tier():
    """The live Alibaba shape: one quantization listed twice at two prices,
    beside a dearer throughput tier."""
    selected, refused, notices, _ = _select(
        [("fixture/fp8", RATES_A), ("fixture/fp8", RATES_B),
         ("fixture/fast", RATES_C)])
    assert refused == {}
    assert selected[HOST].tag == "fixture/fp8"
    assert selected[HOST].rates == RATES_A
    assert _rules(notices, "same quantization")


def test_same_quantization_admits_a_same_price_service_variant():
    """The live Mistral shape: one quantization at two prices, with the
    host's `zdr` service variant listed at the cheaper one. The variant
    rides in that price group, so the cheapest quantization endpoint is
    still the row's, and the equal-price tags are interchangeable."""
    selected, refused, notices, _ = _select(
        [("fixture/zdr", RATES_A), ("fixture/nvfp4", RATES_A),
         ("fixture/nvfp4", RATES_B)])
    assert refused == {}
    assert selected[HOST].tag == "fixture/nvfp4"
    assert selected[HOST].rates == RATES_A
    assert _rules(notices, "same quantization")
    assert "interchangeable" in " ".join(notices)


def test_a_second_offering_inside_a_quantization_price_group_is_refused():
    """A region spelling at a quantization price is another offering, not a
    tier: the group is no longer one quantization's price."""
    selected, refused, _, _ = _select(
        [("fixture/fp8", RATES_A), ("fixture/swedencentral", RATES_A),
         ("fixture/fp8", RATES_B)])
    assert selected == {}
    assert set(refused) == {HOST}


def test_same_quantization_reports_a_possible_twin_switch():
    current = {**RATES_A, "read": 0.15, "fresh": 2.0, "output": 10.0}
    dearer = {**RATES_A, "read": 0.3, "fresh": 3.0, "output": 15.0}
    selected, refused, notices, _ = _select(
        [("fixture/fp8", current), ("fixture/fp8", dearer)], stored=RATES_A)
    assert refused == {}
    assert selected[HOST].rates == current
    assert "possible twin switch" in " ".join(notices)


def test_same_quantization_flip_is_refused():
    """The row's own price is no longer the cheaper one: which endpoint the
    account reaches is exactly what a human must check."""
    cheap = {**RATES_A, "read": 0.05}
    dear = {**RATES_A, "read": 0.5}
    selected, refused, _, _ = _select(
        [("fixture/fp8", cheap), ("fixture/fp8", dear)], stored=dear)
    assert selected == {}
    assert "flipped" in refused[HOST]


def test_same_quantization_tie_is_refused():
    """Same cache read, input and output at different prices: the order
    says nothing about which twin is which."""
    cheap = {**RATES_A, "create_1h": 2.0}
    dear = {**RATES_A, "create_1h": 4.0}
    selected, refused, _, _ = _select(
        [("fixture/fp8", cheap), ("fixture/fp8", dear)])
    assert selected == {}
    assert "tie" in refused[HOST]


def test_two_different_quantizations_are_refused():
    selected, refused, _, _ = _select(
        [("fixture/fp4", RATES_A), ("fixture/fp8", RATES_B)])
    assert selected == {}
    assert set(refused) == {HOST}


def test_a_quantization_beside_a_region_spelling_and_a_tier_is_refused():
    """The same-quantization rule admits service tiers beside it, never an
    unclassifiable sibling."""
    selected, refused, _, _ = _select(
        [("fixture/fp8", RATES_A), ("fixture/fp8", RATES_B),
         ("fixture/swedencentral", RATES_C)])
    assert selected == {}
    assert set(refused) == {HOST}


# --- an explicit resolution keeps precedence ---------------------------------


@pytest.mark.parametrize("pin, listings, tag", [
    pytest.param({"tag": "fixture/fast", "why": "fixture"},
                 [("fixture", RATES_A), ("fixture/fast", RATES_B)],
                 "fixture/fast", id="over-the-bare-rule"),
    pytest.param({"tag": "fixture/fast", "why": "fixture"},
                 [("fixture/fp4", RATES_A), ("fixture/fast", RATES_B)],
                 "fixture/fast", id="over-the-quantization-rule"),
    pytest.param({"tag": "fixture/fp8", "why": "fixture"},
                 [("fixture/fp8", RATES_A), ("fixture/fast", RATES_C)],
                 "fixture/fp8", id="narrowing-to-the-quantization"),
    pytest.param({"tag": "fixture/europe", "why": "fixture"},
                 [("fixture/global", RATES_A), ("fixture/europe", RATES_B)],
                 "fixture/europe", id="over-the-region-rule"),
])
def test_an_explicit_pin_keeps_precedence(pin, listings, tag):
    selected, refused, notices, _ = _select(listings, resolutions={HOST: pin})
    assert refused == {}
    assert selected[HOST].tag == tag
    assert not any("rule-resolved" in note for note in notices)


def test_a_pinned_tag_with_two_prices_still_needs_a_human():
    """A tag pin narrows to the pinned tag; two prices under it are still
    the human's call — no rule resolves within a pin."""
    selected, refused, _, _ = _select(
        [("fixture/fast", RATES_A), ("fixture/fast", RATES_B)],
        resolutions={HOST: {"tag": "fixture/fast", "why": "fixture"}})
    assert selected == {} and set(refused) == {HOST}


# --- the live shape, end to end ----------------------------------------------


def test_the_alibaba_stopgap_pins_are_gone_from_the_document():
    """#893's two cheapest pins were the stopgap this rule replaces."""
    doc = _openrouter_template()
    for key in ("deepseek/deepseek-v4-pro", "glm-5-2"):
        assert "Alibaba" not in doc["models"][key].get("resolve", {})


def test_the_alibaba_shape_resolves_without_its_stopgap_pins(tmp_path, capsys):
    """The live shape that redded four hourly refreshes: two alibaba/fp8
    endpoints at two prices, beside an alibaba/fast tier. With the pins
    gone, the same-quantization rule takes the cheaper fp8 endpoint and the
    run is green with the rule-resolved line in its report. The live hosts
    are deepseek/deepseek-v4-pro and glm-5-2; the fixture world carries the
    former, glm-5-2 being a vendor-tracked key it drops."""
    run = Run(tmp_path)
    keys = ("deepseek/deepseek-v4-pro", "deepseek/deepseek-v4-pro-0813")
    cheap = {**RATES_A, "read": 0.118}
    dear = {**RATES_B, "read": 0.13}
    for key in keys:
        run.endpoints(key)[:] = [
            e for e in run.endpoints(key) if e["provider_name"] != "Alibaba"] + [
            _endpoint("Alibaba", cheap, tag="alibaba/fp8"),
            _endpoint("Alibaba", dear, tag="alibaba/fp8"),
            _endpoint("Alibaba", RATES_C, tag="alibaba/fast")]
    rc, out, err = run(capsys)
    assert rc == 0, err
    assert "rule-resolved (same quantization)" in out
    for key in keys:
        assert run.doc()["providers"][key]["Alibaba"][-1] == {"from": STAMP, **cheap}
