"""Shape coverage for documented provider pins; payloads are fixture-only."""
from __future__ import annotations

import json

from tests.refresh_fixture_builders import RATE_C, RATES_A, RATES_B, _endpoint
from tests.test_provider_rate_refresh import GLM, PRICING_JSON, Run, STAMP, _payloads


def _assert_document_pin_resolves_shape(
        tmp_path, capsys, key: str, host: str, shape: str) -> None:
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

    if shape == "quantization":
        selected = pin["tag"] if pin else f"{host.lower()}/fp8"
        alternatives = [(f"{host.lower()}/fast", RATES_B)]
    elif shape == "service tiers":
        selected = pin["tag"] if pin else host.lower()
        alternatives = [(f"{selected}/fast", RATES_B),
                        (f"{selected}/flex", RATE_C)]
    else:
        selected = pin["tag"] if pin else f"{host.lower()}/global"
        alternatives = [(selected.rsplit("/", 1)[0], RATES_B)]

    endpoints.append(_endpoint(host, RATES_A, tag=selected))
    for tag, rates in alternatives:
        endpoints.append(_endpoint(host, rates, tag=tag))

    rc, _, err = run(capsys)
    assert rc == 0, f"refresh refused {key} via {host}: {err}"
    assert run.doc()["providers"][GLM][host][-1] == {
        "from": STAMP, **RATES_A,
    }
    assert run.doc()["openrouter"]["models"][GLM]["resolve"][host] == pin


def test_quantization_pin_resolves_a_throughput_tier(tmp_path, capsys):
    _assert_document_pin_resolves_shape(
        tmp_path, capsys, "glm-5-2", "Alibaba", "quantization")


def test_bare_namespace_pin_resolves_service_tiers(tmp_path, capsys):
    _assert_document_pin_resolves_shape(
        tmp_path, capsys, "gpt-5", "Azure", "service tiers")


def test_global_region_pin_selects_the_account_endpoint(tmp_path, capsys):
    _assert_document_pin_resolves_shape(
        tmp_path, capsys, "claude-haiku-4-5", "Google", "global")
