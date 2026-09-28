"""Hourly log-backed refresh tests over synthetic OpenRouter responses."""
from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from backend import pricing
from tests.refresh_fixture_builders import _endpoint as fixture_endpoint

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / "scripts" / "ci"
sys.path.insert(0, str(CI))
import refresh_provider_rates as refresh  # noqa: E402
import refresh_pricelog as pricelog  # noqa: E402

MODEL = "synthetic/model"
MODEL_ID = "synthetic/model-id"
SLUG = "synthetic/canonical-model"
HOST = "Wafer"
STAMP = "2031-01-01T00:30:00Z"
NOW = datetime(2031, 1, 1, 0, 30, tzinfo=timezone.utc)
RATE_A = {"fresh": 0.3, "create_5m": 0.3, "create_1h": 0.3,
          "read": 0.01, "output": 0.8}
RATE_B = {"fresh": 0.2, "create_5m": 0.2, "create_1h": 0.2,
          "read": 0.02, "output": 0.7}
RATE_C = {"fresh": 0.4, "create_5m": 0.4, "create_1h": 0.4,
          "read": 0.03, "output": 0.9}

def _point(at: str, value: float) -> dict:
    return {"at": at, "value": value}


def _series(states: list[tuple[str, dict]]) -> dict:
    fields = {"input": [], "output": [], "cacheRead": [], "cacheWrite": [], "discount": []}
    for at, rates in states:
        fields["input"].append(_point(at, rates["fresh"]))
        fields["output"].append(_point(at, rates["output"]))
        fields["cacheRead"].append(_point(at, rates["read"]))
        write = rates["create_5m"] if rates["create_5m"] != rates["fresh"] else 0
        fields["cacheWrite"].append(_point(at, write))
        fields["discount"].append(_point(at, 0))
    return {"endpointId": "synthetic-endpoint-id", "providerName": HOST,
            "providerSlug": "wafer", **fields}


def _doc(hosts: dict[str, list[dict]], resolve: dict | None = None) -> dict:
    return {
        "models": {MODEL: [{"from": None, **RATE_A}]},
        "providers": {MODEL: copy.deepcopy(hosts)},
        "provider_rates_fetched": "2026-01-01T00:00:00Z",
        "openrouter": {
            "data_region": "global",
            "models": {MODEL: {"id": MODEL_ID, "resolve": resolve or {}}},
        },
    }


def _entry(at: str, rates: dict) -> dict:
    return {"from": at, **rates}


def _endpoint(host: str, rates: dict) -> dict:
    return fixture_endpoint(host, rates, tag="wafer/fp8")


def _run(tmp_path: Path, capsys, *, history: list[tuple[str, dict]],
         hosts: dict[str, list[dict]] | None = None,
         endpoint_rates: dict | None = None, now: datetime = NOW,
         log_fetch=None, catalog=None, version: int = 13):
    log_payload = {"data": {"series": [_series(history)]}}
    row_hosts = {HOST: [_entry(None, RATE_A)]} if hosts is None else hosts
    doc = _doc(row_hosts)
    pricing_path = tmp_path / "pricing.json"
    constants_path = tmp_path / "constants.py"
    pricing_path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    constants_path.write_text(f'PRICING_VERSION = "{version}"\n', encoding="utf-8")
    endpoint_rates = endpoint_rates or history[-1][1]
    endpoint_hosts = dict(row_hosts)
    endpoint_hosts.setdefault(HOST, [])
    endpoints = []
    for host, row in endpoint_hosts.items():
        rates = endpoint_rates if host == HOST else row[-1]
        endpoints.append(_endpoint(host, rates))
    payload = {"data": {"endpoints": endpoints}}

    def fetch_endpoints(model_id: str) -> object:
        assert model_id == MODEL_ID
        return copy.deepcopy(payload)

    def fetch_models() -> object:
        return catalog if catalog is not None else {
            "data": [{"id": MODEL_ID, "canonical_slug": SLUG}]}

    def fetch_listed(slug: str) -> object:
        assert slug == SLUG
        if log_fetch:
            return log_fetch()
        return copy.deepcopy(log_payload)

    rc = refresh.main([], fetch=fetch_endpoints, fetch_models=fetch_models,
                      fetch_log=fetch_listed, now=now, pricing_path=pricing_path,
                      constants_path=constants_path)
    out, err = capsys.readouterr()
    return rc, out, err, pricing_path, constants_path


def test_log_backed_history_appends_every_move_at_its_own_time_through_a_flip(
        tmp_path, capsys):
    states = [
        ("2030-12-31T23:00:00Z", RATE_A),
        ("2031-01-01T00:10:00Z", RATE_B),
        ("2031-01-01T00:20:00Z", RATE_A),
    ]

    rc, out, err, pricing_path, constants_path = _run(
        tmp_path, capsys, history=states, version=71)

    assert rc == 0 and not err
    saved = json.loads(pricing_path.read_text(encoding="utf-8"))
    history = saved["providers"][MODEL][HOST]
    assert history == [_entry(None, RATE_A), _entry(states[1][0], RATE_B),
                       _entry(states[2][0], RATE_A)]
    assert saved["provider_rates_fetched"] == STAMP
    assert 'PRICING_VERSION = "72"' in constants_path.read_text(encoding="utf-8")
    assert "alternating price" not in out
    assert "2 log entries, newest fresh 0.3 → 0.3" in out


def test_log_changes_at_or_before_the_newest_entry_are_ignored(tmp_path, capsys):
    row = [_entry(None, RATE_A), _entry("2031-01-01T00:10:00Z", RATE_B)]
    states = [
        ("2031-01-01T00:00:00Z", RATE_A),
        ("2031-01-01T00:10:00Z", RATE_B),
        ("2031-01-01T00:15:00Z", RATE_A),
    ]

    rc, _, err, pricing_path, _ = _run(tmp_path, capsys, history=states, hosts={HOST: row})

    assert rc == 0 and not err
    saved = json.loads(pricing_path.read_text(encoding="utf-8"))
    assert saved["providers"][MODEL][HOST] == row + [_entry(states[2][0], RATE_A)]


def test_a_new_host_gets_its_whole_log_history(tmp_path, capsys):
    states = [("2030-12-31T23:00:00Z", RATE_A),
              ("2031-01-01T00:10:00Z", RATE_B)]

    rc, _, err, pricing_path, _ = _run(tmp_path, capsys, history=states, hosts={})

    assert rc == 0 and not err
    saved = json.loads(pricing_path.read_text(encoding="utf-8"))
    assert saved["providers"][MODEL][HOST] == [_entry(at, rates) for at, rates in states]


def test_disagreement_samples_the_host_at_detection_time_with_a_notice(tmp_path, capsys):
    states = [("2030-12-31T23:00:00Z", RATE_A),
              ("2031-01-01T00:10:00Z", RATE_B)]

    rc, out, err, pricing_path, _ = _run(
        tmp_path, capsys, history=states, endpoint_rates=RATE_C)

    assert rc == 0 and not err
    saved = json.loads(pricing_path.read_text(encoding="utf-8"))
    assert saved["providers"][MODEL][HOST][-1] == _entry(STAMP, RATE_C)
    assert f"{HOST}" in out and "disagrees" in out
    assert f"sampled   {HOST}" in out


def test_log_fetch_failure_samples_every_host_without_refusing_the_run(tmp_path, capsys):
    other = "Other"
    rows = {HOST: [_entry(None, RATE_A)], other: [_entry(None, RATE_B)]}
    states = [("2030-12-31T23:00:00Z", RATE_A)]

    def fail_log():
        raise TimeoutError("synthetic timeout")

    rc, out, err, pricing_path, _ = _run(
        tmp_path, capsys, history=states, hosts=rows, log_fetch=fail_log)

    assert rc == 0 and not err
    assert f"listed-pricing log unavailable for {MODEL}" in out
    assert f"sampled   {other}, {HOST}" in out
    saved = json.loads(pricing_path.read_text(encoding="utf-8"))
    assert len(saved["providers"][MODEL]) == 2


def test_missing_canonical_slug_samples_every_host_without_refusal(tmp_path, capsys):
    states = [("2030-12-31T23:00:00Z", RATE_A)]
    catalog = {"data": [{"id": MODEL_ID}]}

    rc, out, err, _, _ = _run(tmp_path, capsys, history=states, catalog=catalog)

    assert rc == 0 and not err
    assert "canonical_slug" in out
    assert f"sampled   {HOST}" in out


def test_log_append_writes_a_file_accepted_by_both_rate_loaders(tmp_path, capsys):
    states = [("2030-12-31T23:00:00Z", RATE_A),
              ("2031-01-01T00:10:00Z", RATE_B),
              ("2031-01-01T00:20:00Z", RATE_A)]
    rc, _, err, pricing_path, _ = _run(tmp_path, capsys, history=states)

    assert rc == 0 and not err
    written = json.loads(pricing_path.read_text(encoding="utf-8"))
    loaded = pricing.load_tables(written)
    assert loaded["PROVIDER_RATES"][(MODEL, HOST)] == RATE_A
    assert len(loaded["PROVIDER_DATED_RATES"][(MODEL, HOST)]) == 2
    if shutil.which("node"):
        parser_path = ROOT / "src" / "parser.js"
        browser_dir = tmp_path / "browser"
        browser_dir.mkdir()
        shutil.copy(pricing_path, browser_dir / "pricing.json")
        shutil.copy(parser_path, browser_dir / "parser.js")
        proc = subprocess.run(
            ["node", "-e", "global.window = {}; require('./parser.js'); "
             "console.log(JSON.stringify(['2031-01-01T00:09:59Z', "
             "'2031-01-01T00:10:00Z', '2031-01-01T00:20:00Z'].map(ts => "
             "window.rateForModel('synthetic/model', ts, 'Wafer').fresh)));"],
            cwd=browser_dir, capture_output=True, text=True, timeout=60, check=False)
        assert proc.returncode == 0, proc.stderr
        assert json.loads(proc.stdout) == [0.3, 0.2, 0.3]
