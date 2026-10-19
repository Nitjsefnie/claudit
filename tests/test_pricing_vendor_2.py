from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta

from backend.pricing_rows import encode_pricing_doc
from tests.test_pricing_vendor import (
    BARE,
    CUT,
    DATED_ROUTER_HOST,
    HHMM_JS,
    KEY,
    LOADER_JS,
    PARSER_JS,
    PREFIXED_BARE,
    RATES_JS,
    RATE_FIELDS,
    ROUTER_HOST,
    R_NEW,
    R_OLD,
    R_THIRD,
    UTC,
    VENDOR_TABLES_JS,
    _bare_id_doc,
    _doc,
    _install,
    _js_rates,
    _node_raw,
    needs_node,
)


def _copy_browser(tmp_path):
    for source in (LOADER_JS, VENDOR_TABLES_JS, HHMM_JS,
                   HHMM_JS.with_name("pricing-rows.js"), RATES_JS, PARSER_JS):
        shutil.copy(source, tmp_path / source.name)


@needs_node
def test_the_browser_prefixed_id_uses_the_bare_provider_and_vendor_rows(
        tmp_path):
    (tmp_path / "pricing.json").write_text(
        json.dumps(encode_pricing_doc(_bare_id_doc())), encoding="utf-8")
    _copy_browser(tmp_path)
    got = _node_raw(f"""
      global.window = {{}};
      require({str(tmp_path / 'pricing-loader.js')!r});
      require({str(tmp_path / 'rates.js')!r});
      const fold = {json.dumps(PREFIXED_BARE)};
      console.log(JSON.stringify({{
        host: window.resolveModelRate(fold, null, {json.dumps(ROUTER_HOST)}),
        vendor: window.resolveModelRate(fold, null, 'MissingHost'),
        untracked: window.resolveModelRate('acme/untracked-9', null, null),
      }}));
    """)
    assert (got["host"]["kind"], got["host"]["key"]) == ("exact", BARE)
    assert _js_rates(got["host"]["rates"]) == R_OLD
    assert (got["vendor"]["kind"], got["vendor"]["key"]) == \
        ("exact", BARE)
    assert _js_rates(got["vendor"]["rates"]) == R_NEW
    assert got["untracked"]["kind"] == "default"


def test_vendor_boundaries_for_a_bare_pair(monkeypatch):
    _install(monkeypatch, _doc())
    from backend.rate_boundaries import rate_boundaries  # pylint: disable=import-outside-toplevel
    assert rate_boundaries(KEY, None) == [CUT]
    assert rate_boundaries(KEY, "") == [CUT], "an empty provider is bare"


def test_vendor_boundaries_for_a_prefixed_bare_form(monkeypatch):
    _install(monkeypatch, _bare_id_doc())
    from backend.rate_boundaries import rate_boundaries  # pylint: disable=import-outside-toplevel
    assert rate_boundaries(PREFIXED_BARE, None) == [CUT]


def test_vendor_boundaries_for_a_prefixed_provider_pair(monkeypatch):
    _install(monkeypatch, _bare_id_doc())
    from backend.rate_boundaries import rate_boundaries  # pylint: disable=import-outside-toplevel
    prefixed = rate_boundaries(PREFIXED_BARE, DATED_ROUTER_HOST)
    assert prefixed == [CUT]
    assert prefixed == rate_boundaries(BARE, DATED_ROUTER_HOST)


def test_vendor_boundaries_honor_a_start(monkeypatch):
    _install(monkeypatch, _doc(start=CUT - timedelta(days=30)))
    from backend.rate_boundaries import rate_boundaries  # pylint: disable=import-outside-toplevel
    assert rate_boundaries(KEY, None) == [CUT - timedelta(days=30), CUT]


def test_vendor_boundaries_absent_without_a_row(monkeypatch):
    """A tracked vendor entry with no providers row prices nothing: no
    boundaries either (the resolve path falls through the same way)."""
    doc = _doc()
    del doc["providers"][KEY]
    _install(monkeypatch, doc)
    from backend.rate_boundaries import rate_boundaries  # pylint: disable=import-outside-toplevel
    assert rate_boundaries(KEY, None) == []


def test_the_pre_start_fold_uses_the_vendor_windows(monkeypatch):
    """A provider row that begins at a time: before it, a record prices by
    the model alone — which for a vendor key is the vendor row's windows,
    so the fold's boundaries carry them."""
    vendor_start = CUT - timedelta(days=30)
    doc = _doc(start=vendor_start)
    provider_start = datetime(2027, 1, 1, tzinfo=UTC)
    doc["providers"][KEY]["OtherHost"] = [
        {"from": provider_start.isoformat().replace("+00:00", "Z"),
         **{f: R_THIRD[f] for f in RATE_FIELDS}}]
    _install(monkeypatch, doc)
    from backend.rate_boundaries import rate_boundaries  # pylint: disable=import-outside-toplevel
    assert rate_boundaries(KEY, "OtherHost") == [
        vendor_start, CUT, provider_start]
