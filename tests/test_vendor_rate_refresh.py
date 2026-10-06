"""SV-VENDOR-RATES: scripts/ci/refresh_vendor_rates.py, driven by synthetic
catalog and endpoint payloads — never the network, never the live rows
(SV-TEST-DATA): every model id, price and host here is synthetic."""
from __future__ import annotations

import copy
import importlib.util
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from backend import long_context, pricing, pricing_load

ROOT = Path(__file__).resolve().parents[1]
UTC = timezone.utc
NOW = datetime(2031, 1, 1, tzinfo=UTC)
STAMP = "2031-01-01T00:00:00Z"
RATE_FIELDS = ("fresh", "create_5m", "create_1h", "read", "output")
GPT_ID = "openai/gpt-test-9.9"
GPT_KEY = "gpt-test-9-9"
GLM_ID = "z-ai/glm-test-1"
GLM_KEY = "glm-test-1"
RATES = {"fresh": 1.0, "create_5m": 1.25, "create_1h": 2.0, "read": 0.1,
         "output": 5.0}


def _load(name: str):
    path = ROOT / "scripts" / "ci" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


vendor = _load("refresh_vendor_rates")


def _per_token(rate: float) -> str:
    return format(Decimal(repr(rate)).scaleb(-6).normalize(), "f")


def _doc(*, members=None, models=None, resolve=None) -> dict:
    return {
        "long_context_models": list(members or []),
        "models": copy.deepcopy(models) if models else {},
        "openrouter": {"data_region": "global", "models": {},
                       "vendor": {"resolve": resolve or {}}},
        "provider_rates_fetched": "2030-12-31T00:00:00Z",
        "providers": {},
    }


def _price(fresh, output, read=None, write=None, write_1h=None, **extra) -> dict:
    price = {"prompt": _per_token(fresh), "completion": _per_token(output)}
    if read is not None:
        price["input_cache_read"] = _per_token(read)
    if write is not None:
        price["input_cache_write"] = _per_token(write)
    if write_1h is not None:
        price["input_cache_write_1h"] = _per_token(write_1h)
    price.update(extra)
    return price


def _endpoint(tag: str, price: dict, host: str = "Vendor") -> dict:
    return {"provider_name": host, "tag": tag, "quantization": "fp8",
            "status": 0, "context_length": 131072, "pricing": price}


def _payload(*endpoints: dict) -> dict:
    return {"data": {"endpoints": list(endpoints)}}


def _catalog(*ids: str) -> dict:
    return {"data": [{"id": i} for i in ids]}


def _band(fresh: float, output: float, read=None, write=None, write_1h=None, *,
          threshold=None, input_mult=None, output_mult=None) -> dict:
    """A min_prompt_tokens override as the listing spells one, at the meter's
    shape unless a test departs from it. A tier the base prices, the band
    restates (an override without one prices that tier at its own fallback)."""
    tin = long_context.LONG_CONTEXT_INPUT_MULT if input_mult is None else input_mult
    tout = (long_context.LONG_CONTEXT_OUTPUT_MULT if output_mult is None
            else output_mult)
    band = {"min_prompt_tokens": (long_context.LONG_CONTEXT_THRESHOLD
                                  if threshold is None else threshold),
            "prompt": _per_token(fresh * tin),
            "completion": _per_token(output * tout)}
    if read is not None:
        band["input_cache_read"] = _per_token(read * tin)
    if write is not None:
        band["input_cache_write"] = _per_token(write * tin)
    if write_1h is not None:
        band["input_cache_write_1h"] = _per_token(write_1h * tin)
    return band


def _run(doc: dict, catalog: dict, endpoints: dict, *, stamp: str = STAMP):
    """One pass against synthetic fetchers; returns (mutated doc, outcome)."""
    doc = copy.deepcopy(doc)
    outcome = vendor.vendor_pass(doc, lambda: catalog,
                                 lambda mid: endpoints[mid], stamp, NOW)
    return doc, outcome


# --- selection ---------------------------------------------------------------


def test_new_model_gets_a_row():
    doc, out = _run(_doc(), _catalog(GPT_ID), {GPT_ID: _payload(
        _endpoint("openai", _price(1.0, 5.0, read=0.1, write=1.25, write_1h=2.0)))})
    assert not out.refusals and not out.notices
    entry = doc["models"][GPT_KEY][0]
    assert entry["from"] is None
    assert {f: entry[f] for f in RATE_FIELDS} == {
        "fresh": 1.0, "create_5m": 1.25, "create_1h": 2.0, "read": 0.1,
        "output": 5.0}
    assert len(out.moves) == 1 and out.moves[0].old is None


def test_bare_tag_beats_service_tiers():
    doc, out = _run(_doc(), _catalog(GPT_ID), {GPT_ID: _payload(
        _endpoint("openai", _price(1.0, 5.0)),
        _endpoint("openai/fast", _price(2.5, 12.5)),
        _endpoint("openai/flex", _price(0.5, 2.5)))})
    assert not out.refusals
    assert doc["models"][GPT_KEY][0]["fresh"] == 1.0


def test_equal_price_suffixed_endpoints_collapse():
    doc, out = _run(_doc(), _catalog(GLM_ID), {GLM_ID: _payload(
        _endpoint("z-ai/fp8", _price(0.2, 1.0, read=0.02)),
        _endpoint("z-ai/fp4", _price(0.2, 1.0, read=0.02)))})
    assert not out.refusals
    assert doc["models"][GLM_KEY][0]["fresh"] == 0.2


def test_third_party_hosts_alone_are_a_notice():
    doc, out = _run(_doc(), _catalog(GPT_ID), {GPT_ID: _payload(
        _endpoint("azure", _price(1.0, 5.0), host="Azure"))})
    assert out.moves == [] and not out.refusals
    assert "no first-party endpoint" in out.notices[0]
    assert GPT_KEY not in doc["models"]


def test_ambiguous_vendor_prices_refuse():
    doc, out = _run(_doc(), _catalog(GPT_ID), {GPT_ID: _payload(
        _endpoint("openai/mxfp4", _price(1.0, 5.0)),
        _endpoint("openai/int4", _price(2.0, 9.0)))})
    assert not out.moves and len(out.refusals) == 1
    assert "openrouter.vendor.resolve" in out.refusals[0]
    assert GPT_KEY not in doc["models"]


def test_resolve_pin_picks_one_price():
    doc, out = _run(
        _doc(resolve={GPT_KEY: {"tag": "openai/int4",
                                "why": "the int4 tier is the list price"}}),
        _catalog(GPT_ID),
        {GPT_ID: _payload(_endpoint("openai/mxfp4", _price(1.0, 5.0)),
                          _endpoint("openai/int4", _price(2.0, 9.0)))})
    assert not out.refusals
    assert doc["models"][GPT_KEY][0]["fresh"] == 2.0


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


# --- variants ----------------------------------------------------------------


def test_batch_variant_is_never_a_row():
    batch_id = GPT_ID + ":batch"
    doc, out = _run(_doc(), _catalog(GPT_ID, batch_id),
                    {GPT_ID: _payload(_endpoint("openai", _price(1.0, 5.0)))})
    assert not out.refusals
    assert GPT_KEY in doc["models"]
    assert "gpt-test-9-9:batch" not in doc["models"]
    assert [m.id for m in out.moves] == [GPT_ID]


def test_free_variant_is_never_a_row():
    free_id = GLM_ID + ":free"
    _, out = _run(
        _doc(), _catalog(GLM_ID, free_id),
        {GLM_ID: _payload(_endpoint("z-ai", _price(0.2, 1.0)))})
    assert not out.refusals
    assert [m.id for m in out.moves] == [GLM_ID]


# --- append rule -------------------------------------------------------------


def _stored(rates: dict, **entry) -> list[dict]:
    return [{**{"from": None, **rates}, **entry}]


def test_price_change_appends_and_never_rewrites():
    old = _stored(RATES)
    doc, out = _run(_doc(models={GPT_KEY: old}), _catalog(GPT_ID),
                    {GPT_ID: _payload(_endpoint(
                        "openai", _price(1.0, 6.0, read=0.1, write=1.25, write_1h=2.0)))})
    history = doc["models"][GPT_KEY]
    assert len(history) == 2
    assert history[0] == old[0], "the stored entry was rewritten"
    assert history[1]["from"] == STAMP and history[1]["output"] == 6.0
    assert out.moves[0].old["output"] == 5.0


def test_quiet_run_moves_nothing():
    listing = _price(1.0, 5.0, read=0.1, write=1.25, write_1h=2.0)
    _, out = _run(
        _doc(models={GPT_KEY: _stored(
            dict(zip(RATE_FIELDS, (1.0, 1.25, 2.0, 0.1, 5.0))))}),
        _catalog(GPT_ID), {GPT_ID: _payload(_endpoint("openai", listing))})
    assert out.moves == [] and not out.refusals


def test_hand_row_appended_when_the_listing_moves_on():
    doc, _out = _run(
        _doc(models={GLM_KEY: _stored(
            {"fresh": 0.15, "create_5m": 0.0, "create_1h": 0.0, "read": 0.03,
             "output": 0.5})}),
        _catalog(GLM_ID), {GLM_ID: _payload(_endpoint("z-ai", _price(0.15, 0.5, read=0.03)))})
    history = doc["models"][GLM_KEY]
    assert len(history) == 2
    assert history[1]["create_5m"] == 0.15


def test_append_after_the_detection_instant_is_a_notice():
    history = [*_stored(RATES), {**RATES, "from": STAMP}]
    _, out = _run(
        _doc(models={GPT_KEY: history}),
        _catalog(GPT_ID),
        {GPT_ID: _payload(_endpoint("openai", _price(1.0, 6.0)))}, stamp=STAMP)
    assert out.moves == [] and not out.refusals
    assert "not before the detection instant" in out.notices[0]


# --- the long-context band ---------------------------------------------------


def test_banded_model_joins_the_meter_with_its_row():
    doc, out = _run(_doc(), _catalog(GPT_ID), {GPT_ID: _payload(_endpoint(
        "openai", _price(1.0, 5.0, read=0.1, overrides=[_band(1.0, 5.0, read=0.1)])))})
    assert not out.refusals
    assert GPT_KEY in doc["long_context_models"]
    entry = doc["models"][GPT_KEY][0]
    assert entry["fresh"] == 1.0, "the band's own rates entered the row"
    assert out.moves[0].membership == "+"


def test_banded_model_with_a_current_row_moves_membership_only():
    _, out = _run(
        _doc(models={GPT_KEY: _stored(RATES)}),
        _catalog(GPT_ID),
        {GPT_ID: _payload(_endpoint(
            "openai", _price(1.0, 5.0, read=0.1, write=1.25, write_1h=2.0,
                             overrides=[_band(1.0, 5.0, read=0.1, write=1.25,
                                              write_1h=2.0)])))})
    assert not out.refusals
    assert len(out.moves) == 1 and out.moves[0].entries == 0
    assert out.moves[0].membership == "+"


def test_band_removal_leaves_the_meter():
    doc, out = _run(
        _doc(models={GPT_KEY: _stored(RATES)}, members=[GPT_KEY]),
        _catalog(GPT_ID), {GPT_ID: _payload(_endpoint("openai", _price(1.0, 5.0, read=0.1)))})
    assert out.moves[0].membership == "-"
    assert GPT_KEY not in doc["long_context_models"]


def test_non_vendor_member_stands():
    doc, _out = _run(
        _doc(members=["bonsai-test-1"],
             models={"bonsai-test-1": _stored(RATES)}),
        _catalog(GLM_ID),
        {GLM_ID: _payload(_endpoint("z-ai", _price(0.2, 1.0)))})
    assert "bonsai-test-1" in doc["long_context_models"]


def test_non_meter_threshold_is_a_notice():
    doc, out = _run(_doc(), _catalog(GPT_ID), {GPT_ID: _payload(_endpoint(
        "openai", _price(1.0, 5.0, read=0.1, overrides=[_band(1.0, 5.0, read=0.1,
                                                              threshold=200000)])))})
    assert GPT_KEY not in doc["models"] and GPT_KEY not in doc["long_context_models"]
    assert out.refusals == [] and len(out.notices) == 1
    assert "not tracked" in out.notices[0] and "departs from the meter" in out.notices[0]


def test_wrong_multipliers_are_a_notice():
    doc, out = _run(_doc(), _catalog(GPT_ID), {GPT_ID: _payload(_endpoint(
        "openai", _price(1.0, 5.0, overrides=[_band(1.0, 5.0, input_mult=3.0)])))})
    assert out.refusals == [] and len(out.notices) == 1
    assert "not tracked" in out.notices[0] and GPT_KEY not in doc["models"]


def test_band_without_output_is_a_notice():
    band = _band(1.0, 5.0)
    del band["completion"]
    _, out = _run(_doc(), _catalog(GPT_ID), {GPT_ID: _payload(_endpoint(
        "openai", _price(1.0, 5.0, overrides=[band])))})
    assert out.refusals == [] and len(out.notices) == 1
    assert ("not tracked" in out.notices[0]
            and "does not restate input and output" in out.notices[0])


def test_two_bands_are_a_notice():
    _, out = _run(_doc(), _catalog(GPT_ID), {GPT_ID: _payload(_endpoint(
        "openai", _price(1.0, 5.0, overrides=[_band(1.0, 5.0), _band(1.0, 5.0)])))})
    assert out.refusals == [] and len(out.notices) == 1
    assert "not tracked" in out.notices[0] and "2 long-context bands" in out.notices[0]


def test_band_with_utc_fields_are_a_notice():
    band = _band(1.0, 5.0)
    band["utc_days"] = ["monday"]
    _, out = _run(_doc(), _catalog(GPT_ID), {GPT_ID: _payload(_endpoint(
        "openai", _price(1.0, 5.0, overrides=[band])))})
    assert out.refusals == [] and len(out.notices) == 1
    assert "not tracked" in out.notices[0] and "not modelled" in out.notices[0]


# --- unmodelled shapes are notices, never red -------------------------------


def test_weekly_schedule_is_a_notice():
    doc, out = _run(_doc(), _catalog(GLM_ID), {GLM_ID: _payload(_endpoint(
        "z-ai", _price(0.2, 1.0, overrides=[{"utc_days": ["monday"], "utc_start": 0,
                                             "utc_end": 100,
                                             "prompt": _per_token(0.1),
                                             "completion": _per_token(0.5)}])))})
    assert out.refusals == [] and len(out.notices) == 1
    assert "not tracked" in out.notices[0] and "carries no schedule" in out.notices[0]
    assert GLM_KEY not in doc["models"]


def test_unmodelled_pricing_key_is_a_notice_and_zero_passes():
    doc, out = _run(_doc(), _catalog(GPT_ID), {GPT_ID: _payload(_endpoint(
        "openai", _price(1.0, 5.0, image_output="0.00004")))})
    assert out.refusals == [] and len(out.notices) == 1
    assert "not tracked" in out.notices[0] and "image_output" in out.notices[0]
    assert GPT_KEY not in doc["models"]
    doc, out = _run(_doc(), _catalog(GPT_ID), {GPT_ID: _payload(_endpoint(
        "openai", _price(1.0, 5.0, image_output="0")))})
    assert not out.refusals


def test_fee_becomes_a_provenance_note_never_a_priced_fee():
    doc, _out = _run(_doc(), _catalog(GPT_ID), {GPT_ID: _payload(_endpoint(
        "openai", _price(1.0, 5.0, web_search="0.01")))})
    note = doc["models"][GPT_KEY][0]["note"]
    assert note == ("web_search $0.01 per tool call on the listing: not a "
                    "per-request cost")
    assert "/request" not in note
    tables = pricing_load.load_tables(doc)
    assert not tables["FEES"].get(GPT_KEY)


def test_bad_fee_value_is_a_notice():
    doc, out = _run(_doc(), _catalog(GPT_ID), {GPT_ID: _payload(_endpoint(
        "openai", _price(1.0, 5.0, web_search="free")))})
    assert out.refusals == [] and len(out.notices) == 1
    assert "not tracked" in out.notices[0] and "web_search" in out.notices[0]
    assert GPT_KEY not in doc["models"]


def test_discount_note():
    doc, _out = _run(_doc(), _catalog(GLM_ID), {GLM_ID: _payload(_endpoint(
        "z-ai", _price(0.2, 1.0, discount=0.25)))})
    assert doc["models"][GLM_KEY][0]["note"] == "25% off"


# --- report input guards -----------------------------------------------------


def test_unreadable_catalog_skips_the_pass():
    _, out = _run(_doc(), {"data": {}}, {})
    assert out.moves == [] and not out.refusals
    assert "catalog is unreadable" in out.notices[0]


def test_broken_endpoints_payload_refuses():
    _, out = _run(_doc(), _catalog(GPT_ID), {GPT_ID: {"data": {}}})
    assert len(out.refusals) == 1 and "unrecognised" in out.refusals[0]


def test_the_would_be_file_is_loader_checked():
    doc = _doc()
    doc["providers"] = {"openai/gpt-test-9.9": {"Host": [_stored(RATES)[0]]}}
    doc["providers"]["openai/gpt-test-9.9"]["Host"][0]["fresh"] = -1
    with pytest.raises(vendor.RefreshError, match="would not load"):
        vendor.vendor_pass(doc, _catalog, lambda mid: {}, STAMP, NOW)


# --- integration through refresh_provider_rates.main ------------------------


def _main_doc(models: dict, members: list) -> dict:
    return {
        "long_context_models": members,
        "models": models,
        "openrouter": {"data_region": "global", "models": {},
                       "vendor": {"resolve": {}}},
        "provider_rates_fetched": "2030-12-31T00:00:00Z",
        "providers": {},
    }


class MainRun:
    """refresh_provider_rates.main() against synthetic files and fetchers."""

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
    listing = _price(1.0, 5.0, read=0.1, write=1.25, write_1h=2.0,
                     overrides=[_band(1.0, 5.0, read=0.1, write=1.25, write_1h=2.0)])
    run = MainRun(tmp_path, _main_doc({}, []), _catalog(GPT_ID),
                  {GPT_ID: _payload(_endpoint("openai", listing))})
    rc, out, err = run(capsys)
    assert rc == 0, err
    doc = json.loads(run.pricing_path.read_text(encoding="utf-8"))
    assert GPT_KEY in doc["models"] and GPT_KEY in doc["long_context_models"]
    assert run.constants_path.read_text() == 'PRICING_VERSION = "101"\n'
    assert "Vendor list rates" in out
    assert "vendor list rates: 1 new" in run.commit_msg.read_text(encoding="utf-8")


def test_main_vendor_refusal_is_red_but_writes_the_moves(tmp_path, capsys):
    run = MainRun(tmp_path, _main_doc({}, []), _catalog(GPT_ID),
                  {GPT_ID: _payload(_endpoint("openai/mxfp4", _price(1.0, 5.0)),
                                    _endpoint("openai/int4", _price(2.0, 9.0)))})
    rc, _out, err = run(capsys)
    assert rc == 1 and "2 prices" in err
    doc = json.loads(run.pricing_path.read_text(encoding="utf-8"))
    assert GPT_KEY not in doc["models"]


def test_main_untracked_shape_stays_green(tmp_path, capsys):
    run = MainRun(tmp_path, _main_doc({}, []), _catalog(GPT_ID),
                  {GPT_ID: _payload(_endpoint(
                      "openai", _price(1.0, 5.0, image_output="0.00004")))})
    rc, out, err = run(capsys)
    assert rc == 0, err
    assert "not tracked" in out
    doc = json.loads(run.pricing_path.read_text(encoding="utf-8"))
    assert GPT_KEY not in doc["models"]


def test_main_quiet_vendor_run_writes_nothing(tmp_path, capsys):
    listing = _price(1.0, 5.0, read=0.1, write=1.25, write_1h=2.0)
    models = {GPT_KEY: _stored(RATES)}
    run = MainRun(tmp_path, _main_doc(models, []), _catalog(GPT_ID),
                  {GPT_ID: _payload(_endpoint("openai", listing))})
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
    assert "Vendor list rates" not in out
    doc = json.loads(run.pricing_path.read_text(encoding="utf-8"))
    assert GPT_KEY not in doc["models"]
    assert run.constants_path.read_text() == 'PRICING_VERSION = "100"\n'


def test_every_vendor_resolve_pin_names_a_row_and_a_why():
    doc = json.loads((ROOT / "src" / "pricing.json").read_text(encoding="utf-8"))
    pins = doc["openrouter"].get("vendor", {}).get("resolve", {})
    for key, pin in pins.items():
        assert key in doc["models"], key
        assert isinstance(pin.get("tag"), str), key
        assert isinstance(pin.get("why"), str) and pin["why"], key


def test_derived_key_matches_resolver_normalisation():
    """The central identity claim, spanned: the key the refresh derives
    from a catalog id is the key resolve() matches for a transcript naming
    the bare first-party model."""
    for vendor_prefix, slug in (("openai", "gpt-test-9.9"),
                                ("z-ai", "GLM-Test-1.5"),
                                ("moonshotai", "kimi-test-2"),
                                ("anthropic", "Claude-Test-4.5")):
        assert vendor.derive_key(f"{vendor_prefix}/{slug}") == (
            pricing._normalise(slug))  # pylint: disable=protected-access


def test_notice_leaves_an_existing_row_and_member_untouched():
    """The not-tracked contract's second half: a model with a stored row
    and meter membership whose source turns untracked keeps both, byte
    for byte."""
    banded = _price(1.0, 5.0, read=0.1, write=1.25, write_1h=2.0,
                    overrides=[_band(1.0, 5.0, read=0.1, write=1.25,
                                     write_1h=2.0)])
    scheduled = _price(1.0, 5.0, read=0.1, write=1.25, write_1h=2.0,
                       overrides=[{"utc_days": ["monday"], "utc_start": 0,
                                   "utc_end": 100, "prompt": _per_token(1.0),
                                   "completion": _per_token(5.0)}])
    before, _ = _run(
        _doc(members=[], models={}),
        _catalog(GPT_ID), {GPT_ID: _payload(_endpoint("openai", banded))})
    assert GPT_KEY in before["models"] and GPT_KEY in before["long_context_models"]
    after, out = _run(
        before,
        _catalog(GPT_ID), {GPT_ID: _payload(_endpoint("openai", scheduled))})
    assert out.refusals == [] and len(out.notices) == 1
    assert "not tracked" in out.notices[0]
    assert after["models"][GPT_KEY] == before["models"][GPT_KEY]
    assert GPT_KEY in after["long_context_models"]
    assert out.moves == []
