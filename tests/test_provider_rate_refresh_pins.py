"""SV-RATE-REFRESH: a resolve tag pin must reach the fixture payloads.

Driven the same way as test_provider_rate_refresh.py, whose fixture
builders these share: fixture payloads over the seeded view, never the
network.
"""
from __future__ import annotations

import copy
from decimal import Decimal

from tests.test_provider_rate_refresh import GLM, V41, Run, _payloads


def _novita_region_twin(run: Run) -> dict:
    """A dearer copy of GLM's Novita endpoint tagged novita/us."""
    twin = copy.deepcopy(run.endpoint(GLM, "Novita"))
    twin["tag"] = "novita/us"
    twin["pricing"]["input_cache_read"] = "0.00000005"
    run.endpoints(GLM).append(twin)
    return twin


def _pin_novita(tag: str):
    return lambda doc: doc["openrouter"]["models"][GLM].update(
        {"resolve": {"Novita": {"tag": tag, "why": "fixture"}}})


def _baseten_twins(run: Run) -> list[dict]:
    """[cheaper, dearer] by cache read."""
    twins = [e for e in run.endpoints(V41) if e["provider_name"] == "BaseTen"]
    return sorted(twins, key=lambda e: Decimal(e["pricing"]["input_cache_read"]))


def test_a_resolve_tag_pin_names_the_synthetised_endpoint(tmp_path, capsys):
    """A resolve tag pin must reach the payload: the host's synthetic
    endpoint carries the pinned tag, as it does live, so the pinned run
    resolves instead of refusing. The pin is read from the document, never
    hard-coded."""
    run = Run(tmp_path)
    run.edit(lambda doc: doc["openrouter"]["models"][GLM].update(
        resolve={"OpenInference": {"tag": "openinference/fp8",
                                   "why": "the only tag the fixture needs"}}))
    run.payloads = _payloads(run.doc())
    tags = {e["provider_name"]: e["tag"] for e in run.endpoints(GLM)}
    assert tags["OpenInference"] == "openinference/fp8"
    rc, _, _ = run(capsys)
    assert rc == 0
