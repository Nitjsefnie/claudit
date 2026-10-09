"""Shape coverage for the documented provider resolve pins and the
automatic rules that replace the mechanical ones (#877); payloads are
fixture-only."""
from __future__ import annotations

import json
import re

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


def test_same_quantization_uses_cache_read_then_input_then_output_order():
    cheap_by_read = {**RATES_A, "fresh": 100.0, "read": 0.1, "output": 500.0}
    cheap_by_input = {**RATES_A, "fresh": 0.01, "read": 0.2, "output": 0.01}
    selected, refused, notices, _ = _select_synthetic_host(
        "Fixture", [("fixture/fp8", cheap_by_read), ("fixture/fp8", cheap_by_input)])

    assert refused == {}
    assert selected["Fixture"].rates == cheap_by_read
    assert any("rule-resolved" in note and "same quantization" in note
               for note in notices)


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
        entries = [(quant_tag, RATES_A), (quant_tag, RATES_B)]
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


def test_every_committed_resolve_pin_matches_its_rule_resolved_shape():
    """Issue #877: every committed pin is exercised without passing the pin
    to the selector, proving the listing shape resolves to its recorded row.
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
        counts[rule] = counts.get(rule, 0) + 1

    assert {"bare namespace", "unique quantization", "data region",
            "same quantization"} <= set(counts)
