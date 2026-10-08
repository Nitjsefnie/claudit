"""SV-VENDOR-RATES: scripts/ci/refresh_vendor_rates.py, driven by synthetic
catalog and endpoint payloads — never the network, never the live rows
(SV-TEST-DATA): every model id, price and host here is synthetic.

The vendor pass owns the tracked table's vendor membership: a catalog id
under a configured prefix whose derived key is not yet tracked is ADDED to
openrouter.models as {"id", "vendor_host"} — no rates; the provider pass
carries the row from the next hourly run — and every listed vendor id's
first-party listing folds its long-context meter membership. The pass
writes no rates anywhere.
"""
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
from tests.refresh_fixture_builders import seed_doc

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
TRACKED = {"id": GPT_ID, "vendor_host": "Vendor"}


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


def _doc(*, members=None, models=None, resolve=None, tracked=None,
         meters=None, prefixes=None) -> dict:
    """The minimal seed doc (refresh_fixture_builders.seed_doc) with this
    file's frozen fetch stamp and the per-model meter map (#765)."""
    return seed_doc(members=members, models=models, resolve=resolve,
                    tracked=tracked, meters=meters, prefixes=prefixes,
                    fetched="2030-12-31T00:00:00Z")


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


def _run(doc: dict, catalog: dict, endpoints: dict):
    """One pass against synthetic fetchers; returns (mutated doc, outcome)."""
    doc = copy.deepcopy(doc)
    outcome = vendor.vendor_pass(doc, lambda: catalog,
                                 lambda mid: endpoints[mid])
    return doc, outcome


# --- the auto-add path --------------------------------------------------------


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


# --- variants and the configured prefixes -------------------------------------


def test_a_variant_id_is_never_added():
    batch_id = GPT_ID + ":batch"
    doc, out = _run(_doc(), _catalog(GPT_ID, batch_id),
                    {GPT_ID: _payload(_endpoint("openai", _price(1.0, 5.0)))})
    assert not out.refusals
    assert doc["openrouter"]["models"] == {GPT_KEY: TRACKED}
    assert [m.id for m in out.moves] == [GPT_ID]


def test_the_prefix_list_is_config():
    """Adding or dropping a vendor is a one-line pricing.json edit: a catalog
    id under an unlisted prefix is never visited, fetched or added."""
    doc, out = _run(_doc(prefixes=["openai"]), _catalog(GLM_ID),
                    {GLM_ID: _payload(_endpoint("z-ai", _price(0.2, 1.0)))})
    assert out.moves == [] and not out.refusals and not out.notices
    assert doc["openrouter"]["models"] == {}


# --- a tracked entry is never re-added or rewritten ---------------------------


def test_a_tracked_entry_is_never_re_added():
    doc, out = _run(_doc(tracked={GPT_KEY: dict(TRACKED)}),
                    _catalog(GPT_ID),
                    {GPT_ID: _payload(_endpoint(
                        "openai", _price(1.0, 5.0, read=0.1, write=1.25,
                                         write_1h=2.0)))})
    assert not out.refusals and out.moves == []
    assert doc["openrouter"]["models"][GPT_KEY] == TRACKED


def test_a_delisted_tracked_id_is_untouched():
    """A tracked key whose catalog id vanished is not visited: the entry and
    its membership stand while the listed untracked id still joins."""
    doc, out = _run(_doc(tracked={GPT_KEY: dict(TRACKED)}, members=[GPT_KEY]),
                    _catalog(GLM_ID),
                    {GLM_ID: _payload(_endpoint("z-ai", _price(0.2, 1.0)))})
    assert doc["openrouter"]["models"][GPT_KEY] == TRACKED
    assert GPT_KEY in doc["long_context_models"]
    assert [m.id for m in out.moves] == [GLM_ID]


def test_entries_with_no_vendor_source_stand_byte_identical():
    """Issue #818's rule on the new code: a models-table row with no vendor
    source (bonsai-2-27b live) and a tracked non-vendor entry stand byte for
    byte while the pass adds a vendor entry elsewhere."""
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


# --- the membership fold -------------------------------------------------------


def test_banded_model_joins_the_meter_with_its_entry():
    doc, out = _run(_doc(), _catalog(GPT_ID), {GPT_ID: _payload(_endpoint(
        "openai", _price(1.0, 5.0, read=0.1,
                         overrides=[_band(1.0, 5.0, read=0.1)])))})
    assert not out.refusals
    assert GPT_KEY in doc["long_context_models"]
    assert doc["openrouter"]["models"][GPT_KEY] == TRACKED
    assert doc["long_context_meters"][GPT_KEY] == {
        "threshold": long_context.LONG_CONTEXT_THRESHOLD}
    assert out.moves == [vendor.VendorMove(GPT_ID, GPT_KEY, added=True,
                                           membership="+",
                                           meter=long_context.LONG_CONTEXT_THRESHOLD)]


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
        meter=long_context.LONG_CONTEXT_THRESHOLD)]
    assert doc["long_context_meters"][GPT_KEY] == {
        "threshold": long_context.LONG_CONTEXT_THRESHOLD}


def test_band_removal_leaves_the_meter():
    doc, out = _run(
        _doc(tracked={GPT_KEY: dict(TRACKED)}, members=[GPT_KEY],
             meters={GPT_KEY: {"threshold": 200_000}}),
        _catalog(GPT_ID),
        {GPT_ID: _payload(_endpoint("openai", _price(1.0, 5.0, read=0.1)))})
    assert out.moves == [vendor.VendorMove(GPT_ID, GPT_KEY, membership="-")]
    assert GPT_KEY not in doc["long_context_models"]
    assert GPT_KEY not in doc["long_context_meters"]


def test_a_new_threshold_is_learned_from_the_band():
    """The issue #765 case: a band at a threshold of its own (Claude's
    200k) with the meter's multipliers is the meter — the pass folds the
    membership and learns the threshold into long_context_meters."""
    doc, out = _run(
        _doc(tracked={GPT_KEY: dict(TRACKED)}),
        _catalog(GPT_ID),
        {GPT_ID: _payload(_endpoint(
            "openai", _price(1.0, 5.0, read=0.1, write=1.25, write_1h=2.0,
                             overrides=[_band(1.0, 5.0, read=0.1, write=1.25,
                                              write_1h=2.0,
                                              threshold=200_000)])))})
    assert not out.refusals and out.notices == []
    assert GPT_KEY in doc["long_context_models"]
    assert doc["long_context_meters"][GPT_KEY] == {"threshold": 200_000}
    assert out.moves == [vendor.VendorMove(GPT_ID, GPT_KEY, membership="+",
                                           meter=200_000)]


def test_the_stored_meter_moves_with_the_band():
    """A listed band at a threshold other than the stored meter's is a
    move the listing governs: the meter rewrites, membership stays."""
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
    assert doc["long_context_meters"][GPT_KEY] == {"threshold": 300_000}
    assert GPT_KEY in doc["long_context_models"]
    assert out.moves == [vendor.VendorMove(GPT_ID, GPT_KEY, meter=300_000)]


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
    assert "bonsai-test-1" in doc["long_context_models"]


@pytest.mark.parametrize("kw", [{"input_mult": 3.0}, {"output_mult": 1.25}])
def test_departing_multiplier_band_is_a_notice(kw):
    """Each multiplier clause of the meter-shape check kills on its own: a
    band departing on the input side or on output is a notice — no entry, no
    membership."""
    doc, out = _run(_doc(), _catalog(GPT_ID), {GPT_ID: _payload(_endpoint(
        "openai", _price(1.0, 5.0, overrides=[_band(1.0, 5.0, **kw)])))})
    assert out.refusals == [] and len(out.notices) == 1
    assert "not tracked" in out.notices[0]
    assert GPT_KEY not in doc["openrouter"]["models"]
    assert GPT_KEY not in doc["long_context_models"]


def test_a_bad_band_threshold_is_a_notice():
    """A band threshold that is no positive integer is not a meter shape
    the table can carry — a notice, never red, never a fold."""
    for bad in (0, -1, 2.0, True, "200000"):
        doc, out = _run(_doc(), _catalog(GPT_ID), {GPT_ID: _payload(_endpoint(
            "openai", _price(1.0, 5.0, read=0.1,
                             overrides=[_band(1.0, 5.0, threshold=bad)])))})
        assert GPT_KEY not in doc["openrouter"]["models"]
        assert GPT_KEY not in doc["long_context_models"]
        assert GPT_KEY not in doc["long_context_meters"]
        assert out.refusals == [] and len(out.notices) == 1
        assert ("not tracked" in out.notices[0]
                and "positive integer" in out.notices[0])


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


# --- unmodelled shapes are notices, never red ---------------------------------


def test_weekly_schedule_is_a_notice():
    doc, out = _run(_doc(), _catalog(GLM_ID), {GLM_ID: _payload(_endpoint(
        "z-ai", _price(0.2, 1.0, overrides=[{"utc_days": ["monday"], "utc_start": 0,
                                             "utc_end": 100,
                                             "prompt": _per_token(0.1),
                                             "completion": _per_token(0.5)}])))})
    assert out.refusals == [] and len(out.notices) == 1
    assert "not tracked" in out.notices[0] and "weekly schedule" in out.notices[0]
    assert GLM_KEY not in doc["openrouter"]["models"]


def test_unmodelled_pricing_key_is_a_notice_and_zero_passes():
    doc, out = _run(_doc(), _catalog(GPT_ID), {GPT_ID: _payload(_endpoint(
        "openai", _price(1.0, 5.0, image_output="0.00004")))})
    assert out.refusals == [] and len(out.notices) == 1
    assert "not tracked" in out.notices[0] and "image_output" in out.notices[0]
    assert GPT_KEY not in doc["openrouter"]["models"]
    doc, out = _run(_doc(), _catalog(GPT_ID), {GPT_ID: _payload(_endpoint(
        "openai", _price(1.0, 5.0, image_output="0")))})
    assert not out.refusals


def test_a_recorded_fee_changes_nothing_here():
    """A RECORDED fee is the provider row's provenance, not the tracked
    entry's: the pass auto-adds silently, and the fee note lands on the
    (key, host) row the provider pass writes."""
    doc, out = _run(_doc(), _catalog(GPT_ID), {GPT_ID: _payload(_endpoint(
        "openai", _price(1.0, 5.0, web_search="0.01")))})
    assert not out.refusals and not out.notices
    assert doc["openrouter"]["models"][GPT_KEY] == TRACKED


def test_bad_fee_value_is_a_notice():
    doc, out = _run(_doc(), _catalog(GPT_ID), {GPT_ID: _payload(_endpoint(
        "openai", _price(1.0, 5.0, web_search="free")))})
    assert out.refusals == [] and len(out.notices) == 1
    assert "not tracked" in out.notices[0] and "web_search" in out.notices[0]
    assert GPT_KEY not in doc["openrouter"]["models"]


def test_a_discounted_listing_still_auto_adds():
    doc, out = _run(_doc(), _catalog(GLM_ID), {GLM_ID: _payload(_endpoint(
        "z-ai", _price(0.2, 1.0, discount=0.25)))})
    assert not out.refusals and not out.notices
    assert doc["openrouter"]["models"][GLM_KEY] == {"id": GLM_ID,
                                                    "vendor_host": "Vendor"}


def test_an_endpoint_without_a_provider_name_refuses():
    """The auto-add names the host from the selected endpoint's
    provider_name: a payload without one is an unrecognised shape."""
    endpoint = _endpoint("openai", _price(1.0, 5.0))
    del endpoint["provider_name"]
    _, out = _run(_doc(), _catalog(GPT_ID), {GPT_ID: _payload(endpoint)})
    assert len(out.refusals) == 1 and "no provider" in out.refusals[0]


# --- report input guards -------------------------------------------------------


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
    """A pin's subject is a tracked key the pass selects on (post-migration
    there is no models-table row to pin); a pin naming neither is dead data
    a refresh cannot reach."""
    doc = json.loads((ROOT / "src" / "pricing.json").read_text(encoding="utf-8"))
    pins = doc["openrouter"].get("vendor", {}).get("resolve", {})
    for key, pin in pins.items():
        assert key in doc["models"] \
            or key in doc["openrouter"]["models"], key
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


# --- integration through refresh_provider_rates.main ------------------------


def _main_doc(models: dict, members: list) -> dict:
    return _doc(models=models if models else None, members=members)


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
    """The subject's vendor segments are disjoint: a move that is both an
    auto-add and a meter fold counts under "added" alone, never twice."""
    listing = _price(1.0, 5.0, read=0.1,
                     overrides=[_band(1.0, 5.0, read=0.1)])
    run = MainRun(tmp_path, _main_doc({}, []), _catalog(GPT_ID),
                  {GPT_ID: _payload(_endpoint("openai", listing))})
    rc, _out, err = run(capsys)
    assert rc == 0, err
    subject = run.commit_msg.read_text(encoding="utf-8").partition("\n\n")[0]
    assert subject.endswith("vendor table: 1 added")
    assert "metered" not in subject


def test_main_vendor_refusal_is_red_but_other_moves_writes(tmp_path, capsys):
    run = MainRun(tmp_path, _main_doc({}, []), _catalog(GPT_ID),
                  {GPT_ID: _payload(_endpoint("openai/mxfp4", _price(1.0, 5.0)),
                                    _endpoint("openai/int4", _price(2.0, 9.0)))})
    rc, _out, err = run(capsys)
    assert rc == 1 and "2 prices" in err
    doc = json.loads(run.pricing_path.read_text(encoding="utf-8"))
    assert GPT_KEY not in doc["openrouter"]["models"]


def test_main_untracked_shape_stays_green(tmp_path, capsys):
    run = MainRun(tmp_path, _main_doc({}, []), _catalog(GPT_ID),
                  {GPT_ID: _payload(_endpoint(
                      "openai", _price(1.0, 5.0, image_output="0.00004")))})
    rc, out, err = run(capsys)
    assert rc == 0, err
    assert "not tracked" in out
    doc = json.loads(run.pricing_path.read_text(encoding="utf-8"))
    assert GPT_KEY not in doc["openrouter"]["models"]


def test_main_quiet_vendor_run_writes_nothing(tmp_path, capsys):
    """A run where nothing moved — the tracked entry and its provider row
    both reproduce the listing — is byte-identical, unbumped, messageless."""
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
    """Any fetch error — a URL error, a KeyError from a wrong fixture, any
    non-RefreshError — refuses that one model; the other models still
    join."""
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


# --- loaders see the pass's own output ----------------------------------------


def test_a_pass_added_document_loads():
    """The auto-add's entry shape and the fold it may carry satisfy the
    loaders on a synthetic document, tracked entry ahead of its provider
    row — the one-run pickup delay's shape."""
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
