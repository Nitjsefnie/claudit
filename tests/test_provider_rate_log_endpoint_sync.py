"""Log-backed rows follow their selected endpoint even without a new log point."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend import pricing
from backend.pricing_document import expand_pricing_doc
from tests.refresh_fixture_builders import _endpoint, seed_doc
from tests.test_provider_rate_log_refresh import (
    HOST, MODEL, MODEL_ID, RATE_A, RATE_B, RefreshFetches,
    _entry, _series, refresh,
)

STAMP = "2031-01-08T00:00:00Z"
NOW = datetime(2031, 1, 8, tzinfo=timezone.utc)
ROW_AT = "2031-01-05T00:00:00Z"
LOG_AT = "2031-01-03T00:00:00Z"
LISTED = {"fresh": 3.0, "create_5m": 3.0, "create_1h": 3.0,
          "read": 0.4, "output": 8.0}


class SyncRun:
    """A synthetic selected endpoint and its stale, independently dated row."""
    def __init__(self, tmp_path: Path, shape: str, *,
                 row_rates: dict | None = None, listed: dict | None = None,
                 row_at: str = ROW_AT) -> None:
        self.listed = dict(LISTED if listed is None else listed)
        self.history = [_entry(row_at, RATE_A if row_rates is None else row_rates)]
        source: dict = {"id": MODEL_ID}
        endpoints = [_endpoint(HOST, self.listed, tag="wafer/fp8")]
        series = [_series([(LOG_AT, self.listed)])]
        if shape == "repinned":
            source["resolve"] = {HOST: {"tag": "wafer/fp8", "why": "selected tier"}}
            endpoints.append(_endpoint(HOST, RATE_A, tag="wafer/mxfp4"))
            series.append({**_series([("2031-01-02T00:00:00Z", RATE_A)]),
                           "endpointId": "synthetic-old-endpoint"})
        self.doc = seed_doc(tracked={MODEL: source},
                            providers={MODEL: {HOST: self.history}}, fetched=row_at)
        self.fetches = RefreshFetches(
            {"data": {"endpoints": endpoints}},
            {"data": {"series": series}}, None, None)
        self.path = tmp_path / "pricing.json"
        self.constants = tmp_path / "constants.py"
        self.message = tmp_path / "commit.txt"
        self.path.write_text(json.dumps(self.doc) + "\n", encoding="utf-8")
        self.constants.write_text('PRICING_VERSION = "13"\n', encoding="utf-8")
        self.before = self.path.read_bytes(), self.constants.read_bytes()

    def run(self, capsys, *, sampled: bool = False):
        if sampled:
            self.fetches.log_fetch = lambda: {"unexpected": "synthetic unavailable log"}
        rc = refresh.main(
            ["--commit-msg", str(self.message)], fetch=self.fetches.fetch_endpoints,
            fetch_models=self.fetches.fetch_models, fetch_log=self.fetches.fetch_log,
            now=NOW, pricing_path=self.path, constants_path=self.constants, vendor=None)
        out, err = capsys.readouterr()
        return rc, out, err

    def saved(self) -> dict:
        return expand_pricing_doc(json.loads(self.path.read_text(encoding="utf-8")))

    def assert_untouched(self) -> None:
        assert (self.path.read_bytes(), self.constants.read_bytes()) == self.before
        assert not self.message.exists()
        assert not self.path.with_name("pricing_notices.json").exists()


@pytest.mark.parametrize("shape", ["repinned", "resolution_removed"])
def test_stale_selected_endpoint_appends_at_detection(tmp_path, capsys, monkeypatch,
                                                      shape):
    run = SyncRun(tmp_path, shape)

    rc, out, err = run.run(capsys)

    assert rc == 0 and not err
    saved = run.saved()
    assert saved["providers"][MODEL][HOST] == [
        {"from": ROW_AT, "context_length": 131072, **RATE_A}, {"from": STAMP, "context_length": 131072, **LISTED}]
    assert saved["provider_rates_fetched"] == STAMP
    assert 'PRICING_VERSION = "14"' in run.constants.read_text(encoding="utf-8")
    assert f"changed   {HOST}" in out and f"sampled   {HOST}" not in out
    assert "out of step with its endpoint's listed price" in out
    assert "1 changed" in run.message.read_text(encoding="utf-8")
    for name, table in pricing.load_tables(saved).items():
        monkeypatch.setattr(pricing, name, table)
    after_detection = datetime(2031, 1, 8, 0, 0, 1, tzinfo=timezone.utc)
    resolved = pricing.resolve(MODEL, after_detection, HOST)
    assert resolved.rates == LISTED


@pytest.mark.parametrize("shape", ["repinned", "resolution_removed"])
def test_sampling_also_appends_stale_selected_endpoint(tmp_path, capsys, shape):
    run = SyncRun(tmp_path, shape)

    rc, out, err = run.run(capsys, sampled=True)

    assert rc == 0 and not err
    assert run.saved()["providers"][MODEL][HOST][-1] == {"from": STAMP, "context_length": 131072, **LISTED}
    assert f"sampled   {HOST}" in out and f"changed   {HOST}" in out


@pytest.mark.parametrize(("shape", "row_at"), [
    ("repinned", ROW_AT),
    ("resolution_removed", STAMP),
    ("resolution_removed", "2031-01-09T00:00:00Z"),
], ids=["repinned-older-row", "unselected-current-row", "unselected-future-row"])
def test_selected_endpoint_already_in_step_writes_nothing(tmp_path, capsys, shape, row_at):
    run = SyncRun(tmp_path, shape, row_at=row_at,
                  listed=RATE_B, row_rates=RATE_B)

    rc, out, err = run.run(capsys)

    assert rc == 0 and not err
    run.assert_untouched()
    assert "no rate moved" in out
    assert HOST not in out


@pytest.mark.parametrize("row_at", [STAMP, "2031-01-09T00:00:00Z"])
def test_out_of_step_endpoint_refuses_when_detection_cannot_append(
        tmp_path, capsys, row_at):
    run = SyncRun(tmp_path, "repinned", row_at=row_at)

    rc, out, err = run.run(capsys)

    assert rc == 1, out
    assert f"{MODEL} via {HOST}" in out and HOST in err
    assert "out of step with its endpoint's listed price" in out
    assert "not before the detection instant" in out
    run.assert_untouched()


@pytest.mark.parametrize("old_search", [0.0, 0.02])
def test_detected_token_state_shares_separately_sampled_search_change(
        tmp_path, capsys, old_search):
    run = SyncRun(tmp_path, "repinned", row_rates={**RATE_A, "web_search": old_search},
                  listed={**LISTED, "web_search": 0.03})

    rc, out, err = run.run(capsys)

    assert rc == 0 and not err
    history = run.saved()["providers"][MODEL][HOST]
    assert len(history) == 2
    assert history[0].get("web_search", 0.0) == old_search
    assert history[-1] == {"from": STAMP, "context_length": 131072, **LISTED, "web_search": 0.03}
    assert "web_search" in out and "sampled at detection" in out
    assert "out of step with its endpoint's listed price" in out


def test_blocked_endpoint_refusal_still_publishes_other_hosts_move(tmp_path, capsys):
    run = SyncRun(tmp_path, "repinned", row_at=STAMP)
    other = "OtherHost"
    run.doc["providers"][MODEL][other] = [_entry(ROW_AT, RATE_A)]
    run.path.write_text(json.dumps(run.doc) + "\n", encoding="utf-8")
    run.fetches.payload["data"]["endpoints"].append(
        _endpoint(other, LISTED, tag="other/fp8"))
    run.fetches.log_payload["data"]["series"].append({
        **_series([(LOG_AT, LISTED)]), "endpointId": "synthetic-other-endpoint",
        "providerName": other, "providerSlug": "other"})

    rc, out, err = run.run(capsys)

    assert rc == 1 and HOST in err
    saved = run.saved()["providers"][MODEL]
    assert saved[HOST] == [{"from": STAMP, "context_length": 131072, **RATE_A}]
    assert saved[other] == [{"from": ROW_AT, "context_length": 131072, **RATE_A}, {"from": STAMP, "context_length": 131072, **LISTED}]
    assert f"changed   {other}" in out
    assert "1 changed, 1 refused" in run.message.read_text(encoding="utf-8")
