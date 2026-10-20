"""retained future provider entries never block another host's refresh."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from backend import pricing
from backend.pricing_document import expand_pricing_doc
from tests.refresh_fixture_builders import _endpoint, seed_doc
from tests.test_provider_rate_refresh import NOW, STAMP, refresh


@pytest.mark.parametrize("stored_stamp", [
    pytest.param(STAMP, id="equal"),
    pytest.param("2031-01-02T00:00:00Z", id="future"),
    pytest.param("2030-12-31T23:00:00-02:00", id="future-offset"),
])
@pytest.mark.parametrize("logged", [True, False], ids=["logged", "sampled"])
def test_a_postdated_host_does_not_block_other_hosts_moves(
        tmp_path, capsys, stored_stamp, logged):
    """keeping Wafer's later entry must not invalidate OtherHost's move."""
    old = {"fresh": 1.0, "create_5m": 1.0, "create_1h": 1.0,
           "read": 0.1, "output": 3.0}
    wafer_listed = {"fresh": 2.0, "create_5m": 2.0, "create_1h": 2.0,
                    "read": 0.2, "output": 4.0}
    other_listed = {"fresh": 4.0, "create_5m": 4.0, "create_1h": 4.0,
                    "read": 0.4, "output": 6.0}
    doc = seed_doc(
        tracked={"example/model": {"id": "example/model"}},
        providers={"example/model": {
            "Wafer": [{"from": stored_stamp, **old}],
            "OtherHost": [{"from": None, **old}]}},
        fetched=stored_stamp)
    pricing_path = tmp_path / "pricing.json"
    constants_path = tmp_path / "constants.py"
    pricing_path.write_text(json.dumps(doc), encoding="utf-8")
    constants_path.write_text('PRICING_VERSION = "73"\n', encoding="utf-8")
    series = [{
        "endpointId": "wafer-endpoint", "providerName": "Wafer",
        "providerSlug": "wafer", "schedule": [],
        "input": [{"at": "2030-01-01T00:00:00Z", "value": 2.0}],
        "output": [{"at": "2030-01-01T00:00:00Z", "value": 4.0}],
        "cacheRead": [{"at": "2030-01-01T00:00:00Z", "value": 0.2}],
        "cacheWrite": [], "discount": [],
    }] if logged else []
    rc = refresh.main(
        ["--commit-msg", str(tmp_path / "commit-msg.txt")],
        fetch=lambda _model_id: {"data": {"endpoints": [
            _endpoint("Wafer", wafer_listed),
            _endpoint("OtherHost", other_listed)]}},
        fetch_models=lambda: {"data": [{
            "id": "example/model", "canonical_slug": "example/model"}]},
        fetch_log=lambda _slug: {"data": {"series": series}},
        now=NOW, pricing_path=pricing_path, constants_path=constants_path,
        vendor=None)
    out, err = capsys.readouterr()
    after = expand_pricing_doc(json.loads(pricing_path.read_text(encoding="utf-8")))
    assert after != doc, err
    assert after["provider_rates_fetched"] == stored_stamp
    assert after["providers"]["example/model"] == {
        "Wafer": ([{"from": "2030-01-01T00:00:00Z", **wafer_listed},
                   {"from": STAMP, "context_length": 131072, **wafer_listed}]
                  if logged else [{**old, "from": datetime.fromisoformat(stored_stamp)
                                   .astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}]),
        "OtherHost": [{"from": None, **old}, {"from": STAMP, "context_length": 131072, **other_listed}]}
    assert constants_path.read_text(encoding="utf-8") == 'PRICING_VERSION = "74"\n'
    assert "OtherHost" in (tmp_path / "commit-msg.txt").read_text(encoding="utf-8")
    assert rc == 0 and not err
    if logged:
        assert "log rewrite" in out
    else:
        assert "left untouched" in out
    pricing.load_tables(after)
