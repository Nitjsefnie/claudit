"""Id-only tracked entries join the fold, without inventing a vendor host."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from backend import pricing, rate_fingerprint
from backend.rate_boundaries import rate_boundaries
from backend.pricing_rows import encode_pricing_doc
from tests.test_pricing_vendor import (
    BARE, CUT, PKEY, PREFIXED_BARE, ROUTER_HOST, R_NEW, R_OLD,
    _bare_id_doc, _copy_browser, _doc, _entry, _install, _loader_error,
    _node_raw,
)


def test_id_only_bare_entry_prices_prefixed_provider_history(
        tmp_path, monkeypatch):
    doc = _bare_id_doc()
    del doc["openrouter"]["models"][BARE]["vendor_host"]
    doc["providers"][BARE]["undefined"] = [_entry(R_OLD)]
    doc["openrouter"]["models"]["stealth/hidden"] = {"id": "stealth/hidden"}
    _install(monkeypatch, doc)
    assert pricing.VENDOR_BARE[BARE] == BARE
    assert BARE not in pricing.VENDOR_HOSTS
    assert "stealth/hidden" not in pricing.VENDOR_BARE
    for stamp, expected in [(CUT - timedelta(seconds=1), R_OLD), (CUT, R_NEW)]:
        result = pricing.resolve(PREFIXED_BARE, stamp, "DatedRouterHost")
        assert (result.kind, result.key, result.rates) == ("exact", BARE, expected)
    for provider in (None, "MissingHost"):
        assert pricing.resolve(PREFIXED_BARE, provider=provider).kind == "default"
        assert rate_boundaries(PREFIXED_BARE, provider) == []
        assert rate_fingerprint.pair_fingerprint(PREFIXED_BARE, provider)
    assert rate_boundaries(PREFIXED_BARE, "DatedRouterHost") == [CUT]
    (tmp_path / "pricing.json").write_text(json.dumps(encode_pricing_doc(doc)))
    _copy_browser(tmp_path)
    got = _node_raw(f"""
      global.window = {{}};
      require({str(tmp_path / 'pricing-loader.js')!r});
      require({str(tmp_path / 'rates.js')!r});
      console.log(JSON.stringify({{
        bare: window.vendorBare, hosts: window.vendorHosts,
        row: window.resolveModelRate({PREFIXED_BARE!r}, null, {ROUTER_HOST!r}),
        missing: window.resolveModelRate({PREFIXED_BARE!r}, null, 'MissingHost'),
        noProvider: window.resolveModelRate({PREFIXED_BARE!r}),
        literal: window.resolveModelRate({PREFIXED_BARE!r}, null, 'undefined'),
      }}));
    """)
    assert got["bare"] == pricing.VENDOR_BARE
    assert got["hosts"] == pricing.VENDOR_HOSTS
    assert (got["row"]["kind"], got["row"]["key"], got["row"]["rates"]["fresh"]) == (
        "exact", BARE, R_OLD["fresh"])
    assert got["missing"]["kind"] == "default"
    assert got["noProvider"]["kind"] == "default"
    assert got["literal"]["kind"] == pricing.resolve(
        PREFIXED_BARE, provider="undefined").kind == "exact"


@pytest.mark.parametrize("collision", ["bare", "prefixed", "models"])
def test_id_only_bare_form_collisions_refuse_on_both_sides(tmp_path, collision):
    doc = _doc()
    if collision == "models":
        doc["models"]["other-9"] = [_entry(R_OLD)]
    else:
        key = BARE if collision == "bare" else f"other/{BARE}"
        doc["openrouter"]["vendor"]["prefixes"].append("other")
        doc["openrouter"]["models"][key] = {"id": "acme/glm-acme.9"}
        del doc["openrouter"]["models"][PKEY]["vendor_host"]
    with pytest.raises(ValueError, match="bare form"):
        pricing.load_tables(doc)
    assert "bare form" in (_loader_error(tmp_path, doc) or "")


def test_an_id_only_default_row_refuses_cleanly_on_both_sides(tmp_path):
    doc = _doc()
    doc["models"] = {}
    doc["openrouter"]["models"] = {
        "claude-opus-4-7": {"id": "acme/claude-opus-4.7"}}
    doc["providers"] = {"claude-opus-4-7": {"undefined": [_entry(R_OLD)]}}
    with pytest.raises(ValueError, match="default estimate"):
        pricing.load_tables(doc)
    assert "default estimate" in (_loader_error(tmp_path, doc) or "")
