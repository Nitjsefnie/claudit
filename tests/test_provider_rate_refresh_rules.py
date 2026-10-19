"""endpoint-selection rules beneath explicit pins.

Synthetic listings cover namespace, quantization, region and price continuity.
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


def test_the_alibaba_stopgap_pins_are_gone_from_the_document():
    """The rule replaces explicit pins for choosing the cheapest duplicate endpoint."""
    doc = _openrouter_template()
    for model_id in ("deepseek/deepseek-v4-pro", "z-ai/glm-5.2"):
        entry = next(entry for entry in doc["models"].values()
                     if entry["id"] == model_id)
        assert "Alibaba" not in entry.get("resolve", {})


def test_the_alibaba_shape_resolves_without_its_stopgap_pins(tmp_path, capsys):
    """take the cheaper fp8 twin beside an alibaba/fast tier.

    Vendor-tracked snapshots need explicit synthetic seed rows.
    """
    run = Run(tmp_path)
    tracked = _openrouter_template()["models"]
    keys = [next(key for key, entry in tracked.items() if entry["id"] == model_id)
            for model_id in ("deepseek/deepseek-v4-pro",
                             "deepseek/deepseek-v4-pro-0813")]
    cheap = {**RATES_A, "read": 0.118}
    dear = {**RATES_B, "read": 0.13}

    def seed_subjects(doc):
        for key in keys:
            doc["openrouter"]["models"][key] = {"id": tracked[key]["id"]}
            doc["providers"][key] = {"Alibaba": [{"from": None, **RATES_B}]}

    run.edit(seed_subjects)
    for key in keys:
        run.payloads[tracked[key]["id"]] = {"data": {"endpoints": [
            _endpoint("Alibaba", cheap, tag="alibaba/fp8"),
            _endpoint("Alibaba", dear, tag="alibaba/fp8"),
            _endpoint("Alibaba", RATES_C, tag="alibaba/fast")]}}
    rc, out, err = run(capsys)
    assert rc == 0, err
    assert "rule-resolved (same quantization)" in out
    for key in keys:
        assert run.doc()["providers"][key]["Alibaba"][-1] == {"from": STAMP, "context_length": 131072, **cheap}


@pytest.mark.parametrize("quantization, suffixes", [
    ("fp4", ["flex"]),
    ("fp8", ["flex", "priority"]),
    ("fp8", ["future/service", "another-new-tier"]),
])
@pytest.mark.parametrize("stored", [None, RATES_B])
def test_tier_variant_base_wins_over_arbitrary_nested_suffixes(
        quantization, suffixes, stored):
    base = f"fixture/{quantization}"
    variants = [(f"{base}/{suffix}", rates)
                for suffix, rates in zip(suffixes, (RATES_A, RATES_C))]
    selected, refused, notices, _ = _select(
        [*variants, (base, RATES_B)], stored=stored)
    assert refused == {}
    assert selected[HOST].tag == base
    assert selected[HOST].rates == RATES_B
    notice = _rules(notices, "tier variant base")[0]
    assert all(tag in notice for tag, _ in variants)


@pytest.mark.parametrize("field, value", [
    ("quantization", "fp4"), ("context_length", 262144),
    ("max_completion_tokens", 8192), ("max_prompt_tokens", 65536),
])
@pytest.mark.parametrize("stored", [None, RATES_B])
def test_tier_identity_mismatch_declines_or_uses_stored_continuity(field, value, stored):
    base = _endpoint(HOST, RATES_B, tag="fixture/fp8")
    variant = {**_endpoint(HOST, RATES_A, tag="fixture/fp8/flex"), field: value}
    selected, refused, notices, _ = refresh.listed_rows(
        MODEL, {"data": {"endpoints": [base, variant]}}, None, {},
        {HOST: stored} if stored else {}, NOW)
    assert not _rules(notices, "tier variant base")
    if stored is None:
        assert selected == {} and set(refused) == {HOST}
    else:
        assert refused == {}
        assert selected[HOST].tag == "fixture/fp8"
        assert _rules(notices, "stored price continuity")


def test_tier_base_allows_equal_price_aliases_in_its_group():
    selected, refused, notices, _ = _select([
        ("other/fp8", RATES_B), ("fixture/fp8", RATES_B),
        ("fixture/fp8/new-tier", RATES_A)])
    assert refused == {}
    assert selected[HOST].tag == "fixture/fp8"
    assert "interchangeable" in _rules(notices, "tier variant base")[0]


@pytest.mark.parametrize("other", ["fixture/fp8ish/flex", "other/fp8/flex"])
def test_tier_base_requires_every_other_endpoint_to_extend_its_exact_tag(other):
    selected, refused, _, _ = _select([
        ("fixture/fp8", RATES_B), ("fixture/fp8/flex", RATES_A), (other, RATES_A)])
    assert selected == {} and set(refused) == {HOST}


@pytest.mark.parametrize("suffix", ["global", "us", "us-east-1"])
def test_quantization_region_twins_keep_the_cheapest_rule(suffix):
    region = None if suffix == "global" else suffix
    selected, refused, notices, _ = _select([
        (f"fixture/fp8/{suffix}", RATES_B), (f"fixture/fp8/{suffix}", RATES_A)],
        region=region)
    assert refused == {}
    assert selected[HOST].rates == RATES_A
    assert _rules(notices, "same quantization")


@pytest.mark.parametrize("suffix", ["fast", "flex", "priority", "new-tier"])
def test_quantization_service_extensions_are_never_price_twins(suffix):
    tag = f"fixture/fp8/{suffix}"
    selected, refused, _, _ = _select([(tag, RATES_A), (tag, RATES_B)])
    assert selected == {} and set(refused) == {HOST}


def test_stored_price_continuity_takes_first_of_equal_price_tags():
    selected, refused, notices, _ = _select([
        ("fixture/other", RATES_A), ("fixture/primary", RATES_B),
        ("second/twin", RATES_B), ("third/unrelated", RATES_C)], stored=RATES_B)
    assert refused == {}
    assert selected[HOST].tag == "fixture/primary"
    notice = _rules(notices, "stored price continuity")[0]
    assert "interchangeable" in notice and "second/twin" in notice


def test_continuity_follows_sibling_at_stored_price_when_tracked_endpoint_reprices():
    selected, refused, _, _ = _select([
        ("fixture/tracked", RATES_A)], stored=RATES_A)
    assert refused == {} and selected[HOST].tag == "fixture/tracked"

    selected, refused, notices, _ = _select([
        ("fixture/tracked", RATES_B), ("second/new-sibling", RATES_A)], stored=RATES_A)
    assert refused == {}
    assert selected[HOST].tag == "second/new-sibling"
    assert selected[HOST].rates == RATES_A
    notice = _rules(notices, "stored price continuity")[0]
    assert "kept the endpoint 'second/new-sibling' at the row's price" in notice
    assert "assumes the tracked endpoint did not reprice this hour" in notice


@pytest.mark.parametrize("field", list(RATES_A))
def test_continuity_compares_all_five_token_rates_at_ten_decimals(field):
    listed = {**RATES_B, field: RATES_B[field] + 0.000000000001, "web_search": 0.7}
    selected, refused, notices, _ = _select([
        ("fixture/other", RATES_A), ("fixture/primary", listed)],
        stored={**RATES_B, "web_search": 0.1})
    assert refused == {}
    assert selected[HOST].tag == "fixture/primary"
    assert _rules(notices, "stored price continuity")

    selected, refused, _, _ = _select([
        ("fixture/other", RATES_A),
        ("fixture/primary", {**RATES_B, field: RATES_B[field] + 0.000000001})],
        stored=RATES_B)
    assert selected == {} and set(refused) == {HOST}


def test_continuity_ignores_a_changed_schedule():
    endpoint = _endpoint(HOST, RATES_B, tag="fixture/primary")
    endpoint["pricing"]["overrides"] = [{
        "utc_start": 100, "utc_end": 200, "prompt": "0.0000005"}]
    selected, refused, notices, _ = refresh.listed_rows(
        MODEL, {"data": {"endpoints": [
            _endpoint(HOST, RATES_A, tag="fixture/other"), endpoint]}},
        None, {}, {HOST: RATES_B}, NOW)
    assert refused == {}
    assert selected[HOST].tag == "fixture/primary"
    assert selected[HOST].schedule
    assert _rules(notices, "stored price continuity")


def test_continuity_requires_exactly_one_matching_price_group():
    selected, refused, notices, _ = _select([
        ("fixture/primary", {**RATES_B, "web_search": 0.1}),
        ("fixture/twin", {**RATES_A, "web_search": 0.2})], stored=RATES_B)
    assert refused == {}
    assert selected[HOST].tag == "fixture/primary"
    assert _rules(notices, "stored price continuity")

    selected, refused, _, _ = _select([
        ("fixture/primary", {**RATES_B, "web_search": 0.1}),
        ("fixture/twin", {**RATES_B, "web_search": 0.2})], stored=RATES_B)
    assert selected == {} and set(refused) == {HOST}


@pytest.mark.parametrize("stored", [None, {**RATES_C, "read": 0.9}])
def test_unrelated_multi_price_host_without_stored_match_still_refuses(stored):
    """Deliberate refusal: neither a structural fit nor a stored-price match."""
    selected, refused, _, _ = _select([
        ("fixture/first", RATES_A), ("second/another", RATES_B),
        ("third/unrelated", RATES_C)], stored=stored)
    assert selected == {} and set(refused) == {HOST}


def test_structural_rule_precedes_stored_price_continuity():
    selected, refused, notices, _ = _select([
        ("fixture/fp8", RATES_B), ("fixture/fp8/flex", RATES_A)], stored=RATES_A)
    assert refused == {}
    assert selected[HOST].tag == "fixture/fp8"
    assert _rules(notices, "tier variant base")


@pytest.mark.parametrize("pin, expected_tag, refusal", [
    ({"tag": "fixture/fp8/flex", "why": "fixture"}, "fixture/fp8/flex", None),
    ({"select": "cheapest", "why": "fixture"}, None, "identical"),
])
def test_explicit_pin_precedes_tier_base_and_continuity(pin, expected_tag, refusal):
    selected, refused, notices, _ = _select([
        ("fixture/fp8", RATES_B), ("fixture/fp8/flex", RATES_A)],
        resolutions={HOST: pin}, stored=RATES_B)
    assert not any("rule-resolved" in note for note in notices)
    if refusal:
        assert selected == {} and refusal in refused[HOST]
    else:
        assert refused == {} and selected[HOST].tag == expected_tag


def test_explicit_cheapest_pin_flip_still_refuses_before_continuity():
    selected, refused, _, _ = _select([
        ("fixture/fp8", RATES_A), ("fixture/fp8", RATES_B)],
        resolutions={HOST: {"select": "cheapest", "why": "fixture"}}, stored=RATES_B)
    assert selected == {} and "flipped" in refused[HOST]


def test_tag_only_pin_never_uses_stored_price_continuity():
    selected, refused, _, _ = _select([
        ("fixture/unknown", RATES_A), ("fixture/unknown", RATES_B)],
        resolutions={HOST: {"tag": "fixture/unknown", "why": "fixture"}}, stored=RATES_B)
    assert selected == {} and set(refused) == {HOST}
