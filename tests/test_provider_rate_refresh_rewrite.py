"""The refresh corrects logged history while retaining sampled observations."""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone

import pytest

from backend import pricing
from backend.pricing_document import expand_pricing_doc, serialize_pricing_doc
from tests.refresh_fixture_builders import _endpoint, seed_doc
from tests.test_provider_rate_log_refresh import (
    HOST, MODEL, MODEL_ID, NOW, RATE_A, RATE_B, STAMP, RefreshFetches,
    _entry, _series, refresh,
)

EARLY = "2031-01-01T00:05:00Z"
CHANGED = "2031-01-01T00:10:00Z"
DRIFTED = "2031-01-01T00:20:00Z"
START = "2030-12-31T23:00:00Z"


class RewriteRun:
    def __init__(self, tmp_path, history, states, *, listed=None, fetched=DRIFTED):
        self.path = tmp_path / "pricing.json"
        self.constants = tmp_path / "constants.py"
        self.message = tmp_path / "commit.txt"
        self.state = tmp_path / "pricing_notices.json"
        self.doc = seed_doc(
            tracked={MODEL: {"id": MODEL_ID}},
            providers={MODEL: {HOST: history}}, fetched=fetched)
        self.path.write_text(serialize_pricing_doc(self.doc), encoding="utf-8")
        self.constants.write_text('PRICING_VERSION = "17"\n', encoding="utf-8")
        self.fetches = RefreshFetches(
            {"data": {"endpoints": [_endpoint(
                HOST, listed or states[-1][1], tag="wafer/fp8")]}},
            {"data": {"series": [_series(states)]}}, None, None)

    def run(self, capsys):
        rc = refresh.main(
            ["--commit-msg", str(self.message)],
            fetch=self.fetches.fetch_endpoints,
            fetch_models=self.fetches.fetch_models, fetch_log=self.fetches.fetch_log,
            now=NOW, pricing_path=self.path, constants_path=self.constants, vendor=None)
        out, err = capsys.readouterr()
        assert rc == 0, err
        assert not err
        return out

    def saved(self):
        return expand_pricing_doc(json.loads(self.path.read_text(encoding="utf-8")))


@pytest.mark.parametrize("fetched,expected", [
    (DRIFTED, STAMP), ("2031-01-02T00:00:00Z", "2031-01-02T00:00:00Z"),
])
def test_rewrite_only_run_corrects_closed_window_bumps_and_keeps_later_stamp(
        tmp_path, capsys, fetched, expected):
    run = RewriteRun(tmp_path, [_entry(None, RATE_A),
                                _entry(DRIFTED, RATE_B, note="stale annotation")],
                     [(START, RATE_A), (CHANGED, RATE_B)], fetched=fetched)

    out = run.run(capsys)

    saved = run.saved()
    assert saved["providers"][MODEL][HOST] == [
        {"from": None, **RATE_A, "context_length": 131072},
        {"from": CHANGED, **RATE_B, "context_length": 131072}]
    assert saved["provider_rates_fetched"] == expected
    assert run.constants.read_text(encoding="utf-8") == 'PRICING_VERSION = "18"\n'
    assert "log rewrite" in out
    assert "log rewrite" in run.message.read_text(encoding="utf-8")
    pricing.load_tables(json.loads(run.path.read_text(encoding="utf-8")))
    baseline = run.state.read_bytes()
    written = run.path.read_bytes(), run.constants.read_bytes()

    assert "no rate moved" in run.run(capsys)
    assert (run.path.read_bytes(), run.constants.read_bytes()) == written
    assert run.state.read_bytes() == baseline


def test_rewrite_preserves_sampled_search_and_context_epochs(tmp_path, capsys):
    history = [
        _entry(None, RATE_A, web_search=0.01),
        _entry(EARLY, RATE_A, web_search=0.02, context_length=262144),
        _entry(DRIFTED, RATE_B, web_search=0.02, context_length=262144)]
    run = RewriteRun(tmp_path, history, [(START, RATE_A), (CHANGED, RATE_B)],
                     listed={**RATE_B, "web_search": 0.02, "context_length": 262144})

    run.run(capsys)

    assert run.saved()["providers"][MODEL][HOST] == [
        {**history[0], **RATE_A}, {**history[1], **RATE_A},
        {**history[2], **RATE_B, "from": CHANGED}]


@pytest.mark.parametrize("drift", ["none", "note", "grouping", "context"])
def test_price_equivalence_keeps_stored_row_and_notice_baseline_untouched(
        tmp_path, capsys, drift):
    history = [_entry(None, RATE_A), _entry(CHANGED, RATE_B)]
    states = [(START, RATE_A), (CHANGED, RATE_B)]
    if drift in {"note", "context"}:
        states = [(START, RATE_A), (EARLY, RATE_A), (CHANGED, RATE_B)]
        history.insert(1, _entry(EARLY, RATE_A, note="annotation"))
        if drift == "context":
            history[1]["context_length"] = 262144
    if drift == "grouping":
        history.append(_entry(DRIFTED, RATE_A))
        states.append((DRIFTED, RATE_A))
        history.append(_entry("2031-01-01T00:25:00Z", RATE_B))
        states.append(("2031-01-01T00:25:00Z", RATE_B))
        fetched = "2031-01-01T00:25:00Z"
    else:
        fetched = DRIFTED
    run = RewriteRun(tmp_path, history, states, fetched=fetched)
    run.state.write_text('{"notices": ["existing notice"]}\n', encoding="utf-8")
    before = run.path.read_bytes(), run.constants.read_bytes(), run.state.read_bytes()

    out = run.run(capsys)

    assert "no rate moved" in out
    assert (run.path.read_bytes(), run.constants.read_bytes(), run.state.read_bytes()) == before
    assert not run.message.exists()
    if drift != "none":
        assert "unchanged" in out and HOST in out


def test_stored_schedule_disagreement_samples_only_that_host(tmp_path, capsys):
    schedule = [{"days": ["monday"], "rates": RATE_B}]
    history = [_entry(None, RATE_A, schedule=schedule)]
    run = RewriteRun(tmp_path, history, [(START, RATE_A), (CHANGED, RATE_B)])
    before = copy.deepcopy(run.doc["providers"][MODEL][HOST])

    out = run.run(capsys)

    assert "stored schedule differs from log" in out
    assert f"sampled   {HOST}" in out
    assert run.saved()["providers"][MODEL][HOST] == [
        {**before[0], **RATE_A}, {"from": STAMP, **RATE_B, "context_length": 131072}]
    assert "log rewrite" not in out


@pytest.mark.parametrize("context_length", [131072, 262144])
def test_unchanged_detection_time_search_epoch_emits_no_move_or_writes(
        tmp_path, capsys, context_length):
    rates = {**RATE_A, "web_search": 0.02, "context_length": context_length}
    run = RewriteRun(tmp_path, [_entry(STAMP, rates, context_length=context_length)],
                     [(STAMP, RATE_A)],
                     listed=rates, fetched=STAMP)
    run.state.write_text('{"notices": ["existing notice"]}\n', encoding="utf-8")
    before = run.path.read_bytes(), run.constants.read_bytes(), run.state.read_bytes()

    result = refresh.refresh(
        json.loads(run.path.read_text(encoding="utf-8")),
        run.fetches.fetch_endpoints, STAMP,
        run.fetches.fetch_models, run.fetches.fetch_log)

    assert serialize_pricing_doc(result.doc).encode() == before[0]
    assert not result.moves
    for _ in range(2):
        assert "no rate moved" in run.run(capsys)
        assert (run.path.read_bytes(), run.constants.read_bytes(), run.state.read_bytes()) == before
        assert not run.message.exists()


@pytest.mark.parametrize("old_context,new_context", [(131072, 262144), (262144, 131072)])
def test_detection_time_search_epoch_keeps_a_genuine_context_change(
        tmp_path, capsys, old_context, new_context):
    rates = {**RATE_A, "web_search": 0.02}
    run = RewriteRun(tmp_path, [_entry(STAMP, rates, context_length=old_context)],
                     [(STAMP, RATE_A)], listed={**rates, "context_length": new_context},
                     fetched=STAMP)

    result = refresh.refresh(
        json.loads(run.path.read_text(encoding="utf-8")),
        run.fetches.fetch_endpoints, STAMP,
        run.fetches.fetch_models, run.fetches.fetch_log)

    assert len(result.moves) == 1
    assert result.doc["providers"][MODEL][HOST] == [
        {"from": STAMP, **rates, "context_length": new_context}]
    assert "changed" in run.run(capsys)
    assert run.saved()["providers"][MODEL][HOST] == [
        {"from": STAMP, **rates, "context_length": new_context}]
    assert run.constants.read_text(encoding="utf-8") == 'PRICING_VERSION = "18"\n'
    written = run.path.read_bytes(), run.constants.read_bytes(), run.state.read_bytes()
    assert "no rate moved" in run.run(capsys)
    assert (run.path.read_bytes(), run.constants.read_bytes(), run.state.read_bytes()) == written


@pytest.mark.parametrize("grouped", [False, True])
def test_endpoint_continuity_uses_the_newest_occurrence_for_log_join(
        tmp_path, capsys, grouped):
    history = [_entry(START, RATE_A), _entry(CHANGED, RATE_B), _entry(DRIFTED, RATE_A)]
    if grouped:
        history = [{**history[0], "from": [START, DRIFTED]}, history[1]]
    run = RewriteRun(tmp_path, history,
                     [(START, RATE_A), (EARLY, RATE_B), (DRIFTED, RATE_A)])
    other = _endpoint(HOST, RATE_B, tag="wafer/mxfp4")
    other["quantization"] = "mxfp4"
    run.fetches.payload["data"]["endpoints"].append(other)
    run.fetches.log_payload["data"]["series"].append({
        **_series([(START, RATE_B)]), "endpointId": "synthetic-second-endpoint"})

    out = run.run(capsys)

    assert "log rewrite" in out and "sampled   Wafer" not in out
    assert run.saved()["providers"][MODEL][HOST] == [
        {"from": [START, DRIFTED], **RATE_A, "context_length": 131072},
        {"from": EARLY, **RATE_B, "context_length": 131072}]
    assert run.constants.read_text(encoding="utf-8") == 'PRICING_VERSION = "18"\n'


def test_closed_schedule_pricing_drift_rewrites_and_bumps(tmp_path, capsys, monkeypatch):
    start = "2030-12-29T00:00:00Z"
    history = [_entry(start, RATE_A, schedule=[{"days": ["monday"], "rates": RATE_B}]),
               _entry(CHANGED, RATE_A)]
    run = RewriteRun(tmp_path, history, [(start, RATE_A)])
    monday = datetime(2030, 12, 30, tzinfo=timezone.utc)
    for name, table in pricing.load_tables(run.doc).items():
        monkeypatch.setattr(pricing, name, table)
    assert pricing.resolve(MODEL, monday, HOST).rates == RATE_B

    out = run.run(capsys)

    assert "log rewrite" in out
    assert run.saved()["providers"][MODEL][HOST] == [
        {"from": "2030-12-29T00:00:00Z", **RATE_A, "context_length": 131072}]
    for name, table in pricing.load_tables(run.saved()).items():
        monkeypatch.setattr(pricing, name, table)
    assert pricing.resolve(MODEL, monday, HOST).rates == RATE_A
    assert run.constants.read_text(encoding="utf-8") == 'PRICING_VERSION = "18"\n'


@pytest.mark.parametrize("schedule", [
    [{"days": ["monday"], "rates": RATE_A}],
    [{"rates": RATE_A}, {"days": ["monday"], "rates": RATE_B}],
])
def test_effectively_unchanged_closed_schedule_keeps_the_row(tmp_path, capsys, schedule):
    run = RewriteRun(tmp_path, [_entry(START, RATE_A, schedule=schedule),
                                _entry(CHANGED, RATE_A)], [(START, RATE_A)])
    before = run.path.read_bytes(), run.constants.read_bytes()

    assert "no rate moved" in run.run(capsys)
    assert (run.path.read_bytes(), run.constants.read_bytes()) == before
