"""Tests moved from test_provider_rate_refresh.py to keep test modules under 700 lines."""
from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from backend import pricing
from tests.refresh_fixture_builders import _per_token

from tests.test_provider_rate_refresh import (
    GLM,
    NOW,
    PARSER_JS,
    PRICING_JSON,
    Run,
    STAMP,
    UTC,
    V41,
    _baseten_twins,
    _endpoint,
    _move_openinference,
    _novita_region_twin,
    _pin_novita,
    _refused,
    _two_global_novitas,
    needs_node,
    _load,
)

refresh = _load()


def test_the_global_endpoint_is_taken_over_a_region_one(tmp_path, capsys):
    """The global price moving is a move even when a region twin exists."""
    run = Run(tmp_path)
    seeded = run.doc()["providers"][GLM]["Novita"][-1]
    _novita_region_twin(run)
    run.endpoint(GLM, "Novita")["pricing"]["completion"] = _per_token(seeded["output"] * 2)
    assert run(capsys)[0] == 0
    entry = run.doc()["providers"][GLM]["Novita"][-1]
    assert (entry["from"], entry["read"], entry["output"]) == (STAMP, seeded["read"], seeded["output"] * 2)


def test_a_region_endpoint_moving_alone_moves_nothing(tmp_path, capsys):
    run = Run(tmp_path)
    _novita_region_twin(run)["pricing"]["completion"] = "0.000002"
    before = run.snapshot()
    assert run(capsys)[0] == 0
    assert run.snapshot() == before


def test_sail_research_takes_its_global_endpoint(tmp_path, capsys):
    """Its deepseek-v4-flash-0731 is listed as sail-research/fp4 and
    sail-research/us at different prices; the global one applies."""
    run = Run(tmp_path)
    fp4 = {"fresh": 0.03, "create_5m": 0.03, "create_1h": 0.03,
           "read": 0.016, "output": 0.55}
    us_region = {"fresh": 0.038, "create_5m": 0.038, "create_1h": 0.038,
                 "read": 0.0228, "output": 0.55}
    model = "deepseek/deepseek-v4-flash-0731"
    run.endpoints(model)[:] = [
        e for e in run.endpoints(model) if e["provider_name"] != "Sail Research"] + [
        _endpoint("Sail Research", fp4, tag="sail-research/fp4"),
        _endpoint("Sail Research", us_region, tag="sail-research/us")]
    assert run(capsys)[0] == 0
    assert run.doc()["providers"][model]["Sail Research"][-1] == {"from": STAMP, **fp4}


def test_baseten_selects_the_global_synthetic_endpoint_over_a_cheaper_region() -> None:
    global_rates = {
        "fresh": 0.25, "create_5m": 0.25, "create_1h": 0.25,
        "read": 0.125, "output": 1.0,
    }
    regional_rates = {
        "fresh": 0.125, "create_5m": 0.125, "create_1h": 0.125,
        "read": 0.0625, "output": 0.5,
    }
    payload = {"data": {"endpoints": [
        _endpoint("BaseTen", global_rates, tag="baseten/fp8"),
        _endpoint("BaseTen", regional_rates, tag="baseten/us"),
    ]}}
    selected, refused, _ = refresh.listed_rows(
        "deepseek/acme-v4-1", payload, None,
        {"BaseTen": {"select": "cheapest", "why": "synthetic region check"}},
        {}, NOW,
    )

    assert refused == {}
    assert selected["BaseTen"].tag == "baseten/fp8"
    assert selected["BaseTen"].rates == global_rates


def test_modal_ignores_withdrawn_fp8_when_nvfp4_survives() -> None:
    withdrawn_rates = {
        "fresh": 0.45, "create_5m": 0.45, "create_1h": 0.45,
        "read": 0.225, "output": 1.5,
    }
    surviving_rates = {
        "fresh": 0.25, "create_5m": 0.25, "create_1h": 0.25,
        "read": 0.125, "output": 0.75,
    }
    payload = {"data": {"endpoints": [
        _endpoint("Modal", withdrawn_rates, tag="modal/fp8"),
        _endpoint("Modal", surviving_rates, tag="modal/nvfp4"),
    ]}}
    selected, refused, _ = refresh.listed_rows(
        GLM, payload, None,
        {"Modal": {"tag": "modal/nvfp4", "why": "synthetic survivor check"}},
        {}, NOW,
    )

    assert refused == {}
    assert selected["Modal"].tag == "modal/nvfp4"
    assert selected["Modal"].rates == surviving_rates


def test_a_host_listed_only_in_a_region_is_reported_vanished(tmp_path, capsys):
    run = Run(tmp_path)
    run.endpoint(GLM, "Cloudflare")["tag"] = "cloudflare/us"
    before = run.snapshot()
    rc, out, _ = run(capsys)
    assert rc == 0
    assert run.snapshot() == before
    assert re.search(r"vanished\b.*Cloudflare", out)


def test_a_model_with_no_endpoint_in_the_data_region_is_refused(tmp_path, capsys):
    run = Run(tmp_path)
    for endpoint in run.endpoints(GLM):
        endpoint["tag"] = endpoint["tag"].split("/")[0] + "/us"
    _refused(run, capsys, GLM)


def test_a_named_data_region_takes_that_region(tmp_path, capsys):
    run = Run(tmp_path)
    run.edit(lambda doc: doc["openrouter"].update({"data_region": "us"}))
    for model in run.doc()["providers"]:
        resolved = run.doc()["openrouter"]["models"][model].get("resolve", {})
        for endpoint in run.endpoints(model):
            if endpoint["provider_name"] in resolved:
                continue
            endpoint["tag"] = endpoint["tag"].split("/")[0] + "/us"
    global_novita = copy.deepcopy(run.endpoint(GLM, "Novita"))
    global_novita["tag"] = "novita"
    global_novita["pricing"]["input_cache_read"] = "0.00000009"
    run.endpoints(GLM).append(global_novita)
    run.endpoint(GLM, "Novita")["pricing"]["input_cache_read"] = "0.00000005"
    assert run(capsys)[0] == 0
    assert run.doc()["providers"][GLM]["Novita"][-1]["read"] == 0.05


@pytest.mark.parametrize("region", [None, "", "US", "the-us", 5])
def test_a_malformed_data_region_is_refused(tmp_path, capsys, region):
    run = Run(tmp_path)
    run.edit(lambda doc: doc["openrouter"].update({"data_region": region}))
    _refused(run, capsys, "data_region")


def test_a_tag_override_takes_its_endpoint(tmp_path, capsys):
    run = Run(tmp_path)
    _two_global_novitas(run)
    run.edit(_pin_novita("novita/fp4"))
    assert run(capsys)[0] == 0
    entry = run.doc()["providers"][GLM]["Novita"][-1]
    assert (entry["from"], entry["read"]) == (STAMP, 0.05)


def test_a_tag_override_takes_its_endpoint_whatever_its_region(tmp_path, capsys):
    run = Run(tmp_path)
    _novita_region_twin(run)
    run.edit(_pin_novita("novita/us"))
    assert run(capsys)[0] == 0
    assert run.doc()["providers"][GLM]["Novita"][-1]["read"] == 0.05


def test_a_tag_override_naming_no_listed_tag_is_refused(tmp_path, capsys):
    run = Run(tmp_path)
    _two_global_novitas(run)
    run.edit(_pin_novita("novita/int4"))
    _refused(run, capsys, "Novita", "novita/int4", "novita/fp8")


@pytest.mark.parametrize("pin", [
    pytest.param({"match": {"read": 0.0264}}, id="rate-only"),
    pytest.param({"tag": "novita", "match": {"read": 0.0264}}, id="tag-and-rate"),
])
def test_a_resolution_keyed_on_a_rate_is_refused(tmp_path, capsys, pin):
    """A pin on a price stops matching the moment that price moves, which
    turns every run red for exactly the hosts it was meant to settle."""
    run = Run(tmp_path)
    run.edit(lambda doc: doc["openrouter"]["models"][GLM].update(
        {"resolve": {"Novita": pin}}))
    _refused(run, capsys, "Novita", "keyed on 'tag'")


def test_baseten_is_resolved_by_price_order_as_data():
    doc = json.loads(PRICING_JSON.read_text(encoding="utf-8"))
    pin = doc["openrouter"]["models"][V41]["resolve"]["BaseTen"]
    assert pin["select"] == "cheapest" and pin["why"]
    assert set(pin) == {"tag", "select", "ignore", "why"}
    assert pin["tag"] == "baseten/fp8"
    assert pin["ignore"] == ["max_completion_tokens"]


def test_identical_twins_without_an_override_are_refused(tmp_path, capsys):
    run = Run(tmp_path)
    run.edit(lambda doc: doc["openrouter"]["models"][V41].pop("resolve"))
    _refused(run, capsys, f"{V41} via BaseTen", "baseten/fp8")


def test_a_moved_price_on_the_cheaper_twin_is_appended(tmp_path, capsys):
    run = Run(tmp_path)
    cheaper, _ = _baseten_twins(run)
    cheaper["pricing"]["input_cache_read"] = "0.000000008"
    cheaper["pricing"]["completion"] = "0.0000013"
    assert run(capsys)[0] == 0
    entry = run.doc()["providers"][V41]["BaseTen"][-1]
    assert (entry["from"], entry["read"], entry["output"]) == (STAMP, 0.008, 1.3)


def test_a_moved_price_on_the_dearer_twin_moves_nothing(tmp_path, capsys):
    run = Run(tmp_path)
    _, dearer = _baseten_twins(run)
    dearer["pricing"]["completion"] = "0.000002"
    before = run.snapshot()
    assert run(capsys)[0] == 0
    assert run.snapshot() == before


def test_a_flip_in_order_is_refused(tmp_path, capsys):
    """The twin whose price the row holds is no longer the cheaper one:
    which twin the account reaches is exactly what a human must check."""
    run = Run(tmp_path)
    _, dearer = _baseten_twins(run)
    dearer["pricing"]["input_cache_read"] = "0.000000005"
    _refused(run, capsys, f"{V41} via BaseTen", "order")


@pytest.mark.parametrize("field, value", [
    pytest.param("prompt", "0.0000002", id="fresh"),
    pytest.param("completion", "0.0000011", id="output"),
])
def test_cheapest_compares_read_then_fresh_then_output(tmp_path, capsys, field, value):
    """Equal cache reads fall to the input price, then the output price."""
    run = Run(tmp_path)
    cheaper, dearer = _baseten_twins(run)
    cheaper["pricing"]["input_cache_read"] = "0.000000009"
    dearer["pricing"]["input_cache_read"] = "0.000000009"
    cheaper["pricing"][field] = value
    assert run(capsys)[0] == 0
    entry = run.doc()["providers"][V41]["BaseTen"][-1]
    assert entry["from"] == STAMP
    assert entry["fresh" if field == "prompt" else "output"] == float(
        Decimal(value).scaleb(6))
    assert entry["read"] == 0.009


def test_cache_read_decides_before_the_input_price(tmp_path, capsys):
    run = Run(tmp_path)
    cheaper, _ = _baseten_twins(run)
    cheaper["pricing"]["input_cache_read"] = "0.000000008"
    cheaper["pricing"]["prompt"] = "0.0000005"
    assert run(capsys)[0] == 0
    entry = run.doc()["providers"][V41]["BaseTen"][-1]
    assert (entry["read"], entry["fresh"]) == (0.008, 0.5)


def test_a_tie_between_different_prices_is_refused(tmp_path, capsys):
    """Same cache read, input and output, different cache write: the order
    says nothing about which twin is which."""
    run = Run(tmp_path)
    cheaper, dearer = _baseten_twins(run)
    for field in ("prompt", "completion", "input_cache_read"):
        dearer["pricing"][field] = cheaper["pricing"][field]
    dearer["pricing"]["input_cache_write"] = "0.0000009"
    _refused(run, capsys, f"{V41} via BaseTen", "tie")


@pytest.mark.parametrize("field, value", [
    pytest.param("quantization", "fp4", id="quantization"),
    pytest.param("context_length", 65536, id="context-length"),
    pytest.param("max_completion_tokens", 1024, id="max-completion"),
])
def test_cheapest_applies_only_to_otherwise_identical_twins(
        tmp_path, capsys, field, value):
    """The recorded ignore covers only its named fields; under a pin with no
    recorded ignore every identity field is compared."""
    run = Run(tmp_path)
    run.edit(lambda doc: doc["openrouter"]["models"][V41].update(
        {"resolve": {"BaseTen": {"tag": "baseten/fp8", "select": "cheapest",
                                 "why": "synthetic twin check"}}}))
    cheaper, dearer = _baseten_twins(run)
    cheaper["max_completion_tokens"] = dearer["max_completion_tokens"]
    dearer[field] = value
    _refused(run, capsys, f"{V41} via BaseTen", "identical")


@pytest.mark.parametrize("pin", [
    pytest.param({"select": "dearest", "why": "x"}, id="unknown-select"),
    pytest.param({"select": "cheapest", "ignore": [], "why": "x"}, id="ignore-empty"),
    pytest.param({"select": "cheapest", "ignore": ["tag"], "why": "x"},
                 id="ignore-names-tag"),
    pytest.param({"select": "cheapest", "ignore": "max_completion_tokens", "why": "x"},
                 id="ignore-not-a-list"),
    pytest.param({"select": "cheapest", "ignore": ["nope"], "why": "x"},
                 id="ignore-unknown-field"),
    pytest.param({"tag": "baseten/fp8", "ignore": ["max_completion_tokens"], "why": "x"},
                 id="ignore-without-cheapest"),
    pytest.param({}, id="empty"),
    pytest.param({"why": "x"}, id="why-only"),
    pytest.param("cheapest", id="not-an-object"),
    pytest.param({"tag": 5, "why": "x"}, id="tag-not-a-string"),
])
def test_a_malformed_order_override_is_refused(tmp_path, capsys, pin):
    run = Run(tmp_path)
    run.edit(lambda doc: doc["openrouter"]["models"][V41].update(
        {"resolve": {"BaseTen": pin}}))
    _refused(run, capsys, f"{V41} via BaseTen", "a resolution is keyed on")


def test_a_tag_with_cheapest_and_a_recorded_ignore_resolves_a_sibling_tier(
        tmp_path, capsys):
    """Issue #355's live shape: the baseten/fp8 twins differ by a 1-token
    max_completion_tokens listing artifact and baseten/fast lists a sibling
    tier under its own tag. The recorded resolution narrows by tag, takes the
    cheaper twin, and the sibling's price never lands."""
    run = Run(tmp_path)
    cheaper, _ = _baseten_twins(run)
    fast = _endpoint("BaseTen", {"fresh": 0.6, "read": 0.001, "output": 2.4},
                     tag="baseten/fast")
    fast["quantization"] = "fp32"
    run.endpoints(V41).append(fast)
    cheaper["pricing"]["prompt"] = _per_token(0.28)
    rc, out, _ = run(capsys)
    assert rc == 0
    entry = run.doc()["providers"][V41]["BaseTen"][-1]
    assert entry == {"from": STAMP, "fresh": 0.28, "create_5m": 0.28,
                     "create_1h": 0.28, "read": 0.007, "output": 1.2}
    assert "possible twin switch" not in out


@pytest.mark.parametrize("field, value", [
    pytest.param("quantization", "fp4", id="quantization"),
    pytest.param("context_length", 65536, id="context-length"),
    pytest.param("max_prompt_tokens", 4096, id="max-prompt"),
])
def test_a_recorded_ignore_ignores_only_its_named_fields(
        tmp_path, capsys, field, value):
    """The live resolution ignores max_completion_tokens alone: any other
    identity field differing still refuses under it."""
    run = Run(tmp_path)
    _, dearer = _baseten_twins(run)
    dearer[field] = value
    _refused(run, capsys, f"{V41} via BaseTen", "identical")


def test_cheapest_without_a_recorded_ignore_refuses_differing_twins(
        tmp_path, capsys):
    """Without the recorded ignore the artifact itself refuses: the twins are
    not identical in tag, quantization and limits."""
    run = Run(tmp_path)
    run.edit(lambda doc: doc["openrouter"]["models"][V41].update(
        {"resolve": {"BaseTen": {"tag": "baseten/fp8", "select": "cheapest",
                                 "why": "synthetic twin check"}}}))
    _refused(run, capsys, f"{V41} via BaseTen", "identical")


@pytest.mark.parametrize("damage", [
    pytest.param(lambda p: p.update({"data": {}}), id="no-endpoints-key"),
    pytest.param(lambda p: p["data"].update({"endpoints": "none"}),
                 id="endpoints-not-a-list"),
    pytest.param(lambda p: p.clear(), id="no-data"),
    pytest.param(lambda p: p["data"]["endpoints"][0].pop("pricing"),
                 id="endpoint-without-pricing"),
    pytest.param(lambda p: p["data"]["endpoints"][0].pop("provider_name"),
                 id="endpoint-without-host"),
    pytest.param(lambda p: p["data"]["endpoints"][0].pop("tag"),
                 id="endpoint-without-tag"),
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
    damage(run.payloads[run.doc()["openrouter"]["models"][GLM]["id"]])
    _refused(run, capsys, GLM)


def test_no_endpoints_for_a_tracked_model_is_refused(tmp_path, capsys):
    """Indistinguishable from a broken fetch: never read as every host
    vanishing at once."""
    run = Run(tmp_path)
    run.endpoints(GLM).clear()
    _refused(run, capsys, GLM)


def test_a_detection_time_not_after_the_row_leaves_its_host_untouched(tmp_path, capsys):
    run = Run(tmp_path)
    _move_openinference(run)
    assert run(capsys)[0] == 0
    run.endpoint(GLM, "OpenInference")["pricing"]["prompt"] = "0.00000008"
    before = run.snapshot()
    rc, out, err = run(capsys, now=NOW - timedelta(days=1))
    assert rc == 0 and not err
    assert "not before the detection instant" in out
    assert run.snapshot() == before


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


def test_a_dry_run_reports_and_writes_nothing(tmp_path, capsys):
    run = Run(tmp_path)
    before = (run.snapshot(), run.doc()["providers"][GLM]["OpenInference"][-1]["fresh"])
    moved = _move_openinference(run)
    rc, out, _ = run(capsys, "--dry-run")
    assert rc == 0
    assert run.snapshot() == before[0]
    assert not run.commit_msg.exists()
    assert "OpenInference" in out and f"{before[1]!r} → {moved['fresh']!r}" in out
