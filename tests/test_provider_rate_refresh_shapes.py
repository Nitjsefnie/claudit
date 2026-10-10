"""Shape coverage for the documented provider resolve pins and the
automatic rules that replace the mechanical ones (#877); payloads are
fixture-only."""
from __future__ import annotations

import json

from tests.refresh_fixture_builders import RATE_C, RATES_A, RATES_B, _endpoint
from tests.test_provider_rate_refresh import GLM, PRICING_JSON, Run, STAMP, _payloads


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
