"""Shape coverage for the documented provider resolve pins and the
automatic rules that replace the mechanical ones (#877); payloads are
fixture-only."""
from __future__ import annotations

import json
import re

import pytest

from tests.refresh_fixture_builders import RATE_C, RATES_A, RATES_B, _endpoint
from tests.test_provider_rate_refresh import (
    GLM,
    NOW,
    PRICING_JSON,
    Run,
    STAMP,
    _payloads,
    _endpoint as fixture_endpoint,
    refresh,
    refresh_prices,
)


def _assert_document_shape_resolves(
        tmp_path, capsys, key: str, host: str,
        listings: tuple[tuple[str, dict], ...]) -> None:
    """The documented host resolves over `listings`: through its resolve
    pin when the document carries one, else by rule."""
    pin = json.loads(PRICING_JSON.read_text(encoding="utf-8"))[
        "openrouter"]["models"][key].get("resolve", {}).get(host)

    run = Run(tmp_path)

    def add_fixture_host(doc):
        if pin is not None:
            doc["openrouter"]["models"][GLM].setdefault("resolve", {})[host] = pin
        doc["providers"][GLM][host] = [{"from": None, **RATE_C}]

    run.edit(add_fixture_host)
    run.payloads = _payloads(run.doc())
    endpoints = run.endpoints(GLM)
    endpoints[:] = [endpoint for endpoint in endpoints
                    if endpoint["provider_name"] != host]

    for tag, rates in listings:
        endpoints.append(_endpoint(host, rates, tag=tag))

    rc, _, err = run(capsys)
    assert rc == 0, f"refresh refused {key} via {host}: {err}"
    assert run.doc()["providers"][GLM][host][-1] == {
        "from": STAMP, **RATES_A,
    }
    assert run.doc()["openrouter"]["models"][GLM].get("resolve", {}).get(host) == pin


def test_the_alibaba_shape_resolves_without_a_pin(tmp_path, capsys):
    """#893's stopgap pin is gone: the host's live shape (its fp8 endpoint
    listed twice at two prices, beside a throughput tier) resolves by rule."""
    doc = json.loads(PRICING_JSON.read_text(encoding="utf-8"))
    assert "Alibaba" not in doc["openrouter"]["models"]["glm-5-2"].get("resolve", {})
    _assert_document_shape_resolves(
        tmp_path, capsys, "glm-5-2", "Alibaba",
        (("alibaba/fp8", RATES_A), ("alibaba/fp8", RATES_B),
         ("alibaba/fast", RATE_C)))


def test_bare_namespace_pin_resolves_service_tiers(tmp_path, capsys):
    _assert_document_shape_resolves(
        tmp_path, capsys, "gpt-5", "Azure",
        (("azure", RATES_A), ("azure/fast", RATES_B), ("azure/flex", RATE_C)))


def test_global_region_pin_selects_the_account_endpoint(tmp_path, capsys):
    _assert_document_shape_resolves(
        tmp_path, capsys, "claude-haiku-4-5", "Google",
        (("google-vertex/global", RATES_A), ("google-vertex", RATES_B)))


def _select_synthetic_host(host: str, listings: list[tuple], *,
                           region: str | None = None, resolutions: dict | None = None,
                           stored: dict | None = None):
    endpoints = []
    for tag, rates, *metadata in listings:
        endpoint = fixture_endpoint(host, rates, tag=tag)
        if metadata:
            endpoint.update(metadata[0])
        endpoints.append(endpoint)
    payload = {"data": {"endpoints": endpoints}}
    return refresh.listed_rows("synthetic/model", payload, region,
                               resolutions or {}, {host: stored} if stored else {}, NOW)


def _mentions_tier(why: str, tier: str) -> bool:
    return re.search(rf"\b{tier}\b", why.lower()) is not None


def _quantization_tag(why: str) -> str | None:
    for tag in re.findall(r"\b[a-z0-9-]+/[a-z0-9-]+\b", why.lower()):
        if tag.rsplit("/", 1)[1] in refresh_prices.QUANTIZATIONS:
            return tag
    return None


def _tiers_in(why: str) -> list[str]:
    return [tier for tier in ("fast", "highspeed", "flex", "ultrafast")
            if _mentions_tier(why, tier)]


def _scaled_rates(factor: float) -> dict:
    return {field: value * factor for field, value in RATES_A.items()}


def test_bare_namespace_wins_over_multiple_tiers_even_when_fast_is_cheaper():
    faster = {**RATES_A, "read": 0.01, "fresh": 0.05, "output": 0.2}
    selected, refused, notices, _ = _select_synthetic_host(
        "Fixture", [("fixture", RATES_A), ("fixture/fast", faster),
                    ("fixture/flex", RATES_B)])

    assert refused == {}
    assert selected["Fixture"].tag == "fixture"
    assert selected["Fixture"].rates == RATES_A
    assert any("rule-resolved" in note and "bare namespace" in note
               for note in notices)


def test_quantization_endpoint_wins_over_a_cheaper_service_tier():
    cheaper_tier = {**RATES_A, "read": 0.01, "fresh": 0.05, "output": 0.2}
    selected, refused, notices, _ = _select_synthetic_host(
        "Fixture", [("fixture/fp4", RATES_A), ("fixture/fast", cheaper_tier)])

    assert refused == {}
    assert selected["Fixture"].tag == "fixture/fp4"
    assert selected["Fixture"].rates == RATES_A
    assert any("rule-resolved" in note and "quantization" in note
               for note in notices)


def test_data_region_filter_reports_its_single_surviving_endpoint():
    selected, refused, notices, _ = _select_synthetic_host(
        "Fixture", [("fixture/global", RATES_A), ("fixture/europe", RATES_B)])

    assert refused == {}
    assert selected["Fixture"].tag == "fixture/global"
    assert selected["Fixture"].rates == RATES_A
    assert any("rule-resolved" in note and "data region" in note
               for note in notices)


def test_explicit_global_tag_beats_a_bare_tag_in_the_global_region():
    selected, refused, notices, _ = _select_synthetic_host(
        "Fixture", [("fixture/global", RATES_A), ("fixture", RATES_B)])

    assert refused == {}
    assert selected["Fixture"].tag == "fixture/global"
    assert any("rule-resolved" in note and "data region" in note
               for note in notices)


def test_identical_global_and_bare_price_tags_are_reported_interchangeable():
    selected, refused, notices, _ = _select_synthetic_host(
        "Fixture", [("fixture/global", RATES_A), ("fixture", RATES_A)])

    assert refused == {}
    assert selected["Fixture"].tag == "fixture/global"
    assert any("interchangeable" in note for note in notices)


@pytest.mark.parametrize(("alternative_tag", "regional"), [
    pytest.param("fixture/fp8", False, id="quantization-alternative"),
    pytest.param("fixture/unknown", True, id="unrecognised-region-alternative"),
])
def test_global_preference_resolves_only_regional_alternatives(alternative_tag, regional):
    global_rates = {"fresh": 1.0, "create_5m": 1.0, "create_1h": 1.0,
                    "read": 0.3, "output": 2.0}
    alternative_rates = {**global_rates, "read": 0.1}
    selected, refused, notices, _ = _select_synthetic_host(
        "Fixture", [("fixture/global", global_rates),
                    (alternative_tag, alternative_rates)])

    if regional:
        assert refused == {}
        assert selected["Fixture"].tag == "fixture/global"
        assert selected["Fixture"].rates == global_rates
        assert any("rule-resolved (data region)" in note for note in notices)
    else:
        assert selected == {}
        assert set(refused) == {"Fixture"}
        assert "resolve it in openrouter.models" in refused["Fixture"]
        assert not any("rule-resolved (data region)" in note for note in notices)


def test_same_quantization_uses_cache_read_then_input_then_output_order():
    cheap_by_read = {**RATES_A, "fresh": 100.0, "read": 0.1, "output": 500.0}
    cheap_by_input = {**RATES_A, "fresh": 0.01, "read": 0.2, "output": 0.01}
    selected, refused, notices, _ = _select_synthetic_host(
        "Fixture", [("fixture/fp8", cheap_by_read), ("fixture/fp8", cheap_by_input)])

    assert refused == {}
    assert selected["Fixture"].rates == cheap_by_read
    assert any("rule-resolved" in note and "same quantization" in note
               for note in notices)


def test_same_quantization_uses_input_after_equal_cache_read():
    cheaper_input = {**RATES_A, "read": 0.1, "fresh": 0.2, "output": 3.0}
    cheaper_output = {**RATES_A, "read": 0.1, "fresh": 0.3, "output": 1.0}
    selected, refused, _, _ = _select_synthetic_host(
        "Fixture", [("fixture/fp8", cheaper_input),
                    ("fixture/fp8", cheaper_output)])

    assert refused == {}
    assert selected["Fixture"].rates == cheaper_input


def test_same_quantization_uses_output_after_equal_cache_read_and_input():
    cheaper_output = {**RATES_A, "read": 0.1, "fresh": 1.0, "output": 2.0}
    dearer_output = {**RATES_A, "read": 0.1, "fresh": 1.0, "output": 3.0}
    selected, refused, _, _ = _select_synthetic_host(
        "Fixture", [("fixture/fp8", cheaper_output),
                    ("fixture/fp8", dearer_output)])

    assert refused == {}
    assert selected["Fixture"].rates == cheaper_output


def test_rule_resolved_same_quantization_keeps_the_possible_twin_switch_notice():
    old_row = RATES_A
    current_cheapest = {**RATES_A, "read": 0.15, "fresh": 2.0, "output": 10.0}
    current_dearer = {**RATES_A, "read": 0.3, "fresh": 3.0, "output": 15.0}
    selected, refused, notices, _ = _select_synthetic_host(
        "Fixture", [("fixture/fp8", current_cheapest),
                    ("fixture/fp8", current_dearer)], stored=old_row)

    assert refused == {}
    assert selected["Fixture"].rates == current_cheapest
    assert any("rule-resolved" in note and "same quantization" in note
               for note in notices)
    assert any("possible twin switch" in note for note in notices)


def test_tag_pin_resolves_same_quantization_prices_within_the_pinned_tag():
    """A quantization tag pin narrows away the fast tier before twin ordering.

    The maintained catalog no longer needs the Alibaba stopgap pin, so the
    pin is synthetic while the price, tag and twin-switch assertions remain.
    """
    pin = {"tag": "alibaba/fp8", "why": "synthetic quantization pin"}
    assert pin["tag"] == "alibaba/fp8"
    assert set(pin) == {"tag", "why"}

    cheap = {**RATES_A, "fresh": 2.0, "read": 0.15, "output": 10.0}
    dear = {**RATES_A, "fresh": 3.0, "read": 0.3, "output": 15.0}
    endpoints = []
    for tag, rates in (("alibaba/fp8", cheap),
                       ("alibaba/fp8", dear),
                       ("alibaba/fast", RATES_B)):
        endpoint = fixture_endpoint("Alibaba", rates, tag=tag)
        endpoint.update({"quantization": "fp8", "context_length": 1_000_000,
                         "max_completion_tokens": 131_072,
                         "max_prompt_tokens": 1_048_576})
        endpoints.append(endpoint)

    selected, refused, notices, _ = refresh.listed_rows(
        "glm-5-2", {"data": {"endpoints": endpoints}}, None,
        {"Alibaba": pin}, {"Alibaba": RATES_A}, NOW)

    assert refused == {}
    assert selected["Alibaba"].tag == "alibaba/fp8"
    assert selected["Alibaba"].rates == cheap
    assert any(
        "rule-resolved (same quantization, within the pinned tag "
        "'alibaba/fp8')" in note and "took the cheapest fp8 endpoint" in note
        for note in notices)
    assert any("possible twin switch" in note for note in notices)


def test_single_endpoint_quantization_tag_pin_keeps_precedence():
    fp4 = {**RATES_A, "read": 0.2}
    fast = {**RATES_A, "read": 0.1}
    endpoints = []
    for tag, rates, quantization in (("alibaba/fp4", fp4, "fp4"),
                                     ("alibaba/fast", fast, "fp8")):
        endpoint = fixture_endpoint("Alibaba", rates, tag=tag)
        endpoint["quantization"] = quantization
        endpoints.append(endpoint)

    selected, refused, notices, _ = refresh.listed_rows(
        "glm-5-2", {"data": {"endpoints": endpoints}}, None,
        {"Alibaba": {"tag": "alibaba/fp4", "why": "fixture"}}, {}, NOW)

    assert refused == {}
    assert selected["Alibaba"].tag == "alibaba/fp4"
    assert selected["Alibaba"].rates == fp4
    assert not any("rule-resolved" in note for note in notices)


def test_ambiguous_non_quantization_tag_pin_still_refuses():
    endpoints = [fixture_endpoint("Alibaba", rates, tag="alibaba")
                 for rates in (RATES_A, RATES_B)]

    selected, refused, _, _ = refresh.listed_rows(
        "glm-5-2", {"data": {"endpoints": endpoints}}, None,
        {"Alibaba": {"tag": "alibaba", "why": "fixture"}}, {}, NOW)

    assert selected == {}
    assert set(refused) == {"Alibaba"}
    assert "resolve it in openrouter.models.<model>.resolve" in refused["Alibaba"]


def test_an_explicit_pin_keeps_precedence_over_bare_and_region_rules():
    selected, refused, notices, _ = _select_synthetic_host(
        "Fixture", [("fixture", RATES_A), ("fixture/fast", RATES_B)],
        resolutions={"Fixture": {"tag": "fixture/fast", "why": "fixture"}})

    assert refused == {}
    assert selected["Fixture"].tag == "fixture/fast"
    assert not any("rule-resolved" in note for note in notices)

    selected, refused, notices, _ = _select_synthetic_host(
        "Fixture", [("fixture/global", RATES_A), ("fixture/europe", RATES_B)],
        resolutions={"Fixture": {"tag": "fixture/europe", "why": "fixture"}})
    assert refused == {}
    assert selected["Fixture"].tag == "fixture/europe"
    assert not any("rule-resolved" in note for note in notices)


def _synthetic_pin_shape(pin: dict, host: str) -> tuple[list[tuple], str, str, bool]:
    """Build a minimal listing from the committed resolution's stated shape.

    Return listings, expected rule name, the pin's tag when it has one, and
    whether correctness is price-vector parity because equal-price tags are
    interchangeable.
    """
    why = pin["why"]
    target = pin.get("tag")
    if target and target.endswith("/global"):
        sibling = target.rsplit("/", 1)[0] + "/europe"
        return [(target, RATES_A), (sibling, RATES_B)], "data region", target, False

    if target and target.endswith("/zdr"):
        quant_tag = _quantization_tag(why)
        assert quant_tag, f"no quantization tag in {why!r}"
        return [(target, RATES_A), (quant_tag, RATES_A),
                (quant_tag, RATES_B)], "same quantization", target, True

    if pin.get("select") == "cheapest":
        quant_tag = target or _quantization_tag(why)
        assert quant_tag, f"no quantization endpoint in {why!r}"
        entries: list[
            tuple[str, dict[str, float]]
            | tuple[str, dict[str, float], dict[str, int]]
        ] = [(quant_tag, RATES_A), (quant_tag, RATES_B)]
        if "max_completion_tokens" in why:
            entries = [(quant_tag, RATES_A, {"max_completion_tokens": 32767}),
                       (quant_tag, RATES_B, {"max_completion_tokens": 32768})]
        if _mentions_tier(why, "fast"):
            endpoint = fixture_endpoint(host, RATE_C, tag=host.lower() + "/fast")
            entries.append((endpoint["tag"], RATE_C))
        return entries, "same quantization", target or quant_tag, False

    if target and "/" not in target:
        tiers = _tiers_in(why) or ["fast", "flex"]
        entries = [(target, RATES_A)]
        entries.extend((f"{target}/{tier}", _scaled_rates(index + 2))
                       for index, tier in enumerate(tiers))
        return entries, "bare namespace", target, False

    if target and target.rsplit("/", 1)[1] in refresh_prices.QUANTIZATIONS:
        tiers = _tiers_in(why) or ["fast"]
        entries = [(target, RATES_A)]
        entries.extend((f"{target.split('/', 1)[0]}/{tier}", _scaled_rates(index + 2))
                       for index, tier in enumerate(tiers))
        return entries, "unique quantization", target, False

    raise AssertionError(f"no synthetic shape builder for {host}: {pin!r}")


def _assert_resolve_pin_shape(model: str, model_id: str, host: str,
                              pin: dict, region: str | None) -> str:
    """Prove one committed pin's synthetic listing keeps its resolved shape."""
    listings, rule, pinned_tag, interchangeable = _synthetic_pin_shape(pin, host)
    selected, refused, notices, _ = _select_synthetic_host(
        host, listings, region=region)
    assert refused == {}, f"{model} via {host}: {refused}"
    assert host in selected, f"{model} via {host}: no endpoint was selected"
    chosen = selected[host]
    assert chosen.rates == RATES_A, f"{model_id} via {host}: wrong price vector"
    if not interchangeable:
        assert chosen.tag == pinned_tag, (
            f"{model_id} via {host}: expected {pinned_tag}, got {chosen.tag}")
    assert any("rule-resolved" in note and rule in note for note in notices), (
        f"{model_id} via {host}: expected rule {rule!r} in {notices!r}")
    if interchangeable:
        assert any("interchangeable" in note for note in notices), (
            f"{model_id} via {host}: equal-price tags were not reported as interchangeable")
    return rule


def test_every_committed_resolve_pin_matches_its_rule_resolved_shape():
    """Issue #877: each committed pin's listing shape resolves without
    passing the pin to the selector, and selects its recorded price vector.
    """
    doc = json.loads(PRICING_JSON.read_text(encoding="utf-8"))
    region = None if doc["openrouter"]["data_region"] == "global" \
        else doc["openrouter"]["data_region"]
    counts: dict[str, int] = {}
    pins = [(model, entry["id"], host, pin)
            for model, entry in doc["openrouter"]["models"].items()
            for host, pin in entry.get("resolve", {}).items()]
    assert pins, "the pricing document has no resolve entries to prove"

    for model, model_id, host, pin in pins:
        rule = _assert_resolve_pin_shape(model, model_id, host, pin, region)
        counts[rule] = counts.get(rule, 0) + 1

    assert {"bare namespace", "unique quantization", "data region",
            "same quantization"} <= set(counts)
