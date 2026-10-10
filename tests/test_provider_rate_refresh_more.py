"""Tests moved from test_provider_rate_refresh.py to keep test modules under 700 lines."""
from __future__ import annotations

import copy
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from backend import pricing
from tests.refresh_fixture_builders import _per_token

from tests.test_provider_rate_log_refresh import _series as _log_series
from tests.test_provider_rate_refresh import (
    GLM,
    NOW,
    _openrouter_template,
    _browser_pricing,
    Run,
    STAMP,
    UTC,
    V41,
    _endpoint,
    _move_openinference,
    _refused,
    _two_global_novitas,
    needs_node,
    _load,
)
from tests.test_provider_rate_refresh_pins import (
    _baseten_twins, _novita_region_twin, _pin_novita,
)

refresh = _load()


def test_the_global_endpoint_is_taken_over_a_region_one(tmp_path, capsys):
    """The global price moving is a move even when a region twin exists."""
    run = Run(tmp_path)
    seeded = run.doc()["providers"][GLM]["Novita"][-1]
    _novita_region_twin(run)
    run.endpoint(GLM, "Novita")["pricing"]["completion"] = _per_token(seeded["output"] * 2)
    assert run(capsys)[0] == 0
    entry = run.doc()["providers"][GLM]["Novita"][-1]
    assert (entry["from"], entry["read"], entry["output"]) == (STAMP, seeded["read"], seeded["output"] * 2)


def test_a_region_endpoint_moving_alone_moves_nothing(tmp_path, capsys):
    run = Run(tmp_path)
    _novita_region_twin(run)["pricing"]["completion"] = "0.000002"
    before = run.snapshot()
    assert run(capsys)[0] == 0
    assert run.snapshot() == before


def test_sail_research_takes_its_global_endpoint(tmp_path, capsys):
    """Its deepseek-v4-flash-0731 is listed as sail-research/fp4 and
    sail-research/us at different prices; the global one applies."""
    run = Run(tmp_path)
    fp4 = {"fresh": 0.03, "create_5m": 0.03, "create_1h": 0.03,
           "read": 0.016, "output": 0.55}
    us_region = {"fresh": 0.038, "create_5m": 0.038, "create_1h": 0.038,
                 "read": 0.0228, "output": 0.55}
    model = "deepseek/deepseek-v4-flash-0731"
    run.endpoints(model)[:] = [
        e for e in run.endpoints(model) if e["provider_name"] != "Sail Research"] + [
        _endpoint("Sail Research", fp4, tag="sail-research/fp4"),
        _endpoint("Sail Research", us_region, tag="sail-research/us")]
    assert run(capsys)[0] == 0
    assert run.doc()["providers"][model]["Sail Research"][-1] == {"from": STAMP, **fp4}


def test_baseten_selects_the_global_synthetic_endpoint_over_a_cheaper_region() -> None:
    global_rates = {
        "fresh": 0.25, "create_5m": 0.25, "create_1h": 0.25,
        "read": 0.125, "output": 1.0,
    }
    regional_rates = {
        "fresh": 0.125, "create_5m": 0.125, "create_1h": 0.125,
        "read": 0.0625, "output": 0.5,
    }
    payload = {"data": {"endpoints": [
        _endpoint("BaseTen", global_rates, tag="baseten/fp8"),
        _endpoint("BaseTen", regional_rates, tag="baseten/us"),
    ]}}
    selected, refused, _, _ = refresh.listed_rows(
        "deepseek/acme-v4-1", payload, None,
        {"BaseTen": {"select": "cheapest", "why": "synthetic region check"}},
        {}, NOW,
    )

    assert refused == {}
    assert selected["BaseTen"].tag == "baseten/fp8"
    assert selected["BaseTen"].rates == global_rates


def test_modal_ignores_withdrawn_fp8_when_nvfp4_survives() -> None:
    withdrawn_rates = {
        "fresh": 0.45, "create_5m": 0.45, "create_1h": 0.45,
        "read": 0.225, "output": 1.5,
    }
    surviving_rates = {
        "fresh": 0.25, "create_5m": 0.25, "create_1h": 0.25,
        "read": 0.125, "output": 0.75,
    }
    payload = {"data": {"endpoints": [
        _endpoint("Modal", withdrawn_rates, tag="modal/fp8"),
        _endpoint("Modal", surviving_rates, tag="modal/nvfp4"),
    ]}}
    selected, refused, _, _ = refresh.listed_rows(
        GLM, payload, None,
        {"Modal": {"tag": "modal/nvfp4", "why": "synthetic survivor check"}},
        {}, NOW,
    )

    assert refused == {}
    assert selected["Modal"].tag == "modal/nvfp4"
    assert selected["Modal"].rates == surviving_rates


# --- the bare-namespace rule over a /fast tier --------------------------------
# OpenRouter lists a /fast throughput tier under its own tag beside a host's
# base endpoint. The bare namespace rule (#877) takes the base endpoint: a
# tier is a distinct offering, not a price twin. Shapes no rule fits refuse.

BASE_RATES = {"fresh": 0.17, "create_5m": 0.17, "create_1h": 0.17,
              "read": 0.169, "output": 3.0}
FAST_RATES = {"fresh": 0.23, "create_5m": 0.23, "create_1h": 0.23,
              "read": 0.33, "output": 2.6}
THIRD_RATES = {"fresh": 0.4, "create_5m": 0.4, "create_1h": 0.4,
               "read": 0.028, "output": 1.1}


def _fast_payload(tags: list[str]) -> dict:
    """A Fireworks listing over the given tags, each at its own price."""
    rates = [BASE_RATES, FAST_RATES, THIRD_RATES]
    return {"data": {"endpoints": [
        _endpoint("Fireworks", rates[i], tag=tags[i]) for i in range(len(tags))]}}


def test_a_fast_pair_takes_the_base_endpoint_automatically() -> None:
    """Issue #623: a /fast tier is a distinct offering under its own tag,
    not a price twin, so the exact {p, p/fast} pair resolves by rule — the
    base endpoint prices the row — and the run's report records it as
    rule-resolved rather than silently."""
    selected, refused, notices, _ = refresh.listed_rows(
        GLM, _fast_payload(["fireworks", "fireworks/fast"]), None, {}, {}, NOW)
    assert refused == {}
    assert selected["Fireworks"].tag == "fireworks"
    assert selected["Fireworks"].rates == BASE_RATES
    assert any("rule-resolved" in notice and "Fireworks" in notice
               for notice in notices)


def test_the_base_is_taken_even_when_the_fast_tier_is_cheaper() -> None:
    """The rule is shape-based, not price-based: a /fast tier cheaper than
    its base does not turn the pair into a price-twin choice."""
    cheap_fast = {**BASE_RATES, "fresh": 0.05, "read": 0.001, "output": 1.0}
    payload = {"data": {"endpoints": [
        _endpoint("Fireworks", BASE_RATES, tag="fireworks"),
        _endpoint("Fireworks", cheap_fast, tag="fireworks/fast"),
    ]}}
    selected, refused, _, _ = refresh.listed_rows(
        GLM, payload, None, {}, {}, NOW)
    assert refused == {}
    assert selected["Fireworks"].rates == BASE_RATES


@pytest.mark.parametrize("tags", [
    pytest.param(["fireworks", "fireworks/fp4"], id="p-and-p-other"),
    pytest.param(["fireworks", "fireworks/fast", "fireworks/fp4"],
                 id="fast-pair-plus-a-third-price"),
    pytest.param(["fireworks/fp4", "fp8"], id="two-non-fast-tags"),
])
def test_any_other_multi_price_shape_is_still_refused(tags: list[str]) -> None:
    """Each of these shapes fits no automatic rule — a bare namespace
    beside a quantization, a third price beside a fast pair, two
    non-fast tags — so it keeps refusing, as does every shape whose
    endpoints are several offerings rather than variants of one."""
    selected, refused, _, _ = refresh.listed_rows(
        GLM, _fast_payload(tags), None, {}, {}, NOW)
    assert selected == {}
    assert set(refused) == {"Fireworks"}
    assert "resolve it in openrouter.models" in refused["Fireworks"]


def test_an_explicit_pin_beats_the_bare_namespace_rule() -> None:
    """A resolve entry keeps precedence: a pin on the fast tag tracks the
    fast endpoint, and no rule-resolved line is reported."""
    selected, refused, notices, _ = refresh.listed_rows(
        GLM, _fast_payload(["fireworks", "fireworks/fast"]), None,
        {"Fireworks": {"tag": "fireworks/fast", "why": "fixture"}}, {}, NOW)
    assert refused == {}
    assert selected["Fireworks"].tag == "fireworks/fast"
    assert not any("rule-resolved" in notice for notice in notices)


def test_a_cheapest_pin_on_a_fast_pair_still_refuses() -> None:
    """An automatic rule never rescues an explicit resolution: a 'cheapest'
    pin on a {p, p/fast} pair keeps refusing — the tags differ, so the twins
    are not identical."""
    selected, refused, _, _ = refresh.listed_rows(
        GLM, _fast_payload(["fireworks", "fireworks/fast"]), None,
        {"Fireworks": {"select": "cheapest", "why": "fixture"}}, {}, NOW)
    assert selected == {}
    assert set(refused) == {"Fireworks"}
    assert "identical" in refused["Fireworks"]


def test_an_untagged_endpoint_is_never_a_fast_pair_base() -> None:
    """The empty-tag limb: an untagged endpoint is never the base of a fast
    pair — "" + "/fast" spells "/fast" only as a string accident, so the
    shape keeps refusing instead of rule-resolving."""
    untagged = _endpoint("Fireworks", BASE_RATES)
    untagged["tag"] = ""
    payload = {"data": {"endpoints": [
        untagged, _endpoint("Fireworks", FAST_RATES, tag="/fast")]}}
    selected, refused, _, _ = refresh.listed_rows(
        GLM, payload, None, {}, {}, NOW)
    assert selected == {}
    assert set(refused) == {"Fireworks"}
    assert "resolve it in openrouter.models" in refused["Fireworks"]


def _fast_log_series(states: list[tuple[str, dict]]) -> dict:
    """One Fireworks log series, reshaped from the log fixtures' Wafer one."""
    series = _log_series(states)
    series["providerName"], series["providerSlug"] = "Fireworks", "fireworks"
    return series


def test_a_fast_pair_does_not_extend_to_the_log_selection() -> None:
    """SV-RATE-REFRESH's log boundary: no automatic rule extends to the
    price log's own endpoint selection — a {p, p/fast} host the log
    would otherwise back stays sampled unless the base endpoint is pinned."""
    payload = {"data": {"endpoints": [
        _endpoint("Fireworks", BASE_RATES, tag="fireworks"),
        _endpoint("Fireworks", FAST_RATES, tag="fireworks/fast"),
    ]}}
    series = refresh.refresh_pricelog.read_log_payload({"data": {"series": [
        _fast_log_series([("2030-12-31T23:00:00Z", BASE_RATES)]),
        _fast_log_series([("2030-12-31T23:00:00Z", FAST_RATES)]),
    ]}})
    unpinned = refresh.refresh_pricelog.join_listed_pricing(
        payload, series, None, {}, NOW)["Fireworks"]
    assert unpinned.entries is None
    assert unpinned.reason == "endpoint selection found 2 endpoints"
    pinned = refresh.refresh_pricelog.join_listed_pricing(
        payload, series, None,
        {"Fireworks": {"tag": "fireworks", "why": "fixture"}}, NOW)["Fireworks"]
    assert pinned.entries is not None


def test_a_new_fast_pair_host_gets_a_row_for_its_base(tmp_path, capsys) -> None:
    """End to end, the shape that redded master before #623: a host with a
    stored row at the base price is suddenly listed as {p, p/fast}. The
    rule takes the base endpoint, the stored row still matches it, and the
    run is green with the rule-resolved line in its report."""
    run = Run(tmp_path)
    seeded = run.endpoint(GLM, "Fireworks")
    seeded["tag"] = "fireworks"
    fast = copy.deepcopy(seeded)
    fast["tag"] = "fireworks/fast"
    fast["pricing"]["prompt"] = _per_token(0.9)
    run.endpoints(GLM).append(fast)
    before = run.snapshot()
    rc, out, _ = run(capsys)
    assert rc == 0
    assert run.snapshot() == before, "the base is the stored price; nothing appends"
    assert "rule-resolved" in out


def test_a_moved_base_price_beside_a_fast_tier_appends_the_base(
        tmp_path, capsys) -> None:
    """The rule-resolved base is the row's price: when it moves while the
    fast tier is listed beside it, the base's move is the one appended."""
    run = Run(tmp_path)
    seeded = run.endpoint(GLM, "Fireworks")
    seeded["tag"] = "fireworks"
    seeded["pricing"]["prompt"] = _per_token(0.3)
    fast = copy.deepcopy(seeded)
    fast["tag"] = "fireworks/fast"
    fast["pricing"]["prompt"] = _per_token(0.42)
    run.endpoints(GLM).append(fast)
    assert run(capsys)[0] == 0
    entry = run.doc()["providers"][GLM]["Fireworks"][-1]
    assert (entry["from"], entry["fresh"]) == (STAMP, 0.3)


def test_a_host_listed_only_in_a_region_is_reported_vanished(tmp_path, capsys):
    run = Run(tmp_path)
    run.endpoint(GLM, "Cloudflare")["tag"] = "cloudflare/us"
    before = run.snapshot()
    rc, out, _ = run(capsys)
    assert rc == 0
    assert run.snapshot() == before
    assert re.search(r"vanished\b.*Cloudflare", out)


def test_a_model_with_no_endpoint_in_the_data_region_is_refused(tmp_path, capsys):
    run = Run(tmp_path)
    for endpoint in run.endpoints(GLM):
        endpoint["tag"] = endpoint["tag"].split("/")[0] + "/us"
    _refused(run, capsys, GLM)


def test_a_named_data_region_takes_that_region(tmp_path, capsys):
    run = Run(tmp_path)
    run.edit(lambda doc: doc["openrouter"].update({"data_region": "us"}))
    for model in run.doc()["providers"]:
        resolved = run.doc()["openrouter"]["models"][model].get("resolve", {})
        for endpoint in run.endpoints(model):
            if endpoint["provider_name"] in resolved:
                continue
            endpoint["tag"] = endpoint["tag"].split("/")[0] + "/us"
    global_novita = copy.deepcopy(run.endpoint(GLM, "Novita"))
    global_novita["tag"] = "novita"
    global_novita["pricing"]["input_cache_read"] = "0.00000009"
    run.endpoints(GLM).append(global_novita)
    run.endpoint(GLM, "Novita")["pricing"]["input_cache_read"] = "0.00000005"
    assert run(capsys)[0] == 0
    assert run.doc()["providers"][GLM]["Novita"][-1]["read"] == 0.05


@pytest.mark.parametrize("region", [None, "", "US", "the-us", 5])
def test_a_malformed_data_region_is_refused(tmp_path, capsys, region):
    run = Run(tmp_path)
    run.edit(lambda doc: doc["openrouter"].update({"data_region": region}))
    _refused(run, capsys, "data_region")


def test_a_tag_override_takes_its_endpoint(tmp_path, capsys):
    run = Run(tmp_path)
    _two_global_novitas(run)
    run.edit(_pin_novita("novita/fp4"))
    assert run(capsys)[0] == 0
    entry = run.doc()["providers"][GLM]["Novita"][-1]
    assert (entry["from"], entry["read"]) == (STAMP, 0.05)


def test_a_tag_override_takes_its_endpoint_whatever_its_region(tmp_path, capsys):
    run = Run(tmp_path)
    _novita_region_twin(run)
    run.edit(_pin_novita("novita/us"))
    assert run(capsys)[0] == 0
    assert run.doc()["providers"][GLM]["Novita"][-1]["read"] == 0.05


def test_a_tag_override_naming_no_listed_tag_is_refused(tmp_path, capsys):
    run = Run(tmp_path)
    _two_global_novitas(run)
    run.edit(_pin_novita("novita/int4"))
    _refused(run, capsys, "Novita", "novita/int4", "novita/fp8")


@pytest.mark.parametrize("pin", [
    pytest.param({"match": {"read": 0.0264}}, id="rate-only"),
    pytest.param({"tag": "novita", "match": {"read": 0.0264}}, id="tag-and-rate"),
])
def test_a_resolution_keyed_on_a_rate_is_refused(tmp_path, capsys, pin):
    """A pin on a price stops matching the moment that price moves, which
    turns every run red for exactly the hosts it was meant to settle."""
    run = Run(tmp_path)
    run.edit(lambda doc: doc["openrouter"]["models"][GLM].update(
        {"resolve": {"Novita": pin}}))
    _refused(run, capsys, "Novita", "keyed on 'tag'")


def test_baseten_is_resolved_by_price_order_as_data():
    doc = _openrouter_template()
    pin = doc["models"][V41]["resolve"]["BaseTen"]
    assert pin["select"] == "cheapest" and pin["why"]
    assert set(pin) == {"tag", "select", "ignore", "why"}
    assert pin["tag"] == "baseten/fp8"
    assert pin["ignore"] == ["max_completion_tokens"]


def test_identical_twins_without_an_override_resolve_to_the_cheaper(tmp_path, capsys):
    """#877: one quantization's price twins are one offering priced twice,
    so the same-quantization rule takes the cheaper without a resolution.
    The recorded pin's own `ignore` machinery still governs a pinned host
    (the tests below), and a genuinely ambiguous shape still refuses."""
    run = Run(tmp_path)
    run.edit(lambda doc: doc["openrouter"]["models"][V41].pop("resolve"))
    cheaper, _ = _baseten_twins(run)
    before = run.snapshot()
    rc, out, _ = run(capsys)
    assert rc == 0
    assert "rule-resolved (same quantization)" in out
    # The rule takes the cheaper twin, which is the price the row already
    # holds: nothing appends. The dearer twin would have moved the row.
    assert run.snapshot() == before
    assert Decimal(cheaper["pricing"]["input_cache_read"]).scaleb(6) == Decimal("0.007")


def test_a_moved_price_on_the_cheaper_twin_is_appended(tmp_path, capsys):
    run = Run(tmp_path)
    cheaper, _ = _baseten_twins(run)
    cheaper["pricing"]["input_cache_read"] = "0.000000008"
    cheaper["pricing"]["completion"] = "0.0000013"
    assert run(capsys)[0] == 0
    entry = run.doc()["providers"][V41]["BaseTen"][-1]
    assert (entry["from"], entry["read"], entry["output"]) == (STAMP, 0.008, 1.3)


def test_a_moved_price_on_the_dearer_twin_moves_nothing(tmp_path, capsys):
    run = Run(tmp_path)
    _, dearer = _baseten_twins(run)
    dearer["pricing"]["completion"] = "0.000002"
    before = run.snapshot()
    assert run(capsys)[0] == 0
    assert run.snapshot() == before


def test_a_flip_in_order_is_refused(tmp_path, capsys):
    """The twin whose price the row holds is no longer the cheaper one:
    which twin the account reaches is exactly what a human must check."""
    run = Run(tmp_path)
    _, dearer = _baseten_twins(run)
    dearer["pricing"]["input_cache_read"] = "0.000000005"
    _refused(run, capsys, f"{V41} via BaseTen", "order")


@pytest.mark.parametrize("field, value", [
    pytest.param("prompt", "0.0000002", id="fresh"),
    pytest.param("completion", "0.0000011", id="output"),
])
def test_cheapest_compares_read_then_fresh_then_output(tmp_path, capsys, field, value):
    """Equal cache reads fall to the input price, then the output price."""
    run = Run(tmp_path)
    cheaper, dearer = _baseten_twins(run)
    cheaper["pricing"]["input_cache_read"] = "0.000000009"
    dearer["pricing"]["input_cache_read"] = "0.000000009"
    cheaper["pricing"][field] = value
    assert run(capsys)[0] == 0
    entry = run.doc()["providers"][V41]["BaseTen"][-1]
    assert entry["from"] == STAMP
    assert entry["fresh" if field == "prompt" else "output"] == float(
        Decimal(value).scaleb(6))
    assert entry["read"] == 0.009


def test_cache_read_decides_before_the_input_price(tmp_path, capsys):
    run = Run(tmp_path)
    cheaper, _ = _baseten_twins(run)
    cheaper["pricing"]["input_cache_read"] = "0.000000008"
    cheaper["pricing"]["prompt"] = "0.0000005"
    assert run(capsys)[0] == 0
    entry = run.doc()["providers"][V41]["BaseTen"][-1]
    assert (entry["read"], entry["fresh"]) == (0.008, 0.5)


def test_a_tie_between_different_prices_is_refused(tmp_path, capsys):
    """Same cache read, input and output, different cache write: the order
    says nothing about which twin is which."""
    run = Run(tmp_path)
    cheaper, dearer = _baseten_twins(run)
    for field in ("prompt", "completion", "input_cache_read"):
        dearer["pricing"][field] = cheaper["pricing"][field]
    dearer["pricing"]["input_cache_write"] = "0.0000009"
    _refused(run, capsys, f"{V41} via BaseTen", "tie")


@pytest.mark.parametrize("field, value", [
    pytest.param("quantization", "fp4", id="quantization"),
    pytest.param("context_length", 65536, id="context-length"),
    pytest.param("max_completion_tokens", 1024, id="max-completion"),
])
def test_cheapest_applies_only_to_otherwise_identical_twins(
        tmp_path, capsys, field, value):
    """The recorded ignore covers only its named fields; under a pin with no
    recorded ignore every identity field is compared."""
    run = Run(tmp_path)
    run.edit(lambda doc: doc["openrouter"]["models"][V41].update(
        {"resolve": {"BaseTen": {"tag": "baseten/fp8", "select": "cheapest",
                                 "why": "synthetic twin check"}}}))
    cheaper, dearer = _baseten_twins(run)
    cheaper["max_completion_tokens"] = dearer["max_completion_tokens"]
    dearer[field] = value
    _refused(run, capsys, f"{V41} via BaseTen", "identical")


@pytest.mark.parametrize("pin", [
    pytest.param({"select": "dearest", "why": "x"}, id="unknown-select"),
    pytest.param({"select": "cheapest", "ignore": [], "why": "x"}, id="ignore-empty"),
    pytest.param({"select": "cheapest", "ignore": ["tag"], "why": "x"},
                 id="ignore-names-tag"),
    pytest.param({"select": "cheapest", "ignore": "max_completion_tokens", "why": "x"},
                 id="ignore-not-a-list"),
    pytest.param({"select": "cheapest", "ignore": ["nope"], "why": "x"},
                 id="ignore-unknown-field"),
    pytest.param({"tag": "baseten/fp8", "ignore": ["max_completion_tokens"], "why": "x"},
                 id="ignore-without-cheapest"),
    pytest.param({}, id="empty"),
    pytest.param({"why": "x"}, id="why-only"),
    pytest.param("cheapest", id="not-an-object"),
    pytest.param({"tag": 5, "why": "x"}, id="tag-not-a-string"),
])
def test_a_malformed_order_override_is_refused(tmp_path, capsys, pin):
    run = Run(tmp_path)
    run.edit(lambda doc: doc["openrouter"]["models"][V41].update(
        {"resolve": {"BaseTen": pin}}))
    _refused(run, capsys, f"{V41} via BaseTen", "a resolution is keyed on")


def test_a_tag_with_cheapest_and_a_recorded_ignore_resolves_a_sibling_tier(
        tmp_path, capsys):
    """Issue #355's live shape: the baseten/fp8 twins differ by a 1-token
    max_completion_tokens listing artifact and baseten/fast lists a sibling
    tier under its own tag. The recorded resolution narrows by tag, takes the
    cheaper twin, and the sibling's price never lands."""
    run = Run(tmp_path)
    cheaper, _ = _baseten_twins(run)
    fast = _endpoint("BaseTen", {"fresh": 0.6, "read": 0.001, "output": 2.4},
                     tag="baseten/fast")
    fast["quantization"] = "fp32"
    run.endpoints(V41).append(fast)
    cheaper["pricing"]["prompt"] = _per_token(0.28)
    rc, out, _ = run(capsys)
    assert rc == 0
    entry = run.doc()["providers"][V41]["BaseTen"][-1]
    assert entry == {"from": STAMP, "fresh": 0.28, "create_5m": 0.28,
                     "create_1h": 0.28, "read": 0.007, "output": 1.2}
    assert "possible twin switch" not in out


@pytest.mark.parametrize("field, value", [
    pytest.param("quantization", "fp4", id="quantization"),
    pytest.param("context_length", 65536, id="context-length"),
    pytest.param("max_prompt_tokens", 4096, id="max-prompt"),
])
def test_a_recorded_ignore_ignores_only_its_named_fields(
        tmp_path, capsys, field, value):
    """The live resolution ignores max_completion_tokens alone: any other
    identity field differing still refuses under it."""
    run = Run(tmp_path)
    _, dearer = _baseten_twins(run)
    dearer[field] = value
    _refused(run, capsys, f"{V41} via BaseTen", "identical")


def test_cheapest_without_a_recorded_ignore_refuses_differing_twins(
        tmp_path, capsys):
    """Without the recorded ignore the artifact itself refuses: the twins are
    not identical in tag, quantization and limits."""
    run = Run(tmp_path)
    run.edit(lambda doc: doc["openrouter"]["models"][V41].update(
        {"resolve": {"BaseTen": {"tag": "baseten/fp8", "select": "cheapest",
                                 "why": "synthetic twin check"}}}))
    _refused(run, capsys, f"{V41} via BaseTen", "identical")


@pytest.mark.parametrize("damage", [
    pytest.param(lambda p: p.update({"data": {}}), id="no-endpoints-key"),
    pytest.param(lambda p: p["data"].update({"endpoints": "none"}),
                 id="endpoints-not-a-list"),
    pytest.param(lambda p: p.clear(), id="no-data"),
    pytest.param(lambda p: p["data"]["endpoints"][0].pop("pricing"),
                 id="endpoint-without-pricing"),
    pytest.param(lambda p: p["data"]["endpoints"][0].pop("provider_name"),
                 id="endpoint-without-host"),
    pytest.param(lambda p: p["data"]["endpoints"][0].pop("tag"),
                 id="endpoint-without-tag"),
    pytest.param(lambda p: p["data"]["endpoints"][0]["pricing"].update(
        {"prompt": "cheap"}), id="non-numeric-price"),
    pytest.param(lambda p: p["data"]["endpoints"][0]["pricing"].update(
        {"prompt": "-1"}), id="negative-price"),
    pytest.param(lambda p: p["data"]["endpoints"][0]["pricing"].update(
        {"prompt": 0.0000001}), id="price-not-a-string"),
    pytest.param(lambda p: p["data"]["endpoints"][0]["pricing"].pop("completion"),
                 id="missing-output-price"),
    pytest.param(lambda p: p["data"]["endpoints"][0]["pricing"].update(
        {"discount": "half"}), id="non-numeric-discount"),
    pytest.param(lambda p: p["data"]["endpoints"][0]["pricing"].update(
        {"discount": 1.5}), id="discount-over-one"),
    pytest.param(lambda p: p["data"]["endpoints"][0]["pricing"].update(
        {"discount": -0.1}), id="negative-discount"),
    pytest.param(lambda p: p["data"]["endpoints"][0]["pricing"].update(
        {"discount": True}), id="boolean-discount"),
])
def test_an_unrecognised_response_shape_is_refused(tmp_path, capsys, damage):
    run = Run(tmp_path)
    damage(run.payloads[run.doc()["openrouter"]["models"][GLM]["id"]])
    _refused(run, capsys, GLM)


def test_no_endpoints_for_a_tracked_model_is_refused(tmp_path, capsys):
    """Indistinguishable from a broken fetch: never read as every host
    vanishing at once."""
    run = Run(tmp_path)
    run.endpoints(GLM).clear()
    _refused(run, capsys, GLM)


def test_a_detection_time_not_after_the_row_leaves_its_host_untouched(tmp_path, capsys):
    run = Run(tmp_path)
    _move_openinference(run)
    assert run(capsys)[0] == 0
    run.endpoint(GLM, "OpenInference")["pricing"]["prompt"] = "0.00000008"
    before = run.snapshot()
    rc, out, err = run(capsys, now=NOW - timedelta(days=1))
    assert rc == 0 and not err
    assert "not before the detection instant" in out
    assert run.snapshot() == before


def test_the_detection_time_is_whole_seconds_utc_both_loaders_accept():
    local = timezone(timedelta(hours=2))
    stamp = refresh.detection_stamp(
        datetime(2031, 1, 1, 1, 2, 3, 456789, tzinfo=local))
    assert stamp == "2030-12-31T23:02:03Z"
    assert pricing._INSTANT.fullmatch(stamp)  # pylint: disable=protected-access


@needs_node
def test_a_file_written_at_any_clock_reading_loads_on_both_sides(tmp_path, capsys):
    """A mid-second, non-UTC clock: the stamp written is still the one
    spelling both loaders accept, and both read it as the same instant."""
    run = Run(tmp_path)
    _move_openinference(run)
    clock = datetime(2031, 1, 1, 1, 2, 3, 456789, tzinfo=timezone(timedelta(hours=2)))
    assert run(capsys, now=clock)[0] == 0
    doc = run.doc()
    assert doc["providers"][GLM]["OpenInference"][-1]["from"] == "2030-12-31T23:02:03Z"
    epochs = pricing.load_tables(doc)["RATE_EPOCHS"]
    want = int(datetime(2030, 12, 31, 23, 2, 3, tzinfo=UTC).timestamp() * 1000)
    assert want in [int(e.timestamp() * 1000) for e in epochs]
    assert want in _browser_pricing(run, tmp_path / "js",
                                    "console.log(JSON.stringify(window.rateEpochs));")


def test_a_dry_run_reports_and_writes_nothing(tmp_path, capsys):
    run = Run(tmp_path)
    before = (run.snapshot(), run.doc()["providers"][GLM]["OpenInference"][-1]["fresh"])
    moved = _move_openinference(run)
    rc, out, _ = run(capsys, "--dry-run")
    assert rc == 0
    assert run.snapshot() == before[0]
    assert not run.commit_msg.exists()
    assert "OpenInference" in out and f"{before[1]!r} → {moved['fresh']!r}" in out
