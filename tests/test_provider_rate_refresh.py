"""SV-RATE-REFRESH: scripts/ci/refresh_provider_rates.py, driven by fixture
endpoint payloads — never the network.

Every run starts from the SEEDED view of src/pricing.json: each provider row
as it was seeded, before any refresh appended to it. The refresh only ever
appends dated entries, so that view, and every price these tests move away
from, is the same whatever the scheduled job has committed since. The
payloads reproduce it, so "nothing moved" is exact, and each test moves one
thing.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from backend import pricing
from tests.refresh_fixture_builders import (_discount, _endpoint, _overrides, _per_token)

ROOT = Path(__file__).resolve().parents[1]
PRICING_JSON = ROOT / "src" / "pricing.json"  # sv-test-data: allow (seed template only; the docs under test are synthetic seeded views)
CONSTANTS_PY = ROOT / "backend" / "constants.py"
PARSER_JS = ROOT / "src" / "parser.js"
RATE_FIELDS = ("fresh", "create_5m", "create_1h", "read", "output")
FUZZ_RESERVED_NAMESPACE = "zz-fuzz-local/"
UTC = timezone.utc
NOW = datetime(2031, 1, 1, tzinfo=UTC)
STAMP = "2031-01-01T00:00:00Z"
GLM = "z-ai/glm-5-3-flash"
V41 = "deepseek/deepseek-v4-1-flash"
NEWCOMER = {"fresh": 0.2, "create_5m": 0.2, "create_1h": 0.2,
            "read": 0.05, "output": 0.9}

needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available"
)


def _load():
    """Import scripts/ci/refresh_provider_rates.py by path (scripts/ci is
    not a package)."""
    path = ROOT / "scripts" / "ci" / "refresh_provider_rates.py"
    spec = importlib.util.spec_from_file_location("refresh_provider_rates", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["refresh_provider_rates"] = module
    spec.loader.exec_module(module)
    return module


refresh = _load()


def _seeded(doc: dict) -> dict:
    """The file with every provider row cut back to its seeded entry, and
    rows first seen by a refresh or reserved for test-data fuzzing dropped."""
    doc = copy.deepcopy(doc)
    providers = {}
    for model, hosts in doc["providers"].items():
        if model.startswith(FUZZ_RESERVED_NAMESPACE):
            continue
        seeded_hosts = {
            host: history[:1] for host, history in hosts.items()
            if (history[0]["from"] is None
                and not host.startswith(FUZZ_RESERVED_NAMESPACE))
        }
        if seeded_hosts:
            providers[model] = seeded_hosts
    doc["providers"] = providers
    doc["provider_rates_fetched"] = "2026-09-24T22:03:13Z"
    return doc


def _payloads(doc: dict) -> dict:
    """Every tracked model's endpoints payload, reproducing the newest
    entry of every provider row; a pinned host is listed under its pin's
    tag, as it is live. BaseTen's deepseek-v4.1-flash lists its two
    endpoints as OpenRouter does: same tag and quantization, cache 0.007/0.03,
    and the 1-token max_completion_tokens artifact (32767/32768) the recorded
    resolution ignores."""
    out = {}
    for key, hosts in doc["providers"].items():
        pinned = {host: pin["tag"] for host, pin in
                  doc["openrouter"]["models"][key].get("resolve", {}).items()
                  if pin.get("tag")}
        endpoints = [_endpoint(host, history[-1], _discount(history[-1]))
                     for host, history in hosts.items()]
        for endpoint, history in zip(endpoints, hosts.values()):
            if history[-1].get("schedule"):
                endpoint["pricing"]["overrides"] = _overrides(history[-1]["schedule"])
            if endpoint["provider_name"] in pinned:
                endpoint["tag"] = pinned[endpoint["provider_name"]]
        if key == V41:
            for endpoint in endpoints:
                if endpoint["provider_name"] == "BaseTen":
                    endpoint["tag"] = "baseten/fp8"
                    endpoint["max_completion_tokens"] = 32767
            us_region = {**hosts["BaseTen"][-1], "read": 0.03}
            us_twin = _endpoint("BaseTen", us_region, tag="baseten/fp8")
            us_twin["max_completion_tokens"] = 32768
            endpoints.append(us_twin)
        model_id = doc["openrouter"]["models"][key]["id"]
        out[model_id] = {"data": {"id": model_id, "name": model_id,
                                  "endpoints": endpoints}}
    return out


class Run:
    """One refresh run against a private copy of the two tracked files."""

    def __init__(self, tmp_path: Path, pricing_version: str | None = None):
        self.pricing = tmp_path / "pricing.json"
        self.constants = tmp_path / "constants.py"
        self.pricing.write_text(json.dumps(
            _seeded(json.loads(PRICING_JSON.read_text(encoding="utf-8"))),
            indent=2, sort_keys=True) + "\n", encoding="utf-8")
        text = CONSTANTS_PY.read_text(encoding="utf-8")
        if pricing_version is not None:
            text = re.sub(r'(?m)^PRICING_VERSION = "\d+"$',
                          f'PRICING_VERSION = "{pricing_version}"', text)
        self.constants.write_text(text, encoding="utf-8")
        self.payloads = _payloads(self.doc())
        self.commit_msg = tmp_path / "commit-msg.txt"

    def doc(self) -> dict:
        return json.loads(self.pricing.read_text(encoding="utf-8"))

    def endpoints(self, key: str) -> list[dict]:
        return self.payloads[self.doc()["openrouter"]["models"][key]["id"]]["data"]["endpoints"]

    def edit(self, change) -> None:
        """Change the run's pricing.json in place, keeping its layout."""
        doc = self.doc()
        change(doc)
        self.pricing.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n",
                                encoding="utf-8")

    def endpoint(self, key: str, host: str) -> dict:
        return next(e for e in self.endpoints(key) if e["provider_name"] == host)

    def snapshot(self) -> tuple[bytes, bytes]:
        return self.pricing.read_bytes(), self.constants.read_bytes()

    def pricing_version(self) -> int:
        m = re.search(r'(?m)^PRICING_VERSION = "(\d+)"$',
                      self.constants.read_text(encoding="utf-8"))
        assert m
        return int(m.group(1))

    def __call__(self, capsys, *args: str, now: datetime = NOW):
        rc = refresh.main(["--commit-msg", str(self.commit_msg), *args],
                          fetch=lambda model_id: copy.deepcopy(self.payloads[model_id]),
                          fetch_models=lambda: {"data": [
                              {"id": source["id"], "canonical_slug": source["id"]}
                              for source in self.doc()["openrouter"]["models"].values()]},
                          fetch_log=lambda _slug: {"data": {"series": []}},
                          now=now, pricing_path=self.pricing, constants_path=self.constants)
        out, err = capsys.readouterr()
        return rc, out, err


# --- the tracked set ---------------------------------------------------------


def test_every_provider_table_model_names_its_openrouter_id():
    doc = json.loads(PRICING_JSON.read_text(encoding="utf-8"))
    assert doc["openrouter"]["data_region"] == "global"
    assert set(doc["openrouter"]["models"]) == set(doc["providers"])
    for key, entry in doc["openrouter"]["models"].items():
        assert pricing._normalise(entry["id"]) == key  # pylint: disable=protected-access


# --- nothing moved -----------------------------------------------------------


def test_a_run_with_no_moves_leaves_every_file_byte_identical(tmp_path, capsys):
    run = Run(tmp_path)
    before = run.snapshot()
    rc, out, _ = run(capsys)
    assert rc == 0
    assert run.snapshot() == before
    assert not run.commit_msg.exists()
    assert "no rate moved" in out


# --- a price moves -----------------------------------------------------------


def _move_openinference(run: Run, discount: float = 0) -> dict:
    moved = {**(seeded := run.doc()["providers"][GLM]["OpenInference"][-1]),
             **{f: seeded[f] * k or 0.01 for f, k in zip(RATE_FIELDS, (.7, .7, .7, 1.3, .8))}}
    endpoint = run.endpoint(GLM, "OpenInference")
    endpoint.update(_endpoint("OpenInference", moved, discount))
    endpoint["pricing"]["input_cache_write"] = _per_token(moved["create_5m"])
    return {f: moved[f] for f in RATE_FIELDS}


def test_a_moved_price_appends_an_entry_from_the_detection_time(tmp_path, capsys):
    run = Run(tmp_path)
    before = run.doc()
    moved = _move_openinference(run)
    rc, out, _ = run(capsys)
    assert rc == 0
    after = run.doc()
    history = after["providers"][GLM]["OpenInference"]
    assert history[:-1] == before["providers"][GLM]["OpenInference"]
    assert history[-1] == {"from": STAMP, **moved}
    # Nothing else in the file moved but the fetch stamp.
    after["providers"][GLM]["OpenInference"] = history[:-1]
    assert after == {**before, "provider_rates_fetched": STAMP}
    text = run.pricing.read_text(encoding="utf-8")
    assert text == json.dumps(json.loads(text), indent=2, sort_keys=True) + "\n"
    assert "OpenInference" in out and f"{history[-2]['fresh']!r} → {history[-1]['fresh']!r}" in out


def test_a_run_that_appends_bumps_pricing_version_relative_to_the_file(
        tmp_path, capsys):
    """Current + 1, whatever the file holds when the run starts, so a manual
    bump landing first is never collided with. Only the version line moves:
    the bump writes no comment line, the history being the commit that
    carries the bump."""
    run = Run(tmp_path, pricing_version="73")
    _move_openinference(run)
    assert run(capsys)[0] == 0
    assert run.pricing_version() == 74
    text = run.constants.read_text(encoding="utf-8")
    assert len(re.findall(r'(?m)^PRICING_VERSION = "\d+"$', text)) == 1
    assert not re.search(r"(?m)^# 74\b", text)


def test_the_commit_message_names_the_counts_and_carries_the_report(
        tmp_path, capsys):
    run = Run(tmp_path)
    _move_openinference(run)
    run.endpoints(GLM).append(_endpoint("Newcomer", NEWCOMER))
    rc, out, _ = run(capsys)
    assert rc == 0
    message = run.commit_msg.read_text(encoding="utf-8")
    subject, _, body = message.partition("\n\n")
    assert subject == "Refresh OpenRouter provider rates: 1 changed, 1 new"
    assert out.strip() in body
    assert "Co-Authored-By" not in message


@pytest.fixture(name="moved_run")
def _moved_run_fixture(tmp_path, capsys):
    run = Run(tmp_path)
    moved = _move_openinference(run)
    old = {f: run.doc()["providers"][GLM]["OpenInference"][-1][f]
           for f in RATE_FIELDS}
    assert run(capsys)[0] == 0
    return run, old, moved


def test_a_moved_price_prices_by_time_in_the_backend(moved_run, monkeypatch):
    run, old, moved = moved_run
    for name, value in pricing.load_tables(run.doc()).items():
        monkeypatch.setattr(pricing, name, value)
    cut = datetime.fromisoformat(STAMP)
    model = "z-ai/glm-5.3-flash"
    assert pricing.rate_for(model, cut - timedelta(seconds=1), "OpenInference") == old  # sv-test-data: allow (derived: expected values read from the run's own seeded document)
    assert pricing.rate_for(model, cut, "OpenInference") == moved  # sv-test-data: allow (derived: expected values read from the run's own seeded document)


@needs_node
def test_a_moved_price_prices_by_time_in_the_browser(moved_run, tmp_path):
    run, old, moved = moved_run
    got = _node_rates(run, tmp_path / "js", "OpenInference")
    assert got == [old, moved]


# --- a provider appears ------------------------------------------------------

def test_a_new_provider_gets_a_row_that_begins_at_the_detection_time(
        tmp_path, capsys, monkeypatch):
    run = Run(tmp_path)
    run.endpoints(GLM).append(_endpoint("Newcomer", NEWCOMER))
    rc, out, _ = run(capsys)
    assert rc == 0
    assert run.doc()["providers"][GLM]["Newcomer"] == [{"from": STAMP, **NEWCOMER}]
    assert "Newcomer" in out
    cut = datetime.fromisoformat(STAMP)
    model = "z-ai/glm-5.3-flash"
    fallback = pricing.resolve(model, cut - timedelta(seconds=1))
    for name, value in pricing.load_tables(run.doc()).items():
        monkeypatch.setattr(pricing, name, value)
    assert pricing.resolve(model, cut - timedelta(seconds=1), "Newcomer") == fallback
    assert pricing.rate_for(model, cut, "Newcomer") == NEWCOMER


@needs_node
def test_a_new_provider_prices_from_the_detection_time_in_the_browser(
        tmp_path, capsys):
    run = Run(tmp_path)
    run.endpoints(GLM).append(_endpoint("Newcomer", NEWCOMER))
    assert run(capsys)[0] == 0
    fallback = pricing.rate_for("z-ai/glm-5.3-flash",
                                datetime.fromisoformat(STAMP) - timedelta(seconds=1))
    assert _node_rates(run, tmp_path / "js", "Newcomer") == [fallback, NEWCOMER]


def _node_rates(run: Run, where: Path, host: str) -> list[dict]:
    """The browser's rates for `host`'s GLM row one second before, and at,
    the detection time, loading the run's own pricing.json."""
    where.mkdir(exist_ok=True)
    shutil.copy(run.pricing, where / "pricing.json")
    shutil.copy(PARSER_JS, where / "parser.js")
    before = (datetime.fromisoformat(STAMP) - timedelta(seconds=1)).isoformat()
    script = f"""
      global.window = {{}};
      require({str(where / "parser.js")!r});
      const K = {{fresh: 'fresh', c5: 'create_5m', c1h: 'create_1h',
                  read: 'read', out: 'output'}};
      console.log(JSON.stringify([{json.dumps(before)}, {json.dumps(STAMP)}]
        .map(ts => window.rateForModel('z-ai/glm-5.3-flash', ts, {json.dumps(host)}))
        .map(r => Object.fromEntries(Object.entries(K).map(([a, b]) => [b, r[a]])))));
    """
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                          timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# --- a provider vanishes -----------------------------------------------------


def test_a_vanished_provider_keeps_its_row_and_is_reported(tmp_path, capsys):
    run = Run(tmp_path)
    run.endpoints(GLM)[:] = [e for e in run.endpoints(GLM)
                             if e["provider_name"] != "Cloudflare"]
    before = run.snapshot()
    rc, out, _ = run(capsys)
    assert rc == 0
    assert run.snapshot() == before, "a vanished host alone moves nothing"
    assert re.search(r"vanished\b.*Cloudflare", out)


# --- discount metadata -------------------------------------------------------


def test_a_discount_is_recorded_with_the_rate_and_round_trips(tmp_path, capsys):
    run = Run(tmp_path)
    moved = _move_openinference(run, discount=0.3)
    assert run(capsys)[0] == 0
    assert run.doc()["providers"][GLM]["OpenInference"][-1] == {
        "from": STAMP, **moved, "note": "30% off"}
    after_first = run.snapshot()
    assert run(capsys, now=NOW + timedelta(hours=1))[0] == 0
    assert run.snapshot() == after_first, "the same payload again moves nothing"


def test_a_discount_alone_changing_is_not_a_rate_change(tmp_path, capsys):
    """The listed price already has the discount applied; a label moving
    with the price unchanged prices nothing differently."""
    run = Run(tmp_path)
    run.endpoint(GLM, "Novita")["pricing"]["discount"] = 0.5
    before = run.snapshot()
    assert run(capsys)[0] == 0
    assert run.snapshot() == before


# --- normalisation -----------------------------------------------------------


def test_a_listed_cache_write_price_fills_both_create_buckets(tmp_path, capsys):
    run = Run(tmp_path)
    run.endpoint(GLM, "Novita")["pricing"]["input_cache_write"] = "0.0000002"
    assert run(capsys)[0] == 0
    entry = run.doc()["providers"][GLM]["Novita"][-1]
    assert (entry["create_5m"], entry["create_1h"]) == (0.2, 0.2)
    assert entry["fresh"] != 0.2


def test_a_zero_cache_write_price_means_the_input_rate(tmp_path, capsys):
    run = Run(tmp_path)
    run.endpoint(GLM, "Novita")["pricing"]["input_cache_write"] = "0"
    before = run.snapshot()
    assert run(capsys)[0] == 0
    assert run.snapshot() == before


def test_an_absent_cache_read_price_is_zero(tmp_path, capsys):
    run = Run(tmp_path)
    del run.endpoint(GLM, "Novita")["pricing"]["input_cache_read"]
    assert run(capsys)[0] == 0
    assert run.doc()["providers"][GLM]["Novita"][-1]["read"] == 0.0


def test_endpoints_of_one_host_at_one_price_are_one_row(tmp_path, capsys):
    run = Run(tmp_path)
    twin = copy.deepcopy(run.endpoint(GLM, "Novita"))
    twin["tag"] = "novita/fp4"
    run.endpoints(GLM).append(twin)
    before = run.snapshot()
    assert run(capsys)[0] == 0
    assert run.snapshot() == before


# --- designed red runs -------------------------------------------------------


def _refused(run: Run, capsys, *names: str) -> None:
    before = run.snapshot()
    rc, _, err = run(capsys)
    assert rc != 0
    for name in names:
        assert name in err, err
    assert run.snapshot() == before, "a refused run writes nothing"
    assert not run.commit_msg.exists()


def test_a_host_at_two_global_prices_is_refused_naming_the_tags(tmp_path, capsys):
    """Two endpoints outside any region — quantization variants, say — at
    different prices: nothing in the data says which the account gets."""
    run = Run(tmp_path)
    run.endpoint(GLM, "Novita")["tag"] = "novita/fp8"
    twin = copy.deepcopy(run.endpoint(GLM, "Novita"))
    twin["tag"] = "novita/fp4"
    twin["pricing"]["input_cache_read"] = "0.00000005"
    run.endpoints(GLM).append(twin)
    _refused(run, capsys, GLM, "Novita", "novita/fp8", "novita/fp4")


def test_every_refusal_in_a_run_is_named(tmp_path, capsys):
    """One red run names every model a human must look at, not just the
    first one fetched."""
    run = Run(tmp_path)
    twin = copy.deepcopy(run.endpoint(GLM, "Novita"))
    twin["pricing"]["input_cache_read"] = "0.00000005"
    run.endpoints(GLM).append(twin)
    run.endpoints(V41).clear()
    _refused(run, capsys, f"{GLM} via Novita", V41)


def test_a_refused_host_blocks_only_itself(tmp_path, capsys):
    """Every other move is committed, with the PRICING_VERSION bump; the
    refused host's row is untouched; the run still exits nonzero, and both
    its report and the commit message name the refusal."""
    run = Run(tmp_path, pricing_version="73")
    before = run.doc()
    moved = _move_openinference(run)
    _two_global_novitas(run)
    rc, out, err = run(capsys)
    assert rc != 0
    after = run.doc()
    assert after["providers"][GLM]["OpenInference"][-1] == {"from": STAMP, **moved}
    assert after["providers"][GLM]["Novita"] == before["providers"][GLM]["Novita"]
    assert run.pricing_version() == 74
    assert "OpenInference" in out and f"{GLM} via Novita" in out
    assert not re.search(r"vanished\s+Novita", out), "refused is not vanished"
    assert f"{GLM} via Novita" in err
    message = run.commit_msg.read_text(encoding="utf-8")
    assert message.startswith("Refresh OpenRouter provider rates: 1 changed")
    assert f"{GLM} via Novita" in message


def test_a_run_refused_everywhere_writes_nothing(tmp_path, capsys):
    run = Run(tmp_path)
    _two_global_novitas(run)
    _refused(run, capsys, f"{GLM} via Novita")


# --- the data region -----------------------------------------------------------
# The account's keys reach only the global data region, so an endpoint
# whose tag names a region is not one it can be billed by.


@pytest.mark.parametrize("tag, region", [
    ("h/us/fp8", "us"),
    ("h/fp8/eu", "eu"),
    ("h/US", "us"),
    ("h/Us-East-1", "us-east-1"),
    ("h/us2", None),
    ("h/ai", None),
    ("h/xl", None),
    ("h/us-fp8", None),
    ("sail-research/us", "us"),
    ("baseten/us", "us"),
    ("provider/eu", "eu"),
    ("provider/us-east-1", "us-east-1"),
    ("provider/eu-west", "eu-west"),
    ("baseten/fp8", None),
    ("sail-research/fp4", None),
    ("modal/nvfp4", None),
    ("provider/bf16", None),
    ("provider/int4", None),
    ("relace", None),
    ("io", None),
    ("", None),
])
def test_the_region_a_tag_names(tag, region):
    # tag_region lives in refresh_prices; reach it through the script's own
    # import, the way every other helper is reached here.
    assert refresh.refresh_prices.tag_region(tag) == region


def _novita_region_twin(run: Run) -> dict:
    """A dearer copy of GLM's Novita endpoint tagged novita/us."""
    twin = copy.deepcopy(run.endpoint(GLM, "Novita"))
    twin["tag"] = "novita/us"
    twin["pricing"]["input_cache_read"] = "0.00000005"
    run.endpoints(GLM).append(twin)
    return twin


# --- a per-host tag override -------------------------------------------------


def _two_global_novitas(run: Run) -> None:
    run.endpoint(GLM, "Novita")["tag"] = "novita/fp8"
    twin = copy.deepcopy(run.endpoint(GLM, "Novita"))
    twin["tag"] = "novita/fp4"
    twin["pricing"]["input_cache_read"] = "0.00000005"
    run.endpoints(GLM).append(twin)


def _pin_novita(tag: str):
    return lambda doc: doc["openrouter"]["models"][GLM].update(
        {"resolve": {"Novita": {"tag": tag, "why": "fixture"}}})


# --- choosing the cheaper of identical twins ----------------------------------
# BaseTen lists deepseek-v4.1-flash twice with nothing but the price to tell
# the two apart; the file resolves it by price ORDER, which survives a price
# moving and breaks only when the order flips.


def _baseten_twins(run: Run) -> list[dict]:
    """[cheaper, dearer] by cache read."""
    twins = [e for e in run.endpoints(V41) if e["provider_name"] == "BaseTen"]
    return sorted(twins, key=lambda e: Decimal(e["pricing"]["input_cache_read"]))


# --- the detection time ------------------------------------------------------


# --- dry run -----------------------------------------------------------------
