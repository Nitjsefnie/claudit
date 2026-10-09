"""Vendor refresh tests with synthetic catalog and endpoint data only."""
from __future__ import annotations

import copy
import json

import pytest

from backend import long_context, pricing, pricing_load
from tests.vendor_rate_refresh_helpers import (
    GLM_ID, GLM_KEY, GPT_ID, GPT_KEY, NOW, RATE_FIELDS, RATES, ROOT, STAMP,
    TRACKED, _band, _catalog, _doc, _endpoint, _load, _meter_for, _meter_map,
    _move_meter, _payload, _per_token, _price, _run, vendor,
)


def test_a_new_model_joins_the_tracked_set():
    doc, out = _run(_doc(), _catalog(GPT_ID), {GPT_ID: _payload(
        _endpoint("openai", _price(1.0, 5.0, read=0.1, write=1.25, write_1h=2.0)))})
    assert not out.refusals and not out.notices
    assert doc["openrouter"]["models"][GPT_KEY] == TRACKED
    assert GPT_KEY not in doc["models"], "the vendor pass writes no rates"
    assert doc["providers"] == {}
    assert out.moves == [vendor.VendorMove(GPT_ID, GPT_KEY, added=True)]


def test_the_bare_tag_selects_its_own_endpoint_s_host():
    doc, out = _run(_doc(), _catalog(GPT_ID), {GPT_ID: _payload(
        _endpoint("openai", _price(1.0, 5.0), host="OpenAI"),
        _endpoint("openai/fast", _price(2.5, 12.5), host="OpenAI Fast"),
        _endpoint("openai/flex", _price(0.5, 2.5), host="OpenAI Flex"))})
    assert not out.refusals
    assert doc["openrouter"]["models"][GPT_KEY]["vendor_host"] == "OpenAI"


def test_equal_price_suffixed_endpoints_collapse():
    doc, out = _run(_doc(), _catalog(GLM_ID), {GLM_ID: _payload(
        _endpoint("z-ai/fp8", _price(0.2, 1.0, read=0.02)),
        _endpoint("z-ai/fp4", _price(0.2, 1.0, read=0.02)))})
    assert not out.refusals
    assert doc["openrouter"]["models"][GLM_KEY] == {"id": GLM_ID,
                                                    "vendor_host": "Vendor"}


def test_third_party_hosts_alone_are_a_notice():
    doc, out = _run(_doc(), _catalog(GPT_ID), {GPT_ID: _payload(
        _endpoint("azure", _price(1.0, 5.0), host="Azure"))})
    assert out.moves == [] and not out.refusals
    assert "no first-party endpoint" in out.notices[0]
    assert GPT_KEY not in doc["openrouter"]["models"]


def test_ambiguous_vendor_prices_refuse():
    doc, out = _run(_doc(), _catalog(GPT_ID), {GPT_ID: _payload(
        _endpoint("openai/mxfp4", _price(1.0, 5.0)),
        _endpoint("openai/int4", _price(2.0, 9.0)))})
    assert not out.moves and len(out.refusals) == 1
    assert "openrouter.vendor.resolve" in out.refusals[0]
    assert GPT_KEY not in doc["openrouter"]["models"]


def test_resolve_pin_picks_one_price():
    doc, out = _run(
        _doc(resolve={GPT_KEY: {"tag": "openai/int4",
                                "why": "the int4 tier is the list price"}}),
        _catalog(GPT_ID),
        {GPT_ID: _payload(_endpoint("openai/mxfp4", _price(1.0, 5.0),
                                    host="OpenAI Mxfp4"),
                          _endpoint("openai/int4", _price(2.0, 9.0),
                                    host="OpenAI"))})
    assert not out.refusals
    assert doc["openrouter"]["models"][GPT_KEY] == {"id": GPT_ID,
                                                    "vendor_host": "OpenAI"}


def test_stale_pin_refuses():
    _, out = _run(
        _doc(resolve={GPT_KEY: {"tag": "openai/gone", "why": "once was"}}),
        _catalog(GPT_ID), {GPT_ID: _payload(_endpoint("openai", _price(1.0, 5.0)))})
    assert len(out.refusals) == 1 and "stale" in out.refusals[0]


def test_malformed_pin_refuses():
    _, out = _run(
        _doc(resolve={GPT_KEY: {"select": "cheapest"}}),
        _catalog(GPT_ID), {GPT_ID: _payload(_endpoint("openai", _price(1.0, 5.0)))})
    assert len(out.refusals) == 1 and "pin" in out.refusals[0]


def test_a_variant_id_is_never_added():
    batch_id = GPT_ID + ":batch"
    doc, out = _run(_doc(), _catalog(GPT_ID, batch_id),
                    {GPT_ID: _payload(_endpoint("openai", _price(1.0, 5.0)))})
    assert not out.refusals
    assert doc["openrouter"]["models"] == {GPT_KEY: TRACKED}
    assert [m.id for m in out.moves] == [GPT_ID]


def test_only_exact_text_output_models_reach_vendor_selection() -> None:
    image_id = "openai/gpt-image-test"
    audio_id = "openai/gpt-audio-test"
    missing_architecture_id = "openai/gpt-missing-architecture-test"
    missing_output_id = "openai/gpt-missing-output-test"
    text_id = "openai/gpt-text-test"
    image_key = "gpt-image-test"
    text_key = "gpt-text-test"
    existing = {"id": image_id, "vendor_host": "Existing OpenAI"}
    doc = _doc(tracked={image_key: existing}, members=[image_key],
               meters={image_key: {"threshold": 200_000}})
    tracked_before = copy.deepcopy(doc["openrouter"]["models"])
    catalog = {"data": [
        {"id": image_id, "architecture": {
            "output_modalities": ["image", "text"]}},
        {"id": audio_id, "architecture": {
            "output_modalities": ["text", "audio"]}},
        {"id": missing_architecture_id},
        {"id": missing_output_id, "architecture": {
            "input_modalities": ["text"]}},
        {"id": text_id, "architecture": {
            "output_modalities": ["text"]}},
    ]}
    payloads = {
        image_id: _payload(_endpoint(
            "openai", _price(1.0, 5.0, image_output="0.00004"))),
        audio_id: _payload(_endpoint(
            "openai", _price(1.0, 5.0, audio_output="0.00004"))),
        missing_architecture_id: _payload(
            _endpoint("openai", _price(1.0, 5.0))),
        missing_output_id: _payload(_endpoint("openai", _price(1.0, 5.0))),
        text_id: _payload(_endpoint("openai", _price(1.0, 5.0))),
    }
    selected: list[str] = []

    def fetch_endpoints(model_id: str) -> dict:
        selected.append(model_id)
        return payloads[model_id]

    out = vendor.vendor_pass(doc, lambda: catalog, fetch_endpoints)

    assert selected == [text_id]
    assert not out.notices and not out.refusals
    assert out.moves == [vendor.VendorMove(text_id, text_key, added=True)]
    assert doc["openrouter"]["models"] == {
        **tracked_before,
        text_key: {"id": text_id, "vendor_host": "Vendor"},
    }
    assert doc["long_context_meters"] == [
        {"threshold": 200_000, "models": [image_key]}]


def test_the_prefix_list_is_config():
    """Catalog ids outside configured prefixes are ignored."""
    doc, out = _run(_doc(prefixes=["openai"]), _catalog(GLM_ID),
                    {GLM_ID: _payload(_endpoint("z-ai", _price(0.2, 1.0)))})
    assert out.moves == [] and not out.refusals and not out.notices
    assert doc["openrouter"]["models"] == {}


def test_a_tracked_entry_is_never_re_added():
    doc, out = _run(_doc(tracked={GPT_KEY: dict(TRACKED)}),
                    _catalog(GPT_ID),
                    {GPT_ID: _payload(_endpoint(
                        "openai", _price(1.0, 5.0, read=0.1, write=1.25,
                                         write_1h=2.0)))})
    assert not out.refusals and out.moves == []
    assert doc["openrouter"]["models"][GPT_KEY] == TRACKED


def test_a_delisted_tracked_id_is_untouched():
    """A delisted tracked key and its membership remain unchanged."""
    doc, out = _run(_doc(tracked={GPT_KEY: dict(TRACKED)}, members=[GPT_KEY]),
                    _catalog(GLM_ID),
                    {GLM_ID: _payload(_endpoint("z-ai", _price(0.2, 1.0)))})
    assert doc["openrouter"]["models"][GPT_KEY] == TRACKED
    assert GPT_KEY in _meter_map(doc)
    assert [m.id for m in out.moves] == [GLM_ID]


def test_entries_with_no_vendor_source_stand_byte_identical():
    """Non-vendor rows stand byte-identical while another vendor is added."""
    bonsai = [{"from": None, **dict(zip(RATE_FIELDS, (1.0, 1.25, 2.0, 0.1, 5.0)))}]
    deepseek = {"id": "deepseek/deepseek-v9-9", "resolve": {}}
    doc, _out = _run(
        _doc(models={"bonsai-2-27b": bonsai},
             tracked={"deepseek/deepseek-v9-9": deepseek}),
        _catalog(GPT_ID),
        {GPT_ID: _payload(_endpoint(
            "openai", _price(1.0, 5.0, read=0.1, write=1.25, write_1h=2.0)))})
    assert doc["openrouter"]["models"][GPT_KEY] == TRACKED
    assert doc["models"]["bonsai-2-27b"] == bonsai, "the models row was touched"
    assert (doc["openrouter"]["models"]["deepseek/deepseek-v9-9"]
            == deepseek), "the non-vendor tracked entry was touched"


def test_banded_model_joins_the_meter_with_its_entry():
    doc, out = _run(_doc(), _catalog(GPT_ID), {GPT_ID: _payload(_endpoint(
        "openai", _price(1.0, 5.0, read=0.1,
                         overrides=[_band(1.0, 5.0, read=0.1)])))})
    assert not out.refusals
    assert GPT_KEY in _meter_map(doc)
    assert doc["openrouter"]["models"][GPT_KEY] == TRACKED
    assert doc["long_context_meters"] == [{
        "threshold": long_context.LONG_CONTEXT_THRESHOLD,
        "models": [GPT_KEY]}]
    assert out.moves == [vendor.VendorMove(GPT_ID, GPT_KEY, added=True,
                                           membership="+",
                                           meter=_move_meter(
                                               long_context.LONG_CONTEXT_THRESHOLD))]


def test_banded_tracked_entry_folds_membership_and_meter():
    doc, out = _run(
        _doc(tracked={GPT_KEY: dict(TRACKED)}),
        _catalog(GPT_ID),
        {GPT_ID: _payload(_endpoint(
            "openai", _price(1.0, 5.0, read=0.1, write=1.25, write_1h=2.0,
                             overrides=[_band(1.0, 5.0, read=0.1, write=1.25,
                                              write_1h=2.0)])))})
    assert not out.refusals
    assert out.moves == [vendor.VendorMove(
        GPT_ID, GPT_KEY, membership="+",
        meter=_move_meter(long_context.LONG_CONTEXT_THRESHOLD))]
    assert doc["long_context_meters"] == [{
        "threshold": long_context.LONG_CONTEXT_THRESHOLD,
        "models": [GPT_KEY]}]


def test_band_removal_leaves_the_meter():
    doc, out = _run(
        _doc(tracked={GPT_KEY: dict(TRACKED)}, members=[GPT_KEY],
             meters={GPT_KEY: {"threshold": 200_000}}),
        _catalog(GPT_ID),
        {GPT_ID: _payload(_endpoint("openai", _price(1.0, 5.0, read=0.1)))})
    assert out.moves == [vendor.VendorMove(GPT_ID, GPT_KEY, membership="-")]
    assert _meter_map(doc) == {}
    assert doc["long_context_meters"] == []


def test_a_new_threshold_is_learned_from_the_band():
    """A band folds its threshold and factors."""
    doc, out = _run(
        _doc(tracked={GPT_KEY: dict(TRACKED)}),
        _catalog(GPT_ID),
        {GPT_ID: _payload(_endpoint(
            "openai", _price(1.0, 5.0, read=0.1, write=1.25, write_1h=2.0,
                             overrides=[_band(1.0, 5.0, read=0.1, write=1.25,
                                              write_1h=2.0,
                                              threshold=200_000)])))})
    assert not out.refusals and out.notices == []
    assert doc["long_context_meters"] == [
        {"threshold": 200_000, "models": [GPT_KEY]}]
    assert out.moves == [vendor.VendorMove(GPT_ID, GPT_KEY, membership="+",
                                           meter=_move_meter(200_000))]


def test_the_stored_meter_moves_with_the_band():
    """A threshold move rewrites the meter and keeps membership."""
    doc, out = _run(
        _doc(tracked={GPT_KEY: dict(TRACKED)}, members=[GPT_KEY],
             meters={GPT_KEY: {"threshold": 200_000}}),
        _catalog(GPT_ID),
        {GPT_ID: _payload(_endpoint(
            "openai", _price(1.0, 5.0, read=0.1, write=1.25, write_1h=2.0,
                             overrides=[_band(1.0, 5.0, read=0.1, write=1.25,
                                              write_1h=2.0,
                                              threshold=300_000)])))})
    assert not out.refusals and out.notices == []
    assert doc["long_context_meters"] == [
        {"threshold": 300_000, "models": [GPT_KEY]}]
    assert out.moves == [vendor.VendorMove(
        GPT_ID, GPT_KEY, meter=_move_meter(300_000))]


def test_a_quiet_band_writes_nothing():
    _, out = _run(
        _doc(tracked={GPT_KEY: dict(TRACKED)}, members=[GPT_KEY],
             meters={GPT_KEY: {"threshold": 272_000}}),
        _catalog(GPT_ID),
        {GPT_ID: _payload(_endpoint(
            "openai", _price(1.0, 5.0, read=0.1, write=1.25, write_1h=2.0,
                             overrides=[_band(1.0, 5.0, read=0.1, write=1.25,
                                              write_1h=2.0)])))})
    assert out.moves == [] and not out.refusals and out.notices == []


def test_non_vendor_member_stands():
    doc, _out = _run(
        _doc(members=["bonsai-test-1"],
             models={"bonsai-test-1": [{"from": None, **RATES}]}),
        _catalog(GLM_ID),
        {GLM_ID: _payload(_endpoint("z-ai", _price(0.2, 1.0)))})
    assert "bonsai-test-1" in _meter_map(doc)


@pytest.mark.parametrize("kw", [{"input_mult": 3.0}, {"output_mult": 1.25}])
def test_departing_global_multiplier_is_learned_per_model(kw):
    """A coherent band with non-global factors becomes a per-model meter."""
    doc, out = _run(_doc(), _catalog(GPT_ID), {GPT_ID: _payload(_endpoint(
        "openai", _price(1.0, 5.0, overrides=[_band(1.0, 5.0, **kw)])))})
    assert not out.refusals and not out.notices
    expected_factors = {
        field: value for field, value in kw.items()
        if value != (long_context.LONG_CONTEXT_INPUT_MULT
                     if field == "input_mult"
                     else long_context.LONG_CONTEXT_OUTPUT_MULT)}
    expected_model = {GPT_KEY: expected_factors} if expected_factors else GPT_KEY
    assert doc["long_context_meters"] == [{
        "threshold": long_context.LONG_CONTEXT_THRESHOLD,
        "models": [expected_model]}]
    assert GPT_KEY in doc["openrouter"]["models"]
    entry = _meter_for(doc, GPT_KEY)
    assert entry["threshold"] == long_context.LONG_CONTEXT_THRESHOLD
    if "input_mult" in kw:
        assert entry == {"threshold": long_context.LONG_CONTEXT_THRESHOLD,
                         "input_mult": kw["input_mult"]}
        assert out.moves[0].meter == _move_meter(
            long_context.LONG_CONTEXT_THRESHOLD, input_mult=kw["input_mult"])
    else:
        assert entry == {"threshold": long_context.LONG_CONTEXT_THRESHOLD,
                         "output_mult": kw["output_mult"]}
        assert out.moves[0].meter == _move_meter(
            long_context.LONG_CONTEXT_THRESHOLD, output_mult=kw["output_mult"])


def test_haiku_shaped_custom_meter_folds_in_one_run():
    """A synthetic 5x/5x band folds membership and meter together."""
    listing = _price(1.0, 5.0, read=0.1, write=1.25, write_1h=2.0,
                     overrides=[_band(1.0, 5.0, read=0.1, write=1.25,
                                      write_1h=2.0, threshold=100_000,
                                      input_mult=5.0, output_mult=5.0)])
    doc, out = _run(_doc(), _catalog(GPT_ID),
                    {GPT_ID: _payload(_endpoint("openai", listing))})
    assert not out.refusals and not out.notices
    assert doc["openrouter"]["models"][GPT_KEY] == TRACKED
    assert doc["long_context_meters"] == [{
        "threshold": 100_000,
        "models": [{GPT_KEY: {"input_mult": 5.0, "output_mult": 5.0}}]}]
    assert out.moves == [vendor.VendorMove(
        GPT_ID, GPT_KEY, added=True, membership="+",
        meter=_move_meter(100_000, input_mult=5.0, output_mult=5.0))]


def test_a_changed_band_factor_rewrites_the_stored_meter():
    doc, out = _run(
        _doc(tracked={GPT_KEY: dict(TRACKED)}, members=[GPT_KEY],
             meters={GPT_KEY: {"threshold": 100_000, "input_mult": 3.0,
                               "output_mult": 1.25}}),
        _catalog(GPT_ID),
        {GPT_ID: _payload(_endpoint(
            "openai", _price(1.0, 5.0, read=0.1,
                             overrides=[_band(1.0, 5.0, read=0.1,
                                              threshold=100_000,
                                              input_mult=5.0,
                                              output_mult=5.0)])))})
    assert not out.refusals and out.notices == []
    assert doc["long_context_meters"] == [{
        "threshold": 100_000,
        "models": [{GPT_KEY: {"input_mult": 5.0, "output_mult": 5.0}}]}]
    assert out.moves == [vendor.VendorMove(
        GPT_ID, GPT_KEY,
        meter=_move_meter(100_000, input_mult=5.0, output_mult=5.0))]


def test_a_bad_band_threshold_refuses():
    """An invalid threshold is human-actionable and turns the refresh red."""
    for bad in (0, -1, 2.0, True, "200000"):
        doc, out = _run(_doc(), _catalog(GPT_ID), {GPT_ID: _payload(_endpoint(
            "openai", _price(1.0, 5.0, read=0.1,
                             overrides=[_band(1.0, 5.0, threshold=bad)])))})
        assert GPT_KEY not in doc["openrouter"]["models"]
        assert _meter_map(doc) == {}
        assert doc["long_context_meters"] == []
        assert len(out.refusals) == 1 and not out.notices
        assert "positive integer" in out.refusals[0]


def test_band_without_output_refuses():
    band = _band(1.0, 5.0)
    del band["completion"]
    _, out = _run(_doc(), _catalog(GPT_ID), {GPT_ID: _payload(_endpoint(
        "openai", _price(1.0, 5.0, overrides=[band])))})
    assert len(out.refusals) == 1 and not out.notices
    assert "does not restate input and output" in out.refusals[0]


def test_two_bands_refuse():
    _, out = _run(_doc(), _catalog(GPT_ID), {GPT_ID: _payload(_endpoint(
        "openai", _price(1.0, 5.0, overrides=[_band(1.0, 5.0), _band(1.0, 5.0)])))})
    assert len(out.refusals) == 1 and not out.notices
    assert "2 long-context bands" in out.refusals[0]


def test_band_with_utc_fields_refuses():
    band = _band(1.0, 5.0)
    band["utc_days"] = ["monday"]
    _, out = _run(_doc(), _catalog(GPT_ID), {GPT_ID: _payload(_endpoint(
        "openai", _price(1.0, 5.0, overrides=[band])))})
    assert len(out.refusals) == 1 and not out.notices
    assert "not modelled" in out.refusals[0]


# --- unmodelled vendor shapes refuse (run red) --------------------------------


def test_weekly_schedule_refuses():
    doc, out = _run(_doc(), _catalog(GLM_ID), {GLM_ID: _payload(_endpoint(
        "z-ai", _price(0.2, 1.0, overrides=[{"utc_days": ["monday"], "utc_start": 0,
                                             "utc_end": 100,
                                             "prompt": _per_token(0.1),
                                             "completion": _per_token(0.5)}])))})
    assert len(out.refusals) == 1 and not out.notices
    assert "weekly schedule" in out.refusals[0]
    assert GLM_KEY not in doc["openrouter"]["models"]


def test_unmodelled_override_kind_refuses():
    doc, out = _run(_doc(), _catalog(GPT_ID), {GPT_ID: _payload(_endpoint(
        "openai", _price(1.0, 5.0,
                         overrides=[{"latency_ms": 100,
                                     "prompt": _per_token(1.0)}])))})
    assert len(out.refusals) == 1 and not out.notices
    assert "override kind not modelled" in out.refusals[0]
    assert GPT_KEY not in doc["openrouter"]["models"]


def test_unmodelled_pricing_key_refuses_and_zero_passes():
    doc, out = _run(_doc(), _catalog(GPT_ID), {GPT_ID: _payload(_endpoint(
        "openai", _price(1.0, 5.0, image_output="0.00004")))})
    assert len(out.refusals) == 1 and not out.notices
    assert "image_output" in out.refusals[0]
    assert GPT_KEY not in doc["openrouter"]["models"]
    doc, out = _run(_doc(), _catalog(GPT_ID), {GPT_ID: _payload(_endpoint(
        "openai", _price(1.0, 5.0, image_output="0")))})
    assert not out.refusals


def test_search_price_is_parsed_but_stored_on_the_provider_row():
    """The vendor pass recognizes the listing rate while the provider
    refresh owns its dated history; the tracked catalog row stays metadata."""
    endpoint = _endpoint("openai", _price(1.0, 5.0, web_search="0.0137"))
    rates, meter, host = vendor._listing(GPT_ID, endpoint)
    assert rates["web_search"] == 0.0137
    assert meter is None and host == "Vendor"

    doc, out = _run(_doc(), _catalog(GPT_ID), {GPT_ID: _payload(_endpoint(
        "openai", _price(1.0, 5.0, web_search="0.045")))})
    assert not out.refusals and not out.notices
    assert doc["openrouter"]["models"][GPT_KEY] == TRACKED


def test_bad_search_rate_refuses():
    doc, out = _run(_doc(), _catalog(GPT_ID), {GPT_ID: _payload(_endpoint(
        "openai", _price(1.0, 5.0, web_search="free")))})
    assert len(out.refusals) == 1 and not out.notices
    assert "web_search" in out.refusals[0]
    assert GPT_KEY not in doc["openrouter"]["models"]


def test_a_discounted_listing_still_auto_adds():
    doc, out = _run(_doc(), _catalog(GLM_ID), {GLM_ID: _payload(_endpoint(
        "z-ai", _price(0.2, 1.0, discount=0.25)))})
    assert not out.refusals and not out.notices
    assert doc["openrouter"]["models"][GLM_KEY] == {"id": GLM_ID,
                                                    "vendor_host": "Vendor"}


def test_an_endpoint_without_a_provider_name_refuses():
    """Auto-add refuses an endpoint with no provider name."""
    endpoint = _endpoint("openai", _price(1.0, 5.0))
    del endpoint["provider_name"]
    _, out = _run(_doc(), _catalog(GPT_ID), {GPT_ID: _payload(endpoint)})
    assert len(out.refusals) == 1 and "no provider" in out.refusals[0]


def test_unreadable_catalog_skips_the_pass():
    _, out = _run(_doc(), {"data": {}}, {})
    assert out.moves == [] and not out.refusals
    assert "catalog is unreadable" in out.notices[0]


def test_broken_endpoints_payload_refuses():
    _, out = _run(_doc(), _catalog(GPT_ID), {GPT_ID: {"data": {}}})
    assert len(out.refusals) == 1 and "unrecognised" in out.refusals[0]


def test_the_would_be_file_is_loader_checked():
    doc = _doc()
    doc["providers"] = {"openai/gpt-test-9.9": {"Host": [
        {"from": None, **RATES}]}}
    doc["providers"]["openai/gpt-test-9.9"]["Host"][0]["fresh"] = -1
    with pytest.raises(vendor.RefreshError, match="would not load"):
        vendor.vendor_pass(doc, _catalog, lambda mid: {})


def test_every_vendor_resolve_pin_names_a_row_and_a_why():
    """Every vendor pin names a reachable row and a reason."""
    doc = json.loads((ROOT / "src" / "pricing.json").read_text(encoding="utf-8"))
    pins = doc["openrouter"].get("vendor", {}).get("resolve", {})
    for key, pin in pins.items():
        assert key in doc["models"] \
            or key in doc["openrouter"]["models"], key
        assert isinstance(pin.get("tag"), str), key
        assert isinstance(pin.get("why"), str) and pin["why"], key


def test_derived_key_matches_resolver_normalisation():
    """The derived catalog key matches transcript normalization."""
    for vendor_prefix, slug in (("openai", "gpt-test-9.9"),
                                ("z-ai", "GLM-Test-1.5"),
                                ("moonshotai", "kimi-test-2"),
                                ("anthropic", "Claude-Test-4.5")):
        assert vendor.derive_key(f"{vendor_prefix}/{slug}") == (
            pricing._normalise(slug))  # pylint: disable=protected-access


def _main_doc(models: dict, members: list) -> dict:
    return _doc(models=models if models else None, members=members)


class MainRun:
    def __init__(self, tmp_path: Path, doc: dict, catalog: dict, endpoints: dict):
        self.provider_rates = _load("refresh_provider_rates")
        self.pricing_path = tmp_path / "pricing.json"
        self.constants_path = tmp_path / "constants.py"
        self.commit_msg = tmp_path / "commit-msg.txt"
        self.pricing_path.write_text(
            json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self.constants_path.write_text('PRICING_VERSION = "100"\n', encoding="utf-8")
        self.catalog = catalog
        self.endpoints = endpoints

    def __call__(self, capsys, *, run_vendor=True, dry_run=False):
        argv = ["--commit-msg", str(self.commit_msg)]
        if dry_run:
            argv.append("--dry-run")
        rc = self.provider_rates.main(
            argv,
            fetch=lambda mid: copy.deepcopy(self.endpoints[mid]),
            fetch_models=lambda: copy.deepcopy(self.catalog),
            fetch_log=lambda _slug: {"data": {"series": []}},
            now=NOW, pricing_path=self.pricing_path,
            constants_path=self.constants_path,
            vendor=vendor.vendor_pass if run_vendor else None)
        out, err = capsys.readouterr()
        return rc, out, err


def test_main_runs_the_vendor_pass(tmp_path, capsys):
    run = MainRun(tmp_path, _main_doc({}, []), _catalog(GPT_ID),
                  {GPT_ID: _payload(_endpoint(
                      "openai", _price(1.0, 5.0, read=0.1, write=1.25,
                                       write_1h=2.0)))})
    rc, out, err = run(capsys)
    assert rc == 0, err
    doc = json.loads(run.pricing_path.read_text(encoding="utf-8"))
    assert doc["openrouter"]["models"][GPT_KEY] == TRACKED
    assert GPT_KEY not in doc["models"], "the vendor pass writes no rates"
    assert run.constants_path.read_text() == 'PRICING_VERSION = "101"\n'
    assert "Vendor tracked table" in out
    assert "vendor table: 1 added" in run.commit_msg.read_text(encoding="utf-8")


def test_main_an_added_move_that_also_joins_the_meter_counts_once(
        tmp_path, capsys):
    """An auto-add and meter fold count once in the subject."""
    listing = _price(1.0, 5.0, read=0.1,
                     overrides=[_band(1.0, 5.0, read=0.1)])
    run = MainRun(tmp_path, _main_doc({}, []), _catalog(GPT_ID),
                  {GPT_ID: _payload(_endpoint("openai", listing))})
    rc, _out, err = run(capsys)
    assert rc == 0, err
    subject = run.commit_msg.read_text(encoding="utf-8").partition("\n\n")[0]
    assert subject.endswith("vendor table: 1 added")
    assert "metered" not in subject


def test_main_vendor_report_carries_custom_meter_factors(tmp_path, capsys):
    listing = _price(
        1.0, 5.0, read=0.1, write=1.25, write_1h=2.0,
        overrides=[_band(1.0, 5.0, read=0.1, write=1.25, write_1h=2.0,
                         threshold=100_000, input_mult=5.0,
                         output_mult=5.0)])
    run = MainRun(tmp_path, _main_doc({}, []), _catalog(GPT_ID),
                  {GPT_ID: _payload(_endpoint("openai", listing))})
    rc, out, err = run(capsys)
    assert rc == 0, err
    assert "[threshold 100000, input x5, output x5]" in out
    doc = json.loads(run.pricing_path.read_text(encoding="utf-8"))
    assert doc["long_context_meters"] == [{
        "threshold": 100_000,
        "models": [{GPT_KEY: {"input_mult": 5.0, "output_mult": 5.0}}]}]


def test_main_vendor_refusal_is_red_but_other_moves_writes(tmp_path, capsys):
    run = MainRun(tmp_path, _main_doc({}, []), _catalog(GPT_ID),
                  {GPT_ID: _payload(_endpoint("openai/mxfp4", _price(1.0, 5.0)),
                                    _endpoint("openai/int4", _price(2.0, 9.0)))})
    rc, _out, err = run(capsys)
    assert rc == 1 and "2 prices" in err
    doc = json.loads(run.pricing_path.read_text(encoding="utf-8"))
    assert GPT_KEY not in doc["openrouter"]["models"]


def test_main_untracked_shape_refuses_and_turns_run_red(tmp_path, capsys):
    run = MainRun(tmp_path, _main_doc({}, []), _catalog(GPT_ID),
                  {GPT_ID: _payload(_endpoint(
                      "openai", _price(1.0, 5.0, image_output="0.00004")))})
    rc, out, err = run(capsys)
    assert rc == 1
    assert "image_output" in err
    doc = json.loads(run.pricing_path.read_text(encoding="utf-8"))
    assert GPT_KEY not in doc["openrouter"]["models"]


def test_main_quiet_vendor_run_writes_nothing(tmp_path, capsys):
    """A quiet listing leaves the pricing document and version unchanged."""
    doc = _doc(tracked={GPT_KEY: dict(TRACKED)})
    doc["providers"][GPT_KEY] = {"Vendor": [
        {"from": None, "fresh": 1.0, "create_5m": 1.25, "create_1h": 2.0,
         "read": 0.1, "output": 5.0}]}
    run = MainRun(tmp_path, doc, _catalog(GPT_ID),
                  {GPT_ID: _payload(_endpoint(
                      "openai", _price(1.0, 5.0, read=0.1, write=1.25,
                                       write_1h=2.0)))})
    before = run.pricing_path.read_bytes()
    rc, _out, err = run(capsys)
    assert rc == 0, err
    assert run.pricing_path.read_bytes() == before
    assert run.constants_path.read_text() == 'PRICING_VERSION = "100"\n'
    assert not run.commit_msg.exists()


def test_main_vendor_disabled_keeps_the_provider_surface(tmp_path, capsys):
    run = MainRun(tmp_path, _main_doc({}, []), _catalog(GPT_ID),
                  {GPT_ID: _payload(_endpoint("openai", _price(1.0, 5.0)))})
    rc, out, err = run(capsys, run_vendor=False)
    assert rc == 0, err
    assert "Vendor tracked table" not in out
    doc = json.loads(run.pricing_path.read_text(encoding="utf-8"))
    assert GPT_KEY not in doc["openrouter"]["models"]
    assert run.constants_path.read_text() == 'PRICING_VERSION = "100"\n'


def test_a_broken_endpoints_fetch_refuses_only_its_model():
    """One endpoint fetch failure refuses only its model."""
    def fetch(mid):
        if mid == GPT_ID:
            raise KeyError(mid)
        return _payload(_endpoint("z-ai", _price(0.2, 1.0)))

    doc = _doc()
    outcome = vendor.vendor_pass(doc, lambda: _catalog(GPT_ID, GLM_ID), fetch)
    assert len(outcome.refusals) == 1 and GPT_ID in outcome.refusals[0]
    assert "fetching its endpoints failed" in outcome.refusals[0]
    assert doc["openrouter"]["models"][GLM_KEY] == {"id": GLM_ID,
                                                    "vendor_host": "Vendor"}


def test_a_pass_added_document_loads():
    """Auto-add and meter output pass both loaders before the provider row."""
    doc, _out = _run(_doc(), _catalog(GPT_ID), {GPT_ID: _payload(_endpoint(
        "openai", _price(1.0, 5.0, read=0.1,
                         overrides=[_band(1.0, 5.0, read=0.1)])))})
    tables = pricing_load.load_tables(doc)
    assert tables["VENDOR_HOSTS"][GPT_KEY] == "Vendor"
    assert GPT_KEY in tables["LONG_CONTEXT_MODELS"]


def test_a_membership_only_pass_loads():
    doc, _out = _run(_doc(tracked={GPT_KEY: dict(TRACKED)}),
                     _catalog(GPT_ID), {GPT_ID: _payload(_endpoint(
                         "openai", _price(1.0, 5.0, read=0.1,
                                          overrides=[_band(1.0, 5.0,
                                                           read=0.1)])))})
    assert pricing_load.load_tables(doc)["LONG_CONTEXT_MODELS"] == frozenset(
        {GPT_KEY})
