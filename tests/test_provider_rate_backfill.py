from __future__ import annotations

import copy
import importlib.util
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.refresh_fixture_builders import (
    RATE_C, _endpoint as fixture_endpoint, seed_doc,
)
from backend.pricing_document import expand_pricing_doc, serialize_pricing_doc
from backend.pricing_rows import decode_pricing_doc, encode_pricing_doc

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / "scripts" / "ci"
sys.path.insert(0, str(CI))


def _load():
    path = CI / "backfill_provider_rates.py"
    spec = importlib.util.spec_from_file_location("backfill_provider_rates", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["backfill_provider_rates"] = module
    spec.loader.exec_module(module)
    return module


backfill = _load()


def _saved_doc(path: Path) -> dict:
    return expand_pricing_doc(json.loads(path.read_text(encoding="utf-8")))


MODEL = "synthetic/model"
MODEL_ID = "synthetic/model-id"
SLUG = "synthetic/canonical-model"
AS_OF = "2031-01-01T00:15:00Z"


def test_backfill_overlays_dated_context_and_search_without_losing_token_changes():
    history = [
        {**_entry(None, RATE_A), 'context_length': 131072, 'web_search': 0.01},
        {**_entry('2031-01-01T00:03:00Z', RATE_A),
         'context_length': 786432, 'web_search': 0.01},
        {**_entry('2031-01-01T00:08:00Z', RATE_B),
         'context_length': 786432, 'web_search': 0.02},
        {**_entry(AS_OF, RATE_B), 'context_length': 1048576, 'web_search': 0.03}]
    entries = [_entry('2031-01-01T00:00:00Z', RATE_A),
               _entry('2031-01-01T00:06:00Z', RATE_B)]
    rewritten = backfill._rewrite(history, entries, AS_OF)  # pylint: disable=protected-access
    assert rewritten == [history[0], history[1],
                         {**_entry('2031-01-01T00:06:00Z', RATE_B),
                          'context_length': 786432, 'web_search': 0.01},
                         history[2]]


# After every fixture series point, so the join reads each series whole.
NOW = datetime(2031, 1, 1, 0, 30, tzinfo=timezone.utc)
RATE_A = {"fresh": 0.3, "create_5m": 0.3, "create_1h": 0.3,
          "read": 0.01, "output": 0.8}
RATE_B = {"fresh": 0.2, "create_5m": 0.2, "create_1h": 0.2,
          "read": 0.02, "output": 0.7}


def _entry(at: str | list[str] | None, rates: dict) -> dict:
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
        # At the backfill horizon: every seeded row entry is dated at or
        # before it, as requires of a stored document.
        fetched=AS_OF,
    )


@dataclass
class BackfillCase:
    hosts: dict[str, list[dict]]
    states: list[tuple[str, dict]]
    as_of: str
    args: list[str]
    new_hosts: list[str]
    now: datetime = NOW


class BackfillFetches:
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
    original = serialize_pricing_doc(doc)
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
    saved = _saved_doc(pricing_path)
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
    saved = _saved_doc(pricing_path)
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
    saved = _saved_doc(pricing_path)
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
    saved = _saved_doc(pricing_path)
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
    saved = _saved_doc(pricing_path)
    assert saved["providers"][MODEL]["Wafer"] == old
    assert "untouched Wafer" in out
    assert "no in-force log state matches the listing" in out
    assert pricing_path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("days", [4, 5, 30, 31])
def test_backfill_daily_alternation_merges_losslessly(tmp_path, capsys, days):
    start = datetime(2030, 11, 1, tzinfo=timezone.utc)
    states = [((start + timedelta(days=i)).isoformat().replace("+00:00", "Z"),
               RATE_A if i % 2 == 0 else RATE_B) for i in range(days)]
    old = [_entry(states[0][0], RATE_C)]

    rc, _, err, path, _, _ = _run(tmp_path, capsys, {"Wafer": old}, states)

    assert rc == 0 and not err
    stored = json.loads(path.read_text(encoding="utf-8"))
    history = expand_pricing_doc(stored)["providers"][MODEL]["Wafer"]
    assert len(history) == 2
    assert all(isinstance(entry["from"], list) for entry in history)
    flattened = sorted((stamp, {k: v for k, v in entry.items() if k != "from"})
                       for entry in history for stamp in entry["from"])
    assert flattened == states
    assert stored == encode_pricing_doc(decode_pricing_doc(stored))


def test_backfill_accepts_grouped_input_and_compares_the_chronological_newest(tmp_path, capsys):
    states = [("2030-12-31T23:00:00Z", RATE_A),
              ("2031-01-01T00:06:00Z", RATE_B),
              ("2031-01-01T00:12:00Z", RATE_A)]
    history = [_entry([states[0][0], states[2][0]], RATE_A),
               _entry(states[1][0], RATE_B)]

    rc, out, err, path, _, _ = _run(tmp_path, capsys, {"Wafer": history}, states)

    assert rc == 0 and not err
    assert _saved_doc(path)["providers"][MODEL]["Wafer"] == history
    assert "newest log state through" not in out


def test_backfill_null_start_stays_scalar_when_its_rate_returns(tmp_path, capsys):
    states = [("2030-12-31T23:00:00Z", RATE_A),
              ("2031-01-01T00:06:00Z", RATE_B),
              ("2031-01-01T00:12:00Z", RATE_A)]

    rc, _, err, path, _, _ = _run(
        tmp_path, capsys, {"Wafer": [_entry(None, RATE_A)]}, states)

    assert rc == 0 and not err
    assert _saved_doc(path)["providers"][MODEL]["Wafer"] == [
        _entry(None, RATE_A), _entry(states[1][0], RATE_B), _entry(states[2][0], RATE_A)]


@pytest.mark.parametrize("start", [None, "2030-12-31T22:00:00Z"])
def test_backfill_full_rewrite_overlays_sampled_search_history(tmp_path, capsys, start):
    states = [("2030-12-31T23:00:00Z", RATE_A),
              ("2031-01-01T00:06:00Z", RATE_B),
              ("2031-01-01T00:10:00Z", RATE_C)]
    history = [
        {**_entry(start, RATE_C), "web_search": 0.01},
        {**_entry("2031-01-01T00:04:00Z", RATE_B), "web_search": 0.01},
        {**_entry("2031-01-01T00:05:00Z", RATE_B), "web_search": 0.02},
        {**_entry("2031-01-01T00:06:00Z", RATE_C), "web_search": 0.03},
        _entry("2031-01-01T00:08:00Z", RATE_C),
        {**_entry("2031-01-01T00:10:00Z", RATE_C), "web_search": 0.04},
        {**_entry("2031-01-01T00:12:00Z", RATE_C), "web_search": 0.05},
    ]

    rc, out, err, path, _, _ = _run(
        tmp_path, capsys, {"Wafer": history}, states, as_of="2031-01-01T00:10:00Z")

    assert rc == 0 and not err
    assert _saved_doc(path)["providers"][MODEL]["Wafer"] == [
        {**_entry(start, RATE_A), "web_search": 0.01},
        {**_entry("2031-01-01T00:05:00Z", RATE_A), "web_search": 0.02},
        {**_entry("2031-01-01T00:06:00Z", RATE_B), "web_search": 0.03},
        _entry("2031-01-01T00:08:00Z", RATE_B),
        _entry("2031-01-01T00:10:00Z", RATE_C),
    ]
    assert any("Wafer" in line and "overlaid" in line and "web_search" in line
               for line in out.splitlines())


def test_backfill_full_rewrite_search_history_flattens_old_groups(tmp_path, capsys):
    states = [("2030-12-31T23:00:00Z", RATE_A),
              ("2031-01-01T00:06:00Z", RATE_B),
              ("2031-01-01T00:10:00Z", RATE_A)]
    history = [
        _entry(None, RATE_C),
        {**_entry(["2031-01-01T00:05:00Z", "2031-01-01T00:12:00Z"], RATE_C),
         "web_search": 0.02},
        {**_entry("2031-01-01T00:09:00Z", RATE_C), "web_search": 0.03},
    ]

    rc, _, err, path, _, _ = _run(tmp_path, capsys, {"Wafer": history}, states)

    assert rc == 0 and not err
    assert _saved_doc(path)["providers"][MODEL]["Wafer"] == [
        _entry(None, RATE_A),
        {**_entry(["2031-01-01T00:05:00Z", "2031-01-01T00:12:00Z"], RATE_A),
         "web_search": 0.02},
        {**_entry("2031-01-01T00:06:00Z", RATE_B), "web_search": 0.02},
        {**_entry("2031-01-01T00:09:00Z", RATE_B), "web_search": 0.03},
        {**_entry("2031-01-01T00:10:00Z", RATE_A), "web_search": 0.03},
    ]


def test_backfill_full_rewrite_search_starts_at_old_first_instant(tmp_path, capsys):
    states = [("2030-12-31T23:00:00Z", RATE_A),
              ("2031-01-01T00:06:00Z", RATE_B)]
    history = [{**_entry("2030-12-31T23:30:00Z", RATE_C), "web_search": 0.02}]

    rc, _, err, path, _, _ = _run(tmp_path, capsys, {"Wafer": history}, states)

    assert rc == 0 and not err
    assert _saved_doc(path)["providers"][MODEL]["Wafer"] == [
        _entry("2030-12-31T23:00:00Z", RATE_A),
        {**_entry("2030-12-31T23:30:00Z", RATE_A), "web_search": 0.02},
        {**_entry("2031-01-01T00:06:00Z", RATE_B), "web_search": 0.02},
    ]


@pytest.mark.parametrize("band", [{}, {"fresh": [0, 100]}, None])
def test_backfill_refuses_band_input_before_fetching_or_writing(tmp_path, capsys, band):
    doc = _doc({"Wafer": [{**_entry(None, RATE_A), "band": band}]})
    path, constants = tmp_path / "pricing.json", tmp_path / "constants.py"
    original = serialize_pricing_doc(doc)
    path.write_text(original, encoding="utf-8")
    version = 'PRICING_VERSION = "31"\n'
    constants.write_text(version, encoding="utf-8")

    def unexpected_fetch(*_args):
        pytest.fail("an invalid source must be refused before any fetch")

    rc = backfill.main(
        ["--as-of", AS_OF], fetch=unexpected_fetch, fetch_models=unexpected_fetch,
        fetch_log=unexpected_fetch, pricing_path=path, constants_path=constants, now=NOW)
    out, err = capsys.readouterr()

    assert rc == 1 and not out
    assert "the source file would not load" in err
    assert f"{MODEL} via Wafer[0]" in err and "band" in err
    assert path.read_text(encoding="utf-8") == original
    assert constants.read_text(encoding="utf-8") == version


def test_backfill_unavailable_log_preserves_history_and_reports_failure():
    history = [_entry(None, RATE_A), _entry("2031-01-01T00:05:00Z", RATE_C)]
    doc = _doc({"Wafer": history})
    fetches = BackfillFetches(
        [fixture_endpoint("Wafer", RATE_C, tag="wafer/fp8")], {})

    def unavailable(_slug):
        raise OSError("synthetic unavailable log")

    result, reports, changed = backfill.backfill(
        doc, fetches.fetch_endpoints, AS_OF, fetches.fetch_models, unavailable, NOW)

    assert not changed
    assert result["providers"][MODEL]["Wafer"] == history
    assert any("untouched Wafer" in line and "synthetic unavailable log" in line
               for line in reports[MODEL])


def test_backfill_preserves_search_rate_and_as_of(tmp_path, capsys):
    states = [("2030-12-31T23:00:00Z", RATE_A),
              ("2031-01-01T00:06:00Z", RATE_B),
              ("2031-01-01T00:20:00Z", RATE_A)]
    history = [_entry(None, RATE_B),
               {**_entry("2031-01-01T00:05:00Z", RATE_C), "web_search": 0.03}]

    rc, _, err, path, _, _ = _run(tmp_path, capsys, {"Wafer": history}, states)

    assert rc == 0 and not err
    assert _saved_doc(path)["providers"][MODEL]["Wafer"] == [
        _entry(None, RATE_A),
        {**_entry("2031-01-01T00:05:00Z", RATE_A), "web_search": 0.03},
        {**_entry("2031-01-01T00:06:00Z", RATE_B), "web_search": 0.03}]
