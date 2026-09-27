"""SV-RATE-REFRESH: a resolve tag pin must reach the fixture payloads.

Driven the same way as test_provider_rate_refresh.py, whose fixture
builders these share: fixture payloads over the seeded view, never the
network.
"""
from __future__ import annotations

from tests.test_provider_rate_refresh import GLM, Run, _payloads


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
