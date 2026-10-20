"""Vendor namespace migrations use synthetic pricing documents only."""
from __future__ import annotations

import copy
import json

import pytest

from backend import pricing
from backend.pricing_document import expand_pricing_doc, serialize_pricing_doc
from tests.vendor_rate_refresh_helpers import (
    NOW, RATES, _catalog, _doc, _endpoint, _load, _payload, _price, _run,
    seed_doc, vendor,
)

CATALOG_ID = "nvidia/nemotron-test-3.5-super-vl"
PREFIXED = "nvidia/nemotron-test-3-5-super-vl"
BARE = "nemotron-test-3-5-super-vl"


def _prefixed_doc():
    return seed_doc(
        prefixes=["nvidia"],
        fetched="2030-12-31T00:00:00Z",
        tracked={PREFIXED: {"id": CATALOG_ID, "vendor_host": "Vendor",
                            "context_length": 131072}},
        providers={PREFIXED: {"Vendor": [
            {"from": None, **RATES},
            {"from": "2030-06-01T00:00:00Z", **RATES, "web_search": 0.01}]}},
    )


@pytest.mark.parametrize("catalog", [_catalog(), {"data": {}}])
def test_rekey_preserves_whole_provider_row_even_without_catalog(catalog):
    doc = _prefixed_doc()
    tracked = doc["openrouter"]["models"][PREFIXED]
    history = doc["providers"][PREFIXED]
    previous = copy.deepcopy(history)
    out = vendor.vendor_pass(doc, lambda: catalog, pytest.fail)
    assert doc["openrouter"]["models"] == {BARE: tracked}
    assert doc["openrouter"]["models"][BARE] is tracked
    assert doc["providers"] == {BARE: previous}
    assert doc["providers"][BARE] is history
    assert len(out.moves) == 1 and not out.refusals
    assert out.renames == {PREFIXED: BARE}
    assert (out.moves[0].id, out.moves[0].key, out.moves[0].rekeyed) == (
        CATALOG_ID, BARE, PREFIXED)
    assert bool(out.notices) == (catalog == {"data": {}})


def test_untracked_namespaces_and_bare_keys_produce_no_move():
    keys = ["inclusionai/model-test", "stealth/model-test", BARE]
    doc = _doc(prefixes=["nvidia"], tracked={
        key: {"id": f"nvidia/{key}"} for key in keys})
    doc["providers"] = {key: {"Host": [{"from": None, **RATES}]} for key in keys}
    previous = copy.deepcopy(doc)
    out = vendor.vendor_pass(doc, _catalog, pytest.fail)
    assert doc == previous
    assert out.moves == [] and not out.refusals and not out.notices


def test_adding_a_namespace_rekeys_its_existing_entries():
    doc = _doc(tracked={"acme/model-test": {"id": "acme/model-test"}})
    doc["openrouter"]["vendor"]["prefixes"].append("acme")
    doc, out = _run(doc, _catalog(), {})
    assert doc["openrouter"]["models"] == {"model-test": {"id": "acme/model-test"}}
    assert len(out.moves) == 1 and not out.refusals


def test_first_party_listing_uses_the_rekeyed_entry_without_duplicate():
    doc, out = _run(_prefixed_doc(), _catalog(CATALOG_ID), {
        CATALOG_ID: _payload(_endpoint("nvidia", _price(1.0, 5.0)))})
    assert not out.refusals and not out.notices
    assert list(doc["openrouter"]["models"]) == [BARE]
    assert doc["openrouter"]["models"][BARE]["id"] == CATALOG_ID
    assert len(out.moves) == 1 and out.moves[0].rekeyed == PREFIXED
    assert not out.moves[0].added
    pricing.load_tables(doc)


@pytest.mark.parametrize("factors", [None, {"input_mult": 3.0, "output_mult": 1.5}])
def test_resolve_pin_and_meter_member_follow_the_rekey(factors):
    doc = _prefixed_doc()
    pin = {"tag": "nvidia/fp8", "why": "fixture endpoint"}
    doc["openrouter"]["vendor"]["resolve"] = {PREFIXED: pin}
    entry = PREFIXED if factors is None else {PREFIXED: factors}
    doc["long_context_meters"] = [{"threshold": 200_000, "models": [entry]}]
    previous = copy.deepcopy(doc)
    out = vendor.vendor_pass(doc, _catalog, pytest.fail)
    assert not out.refusals
    assert doc["openrouter"]["vendor"]["resolve"] == {
        BARE: previous["openrouter"]["vendor"]["resolve"][PREFIXED]}
    assert doc["openrouter"]["vendor"]["resolve"][BARE] is pin
    expected = (BARE if factors is None else
                {BARE: previous["long_context_meters"][0]["models"][0][PREFIXED]})
    assert doc["long_context_meters"] == [{"threshold": 200_000, "models": [expected]}]
    pricing.load_tables(doc)


@pytest.mark.parametrize("table", ["models", "providers", "resolve", "meters"])
def test_collision_refuses_both_names_before_any_mutation(table):
    doc = _prefixed_doc()
    if table == "models":
        doc["openrouter"]["models"][BARE] = {"id": CATALOG_ID}
    elif table == "providers":
        doc["providers"][BARE] = copy.deepcopy(doc["providers"][PREFIXED])
    elif table == "resolve":
        doc["openrouter"]["vendor"]["resolve"] = {
            key: {"tag": "nvidia", "why": "fixture"} for key in (PREFIXED, BARE)}
    else:
        doc["long_context_meters"] = [
            {"threshold": 200_000, "models": [PREFIXED]},
            {"threshold": 300_000, "models": [{BARE: {"input_mult": 3.0}}]}]
    previous = copy.deepcopy(doc)
    with pytest.raises(vendor.RefreshError) as exc:
        vendor.vendor_pass(doc, lambda: {"data": {}}, pytest.fail)
    assert repr(PREFIXED) in str(exc.value) and repr(BARE) in str(exc.value)
    assert doc == previous


def test_catalog_id_resolves_to_the_rekeyed_bare_key(monkeypatch):
    doc, out = _run(_prefixed_doc(), _catalog(), {})
    assert not out.refusals
    with monkeypatch.context() as patch:
        for name, value in pricing.load_tables(doc).items():
            patch.setattr(pricing, name, value)
        pricing._MATCH_KEY_CACHE.clear()  # pylint: disable=protected-access
        pricing._VENDOR_MATCH_CACHE.clear()  # pylint: disable=protected-access
        try:
            resolved = pricing.resolve(CATALOG_ID)
            assert (resolved.kind, resolved.key, resolved.rates) == (
                "exact", BARE, {**RATES, "web_search": 0.01})
        finally:
            pricing._MATCH_KEY_CACHE.clear()  # pylint: disable=protected-access
            pricing._VENDOR_MATCH_CACHE.clear()  # pylint: disable=protected-access


def test_main_bumps_version_when_rekey_is_the_only_move(tmp_path, capsys):
    refresh = _load("refresh_provider_rates")
    pricing_path = tmp_path / "pricing.json"
    constants_path = tmp_path / "constants.py"
    message_path = tmp_path / "commit-msg.txt"
    doc = _prefixed_doc()
    history = copy.deepcopy(doc["providers"][PREFIXED])
    pricing_path.write_text(serialize_pricing_doc(doc), encoding="utf-8")
    constants_path.write_text('PRICING_VERSION = "100"\n', encoding="utf-8")
    assert refresh.main(
        ["--commit-msg", str(message_path)],
        fetch=pytest.fail,
        fetch_models=lambda: _catalog("stealth/unrelated-test"),
        fetch_log=pytest.fail, now=NOW,
        pricing_path=pricing_path, constants_path=constants_path) == 0
    assert constants_path.read_text(encoding="utf-8") == 'PRICING_VERSION = "101"\n'
    written = expand_pricing_doc(json.loads(pricing_path.read_text(encoding="utf-8")))
    assert written["providers"] == {BARE: history}
    assert list(written["openrouter"]["models"]) == [BARE]
    output, error = capsys.readouterr()
    assert not error and "rekeyed" in output
    subject = message_path.read_text(encoding="utf-8").splitlines()[0]
    assert "vendor table: 1 rekeyed" in subject
    assert "updated" not in subject


def _overlapping_doc(reverse):
    keys = ["p/p/z", "p/z"]
    if reverse:
        keys.reverse()
    return seed_doc(
        prefixes=["p"],
        fetched="2030-12-31T00:00:00Z",
        tracked={key: {"id": f"p/id-{index}", "vendor_host": "Vendor"}
                 for index, key in enumerate(keys)},
        providers={key: {"Vendor": [{"from": None, **RATES, "fresh": index + 1.0},
                                    {"from": "2030-06-01T00:00:00Z", **RATES,
                                     "fresh": index + 3.0}]}
                   for index, key in enumerate(keys)})


@pytest.mark.parametrize("reverse", [False, True])
def test_overlapping_sources_refuse_without_mutating(reverse):
    doc = _overlapping_doc(reverse)
    pricing.load_tables(doc)
    previous = json.dumps(doc)
    with pytest.raises(vendor.RefreshError) as exc:
        vendor.vendor_pass(doc, _catalog, pytest.fail)
    assert "'p/p/z'" in str(exc.value) and "'p/z'" in str(exc.value)
    assert json.dumps(doc) == previous


@pytest.mark.parametrize("reverse", [False, True])
def test_main_overlapping_sources_write_nothing(reverse, tmp_path, capsys):
    refresh = _load("refresh_provider_rates")
    path = tmp_path / "pricing.json"
    constants = tmp_path / "constants.py"
    message = tmp_path / "commit-msg.txt"
    path.write_text(serialize_pricing_doc(_overlapping_doc(reverse)), encoding="utf-8")
    constants.write_text('PRICING_VERSION = "100"\n', encoding="utf-8")
    previous = {file.name: file.read_bytes() for file in tmp_path.iterdir()}
    assert refresh.main(
        ["--commit-msg", str(message)], pricing_path=path, constants_path=constants,
        now=NOW, fetch=pytest.fail, fetch_log=pytest.fail,
        fetch_models=lambda: _catalog("other/absent")) == 1
    assert {file.name: file.read_bytes() for file in tmp_path.iterdir()} == previous
    output, error = capsys.readouterr()
    assert not output and "'p/p/z'" in error and "'p/z'" in error


@pytest.fixture(name="malformed_doc", params=[
    ("openrouter", "missing"), ("openrouter.vendor", "missing"),
    ("openrouter.vendor.prefixes", "missing"),
    ("openrouter.vendor.prefixes", None), ("openrouter.vendor.prefixes", 5),
    ("openrouter.vendor.prefixes", "nvidia"),
    ("openrouter.vendor.prefixes", {"nvidia": True}),
    ("openrouter", []), ("openrouter.vendor", []),
    ("openrouter.vendor.prefixes", []),
    ("openrouter.vendor.prefixes", ["nvidia", "nvidia"]),
    ("openrouter.vendor.prefixes", ["NVIDIA"]),
    ("openrouter.vendor.prefixes", ["nvidia/nvidia"]),
    ("openrouter.vendor.prefixes", [""]),
    ("openrouter.vendor.prefixes", [None]),
])
def _malformed_doc(request):
    doc = _prefixed_doc()
    path, value = request.param
    parent = doc
    *parts, key = path.split(".")
    for part in parts:
        parent = parent[part]
    if value == "missing":
        del parent[key]
    else:
        parent[key] = value
    return doc, path


def test_malformed_vendor_shape_refuses_before_mutating(malformed_doc):
    doc, problem = malformed_doc
    previous = json.dumps(doc)
    with pytest.raises(vendor.RefreshError, match=problem):
        vendor.vendor_pass(doc, _catalog, pytest.fail)
    assert json.dumps(doc) == previous


def test_standalone_malformed_shape_returns_failure(
        malformed_doc, monkeypatch, tmp_path, capsys):
    doc, problem = malformed_doc
    path = tmp_path / "pricing.json"
    path.write_text(serialize_pricing_doc(doc), encoding="utf-8")
    previous = path.read_bytes()
    monkeypatch.setattr(vendor, "PRICING_JSON", path)
    monkeypatch.setattr(vendor.refresh_pricelog, "fetch_models", _catalog)
    assert vendor.main(["--dry-run"]) == 1
    output, error = capsys.readouterr()
    assert not output and problem in error and "Traceback" not in error
    assert path.read_bytes() == previous


@pytest.mark.parametrize("surface", ["stdout", "body", "subject", "sampled", "vanished"])
def test_rekey_reports_provider_moves_and_standing_state(surface, tmp_path, capsys):
    refresh = _load("refresh_provider_rates")
    path = tmp_path / "pricing.json"
    constants = tmp_path / "constants.py"
    message = tmp_path / "commit-msg.txt"
    doc = _prefixed_doc()
    doc["providers"][PREFIXED]["Gone"] = [{"from": None, **RATES}]
    previous = copy.deepcopy(doc["providers"][PREFIXED])
    path.write_text(serialize_pricing_doc(doc), encoding="utf-8")
    constants.write_text('PRICING_VERSION = "100"\n', encoding="utf-8")
    assert refresh.main(
        ["--commit-msg", str(message)], pricing_path=path, constants_path=constants,
        now=NOW, fetch_models=lambda: _catalog(CATALOG_ID), fetch_log=pytest.fail,
        fetch=lambda _mid: _payload(_endpoint(
            "nvidia", _price(3, 15, 0.3, 3.75, 6)))) == 0
    output, error = capsys.readouterr()
    assert not error
    body = message.read_text(encoding="utf-8")
    if surface in ("stdout", "body"):
        rendered = output if surface == "stdout" else body
        assert f"\n{BARE} ({CATALOG_ID})\n" in rendered
        assert "  changed   Vendor: fresh 1.0 → 3.0" in rendered
        assert f"\n{PREFIXED} ({CATALOG_ID})\n" not in rendered
    if surface == "subject":
        assert BARE in body.splitlines()[0] and PREFIXED not in body.splitlines()[0]
    state = json.loads((tmp_path / "pricing_notices.json").read_text(encoding="utf-8"))
    if surface == "sampled":
        assert set(state["sampled"]) == {BARE}
        assert set(state["sampled"][BARE]) == {"Vendor"}
    if surface == "vanished":
        assert state["vanished"] == {BARE: ["Gone"]}
    written = expand_pricing_doc(json.loads(path.read_text(encoding="utf-8")))
    assert written["providers"][BARE]["Gone"] == previous["Gone"]
    assert written["providers"][BARE]["Vendor"][:-1] == previous["Vendor"]
    assert written["providers"][BARE]["Vendor"][-1]["fresh"] == 3.0
