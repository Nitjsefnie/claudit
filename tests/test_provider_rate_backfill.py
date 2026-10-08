"""One-time historical OpenRouter provider-rate rewrite tests."""
from __future__ import annotations

import copy
import importlib.util
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from tests.refresh_fixture_builders import (
    RATE_C, _endpoint as fixture_endpoint, seed_doc,
)

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / "scripts" / "ci"
sys.path.insert(0, str(CI))


def _load():
    """Import scripts/ci/backfill_provider_rates.py by path."""
    path = CI / "backfill_provider_rates.py"
    spec = importlib.util.spec_from_file_location("backfill_provider_rates", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["backfill_provider_rates"] = module
    spec.loader.exec_module(module)
    return module


backfill = _load()

MODEL = "synthetic/model"
MODEL_ID = "synthetic/model-id"
SLUG = "synthetic/canonical-model"
AS_OF = "2031-01-01T00:15:00Z"
# After every fixture series point, so the join reads each series whole.
NOW = datetime(2031, 1, 1, 0, 30, tzinfo=timezone.utc)
RATE_A = {"fresh": 0.3, "create_5m": 0.3, "create_1h": 0.3,
          "read": 0.01, "output": 0.8}
RATE_B = {"fresh": 0.2, "create_5m": 0.2, "create_1h": 0.2,
          "read": 0.02, "output": 0.7}

def _entry(at: str | None, rates: dict) -> dict:
    return {"from": at, **rates}


def _series(host: str, slug: str, states: list[tuple[str, dict]]) -> dict:
    fields = {"input": [], "output": [], "cacheRead": [], "cacheWrite": [], "discount": []}
    for at, rates in states:
        fields["input"].append({"at": at, "value": rates["fresh"]})
        fields["output"].append({"at": at, "value": rates["output"]})
        fields["cacheRead"].append({"at": at, "value": rates["read"]})
        write = rates["create_5m"] if rates["create_5m"] != rates["fresh"] else 0
        fields["cacheWrite"].append({"at": at, "value": write})
        fields["discount"].append({"at": at, "value": 0})
    return {"endpointId": f"synthetic-{slug}", "providerName": host,
            "providerSlug": slug, **fields}


def _doc(hosts: dict[str, list[dict]]) -> dict:
    return seed_doc(
        models={MODEL: [{"from": None, **RATE_A}]},
        providers={MODEL: hosts},
        tracked={MODEL: {"id": MODEL_ID}},
        fetched="2026-01-01T00:00:00Z",
    )


@dataclass
class BackfillCase:
    """Inputs for one synthetic backfill invocation."""
    hosts: dict[str, list[dict]]
    states: list[tuple[str, dict]]
    as_of: str
    args: list[str]
    new_hosts: list[str]
    now: datetime = NOW


class BackfillFetches:
    """Network stubs used by every backfill test in this file."""
    def __init__(self, endpoints: list[dict], log: dict) -> None:
        self.endpoints = endpoints
        self.log = log

    def fetch_endpoints(self, model_id: str) -> object:
        assert model_id == MODEL_ID
        return {"data": {"endpoints": copy.deepcopy(self.endpoints)}}

    def fetch_models(self) -> object:
        return {"data": [{"id": MODEL_ID, "canonical_slug": SLUG}]}

    def fetch_log(self, canonical_slug: str) -> object:
        assert canonical_slug == SLUG
        return copy.deepcopy(self.log)


def _request_payloads(hosts: dict[str, list[dict]], states: list[tuple[str, dict]],
                      new_hosts: list[str]) -> tuple[list[dict], dict]:
    endpoints = []
    for host, history in hosts.items():
        listed = states[-1][1] if host == "Wafer" else history[-1]
        endpoints.append(fixture_endpoint(host, listed, tag=f"{host.lower()}/fp8"))
    endpoints.extend(fixture_endpoint(host, RATE_A, tag=f"{host.lower()}/fp8")
                     for host in new_hosts)
    log = {"data": {"series": [_series("Wafer", "wafer", states)]}}
    return endpoints, log


def _run(tmp_path: Path, capsys, hosts: dict[str, list[dict]],
         states: list[tuple[str, dict]], *, as_of: str = AS_OF,
         args: list[str] | None = None, new_hosts: list[str] | None = None):
    case = BackfillCase(hosts, states, as_of, args or [], new_hosts or [])
    return _run_case(tmp_path, capsys, case)


def _run_case(tmp_path: Path, capsys, case: BackfillCase):
    doc = _doc(case.hosts)
    pricing_path = tmp_path / "pricing.json"
    constants_path = tmp_path / "constants.py"
    original = json.dumps(doc, indent=2, sort_keys=True) + "\n"
    pricing_path.write_text(original, encoding="utf-8")
    constants_path.write_text('PRICING_VERSION = "31"\n', encoding="utf-8")
    endpoints, log = _request_payloads(case.hosts, case.states, case.new_hosts)
    fetches = BackfillFetches(endpoints, log)
    rc = backfill.main(["--as-of", case.as_of, *case.args], fetch=fetches.fetch_endpoints,
                       fetch_models=fetches.fetch_models, fetch_log=fetches.fetch_log,
                       pricing_path=pricing_path, constants_path=constants_path,
                       now=case.now)
    out, err = capsys.readouterr()
    return rc, out, err, pricing_path, constants_path, original


def test_backfill_keeps_null_start_and_drops_changes_after_as_of(tmp_path, capsys):
    states = [("2030-12-31T23:00:00Z", RATE_A),
              ("2031-01-01T00:10:00Z", RATE_B),
              ("2031-01-01T00:20:00Z", RATE_C)]
    old = [_entry(None, RATE_A), _entry("2031-01-01T00:05:00Z", RATE_C)]

    rc, out, err, pricing_path, constants_path, _ = _run(
        tmp_path, capsys, {"Wafer": old}, states)

    assert rc == 0 and not err
    saved = json.loads(pricing_path.read_text(encoding="utf-8"))
    assert saved["providers"][MODEL]["Wafer"] == [
        _entry(None, RATE_A), _entry("2031-01-01T00:10:00Z", RATE_B)]
    assert saved["provider_rates_fetched"] == AS_OF
    assert 'PRICING_VERSION = "32"' in constants_path.read_text(encoding="utf-8")
    assert "Wafer: 2 → 2 entries" in out
    assert "newest log state through" in out


def test_backfill_keeps_an_earlier_non_null_start(tmp_path, capsys):
    states = [("2030-12-31T23:00:00Z", RATE_A),
              ("2031-01-01T00:10:00Z", RATE_B),
              ("2031-01-01T00:20:00Z", RATE_C)]
    old = [_entry("2030-01-01T00:00:00Z", RATE_A)]

    rc, _, err, pricing_path, _, _ = _run(tmp_path, capsys, {"Wafer": old}, states)

    assert rc == 0 and not err
    saved = json.loads(pricing_path.read_text(encoding="utf-8"))
    rewritten = saved["providers"][MODEL]["Wafer"]
    assert len(rewritten) == 2
    assert rewritten[0] == _entry("2030-01-01T00:00:00Z", RATE_A)
    assert rewritten[1] == _entry("2031-01-01T00:10:00Z", RATE_B)


def test_backfill_leaves_sampled_rows_unchanged_and_reports_the_reason(tmp_path, capsys):
    other = "BaseTen"
    rows = {"Wafer": [_entry(None, RATE_A)], other: [_entry(None, RATE_B)]}
    states = [("2030-12-31T23:00:00Z", RATE_A),
              ("2031-01-01T00:20:00Z", RATE_C)]

    rc, out, err, pricing_path, _, _ = _run(tmp_path, capsys, rows, states)

    assert rc == 0 and not err
    saved = json.loads(pricing_path.read_text(encoding="utf-8"))
    assert saved["providers"][MODEL][other] == rows[other]
    assert f"untouched {other}:" in out
    assert "series count does not match endpoint count" in out


def test_backfill_reports_hosts_without_rows_without_creating_new_rows(tmp_path, capsys):
    states = [("2030-12-31T23:00:00Z", RATE_A),
              ("2031-01-01T00:20:00Z", RATE_C)]

    rc, out, err, pricing_path, _, _ = _run(
        tmp_path, capsys, {"Wafer": [_entry(None, RATE_A)]}, states,
        new_hosts=["NewHost"])

    assert rc == 0 and not err
    saved = json.loads(pricing_path.read_text(encoding="utf-8"))
    assert "NewHost" not in saved["providers"][MODEL]
    assert "untouched NewHost: no row" in out
    assert "series count does not match endpoint count" in out


def test_backfill_dry_run_writes_nothing_and_reports_entry_counts(tmp_path, capsys):
    states = [("2030-12-31T23:00:00Z", RATE_A),
              ("2031-01-01T00:10:00Z", RATE_B),
              ("2031-01-01T00:20:00Z", RATE_C)]
    old = [_entry(None, RATE_A), _entry("2031-01-01T00:05:00Z", RATE_B),
           _entry("2031-01-01T00:12:00Z", RATE_C)]

    rc, out, err, pricing_path, constants_path, original = _run(
        tmp_path, capsys, {"Wafer": old}, states, args=["--dry-run"])

    assert rc == 0 and not err
    assert pricing_path.read_text(encoding="utf-8") == original
    assert constants_path.read_text(encoding="utf-8") == 'PRICING_VERSION = "31"\n'
    assert "Wafer: 3 → 2 entries" in out


def test_backfill_leaves_a_row_backed_only_by_a_future_point_untouched(tmp_path, capsys):
    states = [("2030-12-31T23:00:00Z", RATE_A),
              ("2099-01-01T00:00:00Z", RATE_C)]
    old = [_entry(None, RATE_A)]

    rc, out, err, pricing_path, _, original = _run(
        tmp_path, capsys, {"Wafer": old}, states)

    assert rc == 0 and not err
    saved = json.loads(pricing_path.read_text(encoding="utf-8"))
    assert saved["providers"][MODEL]["Wafer"] == old
    assert "untouched Wafer" in out
    assert "no in-force log state matches the listing" in out
    assert pricing_path.read_text(encoding="utf-8") == original
