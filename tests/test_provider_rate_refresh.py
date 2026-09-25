"""SV-RATE-REFRESH: scripts/ci/refresh_provider_rates.py, driven by fixture
endpoint payloads — never the network.

The payloads are built from src/pricing.json itself, so "nothing moved"
is the committed data exactly, and each test moves one thing.
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

ROOT = Path(__file__).resolve().parents[1]
PRICING_JSON = ROOT / "src" / "pricing.json"
CONSTANTS_PY = ROOT / "backend" / "constants.py"
PARSER_JS = ROOT / "src" / "parser.js"
RATE_FIELDS = ("fresh", "create_5m", "create_1h", "read", "output")
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


def _per_token(rate: float) -> str:
    """OpenRouter's spelling: USD per token, as a decimal string."""
    return format(Decimal(repr(rate)).scaleb(-6).normalize(), "f")


def _endpoint(host: str, rates: dict, discount: float = 0, tag: str = "") -> dict:
    """One endpoint as the API returns it, extra fields included."""
    return {
        "name": f"{host} | fixture", "provider_name": host,
        "tag": tag or host.lower(), "quantization": "fp8", "status": 0,
        "context_length": 131072, "uptime_last_30m": 100,
        "pricing": {
            "prompt": _per_token(rates["fresh"]),
            "completion": _per_token(rates["output"]),
            "input_cache_read": _per_token(rates["read"]),
            "discount": discount,
        },
    }


def _discount(entry: dict) -> float:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)% off", entry.get("note", ""))
    return float(m.group(1)) / 100 if m else 0


def _payloads(doc: dict) -> dict:
    """Every tracked model's endpoints payload, reproducing the newest
    entry of every provider row. BaseTen's deepseek-v4.1-flash lists its
    two real endpoints, one the pinned resolution picks."""
    out = {}
    for key, hosts in doc["providers"].items():
        endpoints = [_endpoint(host, history[-1], _discount(history[-1]))
                     for host, history in hosts.items()]
        if key == V41:
            us_region = {**hosts["BaseTen"][-1], "read": 0.03}
            endpoints.append(_endpoint("BaseTen", us_region, tag="baseten/us"))
        model_id = doc["openrouter"][key]["id"]
        out[model_id] = {"data": {"id": model_id, "name": model_id,
                                  "endpoints": endpoints}}
    return out


class Run:
    """One refresh run against a private copy of the two tracked files."""

    def __init__(self, tmp_path: Path, parser_version: str | None = None):
        self.pricing = tmp_path / "pricing.json"
        self.constants = tmp_path / "constants.py"
        shutil.copy(PRICING_JSON, self.pricing)
        text = CONSTANTS_PY.read_text(encoding="utf-8")
        if parser_version is not None:
            text = re.sub(r'(?m)^PARSER_VERSION = "\d+"$',
                          f'PARSER_VERSION = "{parser_version}"', text)
        self.constants.write_text(text, encoding="utf-8")
        self.payloads = _payloads(self.doc())
        self.commit_msg = tmp_path / "commit-msg.txt"

    def doc(self) -> dict:
        return json.loads(self.pricing.read_text(encoding="utf-8"))

    def endpoints(self, key: str) -> list[dict]:
        return self.payloads[self.doc()["openrouter"][key]["id"]]["data"]["endpoints"]

    def endpoint(self, key: str, host: str) -> dict:
        return next(e for e in self.endpoints(key) if e["provider_name"] == host)

    def snapshot(self) -> tuple[bytes, bytes]:
        return self.pricing.read_bytes(), self.constants.read_bytes()

    def parser_version(self) -> int:
        m = re.search(r'(?m)^PARSER_VERSION = "(\d+)"$',
                      self.constants.read_text(encoding="utf-8"))
        assert m
        return int(m.group(1))

    def __call__(self, capsys, *args: str, now: datetime = NOW):
        payloads = copy.deepcopy(self.payloads)

        def fetch(model_id: str) -> object:
            return payloads[model_id]

        rc = refresh.main(["--commit-msg", str(self.commit_msg), *args],
                          fetch=fetch, now=now, pricing_path=self.pricing,
                          constants_path=self.constants)
        out, err = capsys.readouterr()
        return rc, out, err


# --- the tracked set ---------------------------------------------------------


def test_every_provider_table_model_names_its_openrouter_id():
    doc = json.loads(PRICING_JSON.read_text(encoding="utf-8"))
    assert set(doc["openrouter"]) == set(doc["providers"])
    for key, entry in doc["openrouter"].items():
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
    moved = {**run.doc()["providers"][GLM]["OpenInference"][-1], "fresh": 0.07,
             "create_5m": 0.07, "create_1h": 0.07, "read": 0.02, "output": 0.36}
    run.endpoints(GLM)[:] = [
        _endpoint("OpenInference", moved, discount)
        if e["provider_name"] == "OpenInference" else e
        for e in run.endpoints(GLM)]
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
    assert "OpenInference" in out and "0.1 → 0.07" in out


def test_a_run_that_appends_bumps_parser_version_relative_to_the_file(
        tmp_path, capsys):
    """Current + 1, whatever the file holds when the run starts, so a manual
    bump landing first is never collided with."""
    run = Run(tmp_path, parser_version="73")
    _move_openinference(run)
    assert run(capsys)[0] == 0
    assert run.parser_version() == 74
    text = run.constants.read_text(encoding="utf-8")
    assert re.search(r"(?m)^# 74 .*\n(?:#.*\n)*PARSER_VERSION = \"74\"$", text)


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
    assert pricing.rate_for(model, cut - timedelta(seconds=1), "OpenInference") == old
    assert pricing.rate_for(model, cut, "OpenInference") == moved


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
    twin["tag"] = "novita/us"
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


def test_a_host_at_two_prices_with_no_resolution_is_refused(tmp_path, capsys):
    run = Run(tmp_path)
    twin = copy.deepcopy(run.endpoint(GLM, "Novita"))
    twin["pricing"]["input_cache_read"] = "0.00000005"
    run.endpoints(GLM).append(twin)
    _refused(run, capsys, GLM, "Novita")


def test_every_refusal_in_a_run_is_named(tmp_path, capsys):
    """One red run names every model a human must look at, not just the
    first one fetched."""
    run = Run(tmp_path)
    twin = copy.deepcopy(run.endpoint(GLM, "Novita"))
    twin["pricing"]["input_cache_read"] = "0.00000005"
    run.endpoints(GLM).append(twin)
    run.endpoints(V41).clear()
    _refused(run, capsys, f"{GLM} via Novita", V41)


def test_a_pinned_resolution_that_no_longer_matches_is_refused(tmp_path, capsys):
    """BaseTen lists deepseek-v4.1-flash at two prices; the file pins the
    endpoint this account reaches. When no listed endpoint matches the pin,
    a human decides — the other price is never taken instead."""
    run = Run(tmp_path)
    for endpoint in run.endpoints(V41):
        if endpoint["provider_name"] == "BaseTen" and endpoint["tag"] != "baseten/us":
            endpoint["pricing"]["input_cache_read"] = "0.000000006"
    _refused(run, capsys, V41, "BaseTen")


def test_a_pinned_resolution_picks_its_endpoint(tmp_path, capsys):
    run = Run(tmp_path)
    for endpoint in run.endpoints(V41):
        if endpoint["provider_name"] == "BaseTen" and endpoint["tag"] != "baseten/us":
            endpoint["pricing"]["completion"] = "0.0000013"
    assert run(capsys)[0] == 0
    entry = run.doc()["providers"][V41]["BaseTen"][-1]
    assert (entry["from"], entry["read"], entry["output"]) == (STAMP, 0.007, 1.3)


@pytest.mark.parametrize("damage", [
    pytest.param(lambda p: p.update({"data": {}}), id="no-endpoints-key"),
    pytest.param(lambda p: p["data"].update({"endpoints": "none"}),
                 id="endpoints-not-a-list"),
    pytest.param(lambda p: p.clear(), id="no-data"),
    pytest.param(lambda p: p["data"]["endpoints"][0].pop("pricing"),
                 id="endpoint-without-pricing"),
    pytest.param(lambda p: p["data"]["endpoints"][0].pop("provider_name"),
                 id="endpoint-without-host"),
    pytest.param(lambda p: p["data"]["endpoints"][0]["pricing"].update(
        {"prompt": "cheap"}), id="non-numeric-price"),
    pytest.param(lambda p: p["data"]["endpoints"][0]["pricing"].update(
        {"prompt": "-1"}), id="negative-price"),
    pytest.param(lambda p: p["data"]["endpoints"][0]["pricing"].update(
        {"prompt": 0.0000001}), id="price-not-a-string"),
    pytest.param(lambda p: p["data"]["endpoints"][0]["pricing"].pop("completion"),
                 id="missing-output-price"),
    pytest.param(lambda p: p["data"]["endpoints"][0]["pricing"].update(
        {"discount": "half"}), id="non-numeric-discount"),
    pytest.param(lambda p: p["data"]["endpoints"][0]["pricing"].update(
        {"discount": 1.5}), id="discount-over-one"),
    pytest.param(lambda p: p["data"]["endpoints"][0]["pricing"].update(
        {"discount": -0.1}), id="negative-discount"),
    pytest.param(lambda p: p["data"]["endpoints"][0]["pricing"].update(
        {"discount": True}), id="boolean-discount"),
])
def test_an_unrecognised_response_shape_is_refused(tmp_path, capsys, damage):
    run = Run(tmp_path)
    damage(run.payloads[run.doc()["openrouter"][GLM]["id"]])
    _refused(run, capsys, GLM)


def test_no_endpoints_for_a_tracked_model_is_refused(tmp_path, capsys):
    """Indistinguishable from a broken fetch: never read as every host
    vanishing at once."""
    run = Run(tmp_path)
    run.endpoints(GLM).clear()
    _refused(run, capsys, GLM)


def test_a_detection_time_not_after_the_row_is_refused(tmp_path, capsys):
    run = Run(tmp_path)
    _move_openinference(run)
    assert run(capsys)[0] == 0
    run.endpoint(GLM, "OpenInference")["pricing"]["prompt"] = "0.00000008"
    before = run.snapshot()
    rc, _, err = run(capsys, now=NOW - timedelta(days=1))
    assert rc != 0 and "OpenInference" in err
    assert run.snapshot() == before


# --- the detection time ------------------------------------------------------


def test_the_detection_time_is_whole_seconds_utc_both_loaders_accept():
    local = timezone(timedelta(hours=2))
    stamp = refresh.detection_stamp(
        datetime(2031, 1, 1, 1, 2, 3, 456789, tzinfo=local))
    assert stamp == "2030-12-31T23:02:03Z"
    assert pricing._INSTANT.fullmatch(stamp)  # pylint: disable=protected-access


@needs_node
def test_a_file_written_at_any_clock_reading_loads_on_both_sides(tmp_path, capsys):
    """A mid-second, non-UTC clock: the stamp written is still the one
    spelling both loaders accept, and both read it as the same instant."""
    run = Run(tmp_path)
    _move_openinference(run)
    clock = datetime(2031, 1, 1, 1, 2, 3, 456789, tzinfo=timezone(timedelta(hours=2)))
    assert run(capsys, now=clock)[0] == 0
    doc = run.doc()
    assert doc["providers"][GLM]["OpenInference"][-1]["from"] == "2030-12-31T23:02:03Z"
    epochs = pricing.load_tables(doc)["RATE_EPOCHS"]
    want = int(datetime(2030, 12, 31, 23, 2, 3, tzinfo=UTC).timestamp() * 1000)
    assert want in [int(e.timestamp() * 1000) for e in epochs]
    js = tmp_path / "js"
    js.mkdir()
    shutil.copy(run.pricing, js / "pricing.json")
    shutil.copy(PARSER_JS, js / "parser.js")
    proc = subprocess.run(["node", "-e", f"""
      global.window = {{}};
      require({str(js / "parser.js")!r});
      console.log(JSON.stringify(window.rateEpochs));
    """], capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    assert want in json.loads(proc.stdout)


# --- dry run -----------------------------------------------------------------


def test_a_dry_run_reports_and_writes_nothing(tmp_path, capsys):
    run = Run(tmp_path)
    _move_openinference(run)
    before = run.snapshot()
    rc, out, _ = run(capsys, "--dry-run")
    assert rc == 0
    assert run.snapshot() == before
    assert not run.commit_msg.exists()
    assert "OpenInference" in out and "0.1 → 0.07" in out
