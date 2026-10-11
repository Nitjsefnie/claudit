"""The stored document's compact shape, checked at the raw-file
boundaries only (issue #931, storage half): a cache-write rate stored in
src/pricing.json must differ from `fresh` — compact_pricing_doc strips an
equal pair at every write, so a present pair is a hand edit. The shared
validation (`load_tables`, the node loader path) stays shape-agnostic and
keeps accepting the expanded intermediate every writer and test hands it;
the page's ingest calls the check on the fetched file itself.
"""
from __future__ import annotations

import json

import pytest

from backend import pricing
from backend.pricing_document import check_compact_shape
from tests.pricing_data_loader_helpers import _browser_load, _node_load
from tests.test_pricing_data import (
    LOADER_JS,
    _doc,
    _node_raw,
    _pricing_json,
    needs_node,
)


def _raw() -> dict:
    return json.loads(_pricing_json().read_text(encoding="utf-8"))


def _entry_violation() -> dict:
    doc = _raw()
    row = doc["models"]["bonsai-2-27b"]
    row[0]["create_5m"] = row[0]["fresh"]
    return doc


def _window_violation() -> dict:
    doc = _raw()
    rates = (doc["providers"]["deepseek/deepseek-v4-1-flash"]["Alibaba"][0]
             ["schedule"][0]["rates"])
    rates["create_1h"] = rates["fresh"]
    return doc


def _node_compact(doc: dict) -> str | None:
    """The browser loader's exported check against `doc`; its error."""
    return _node_raw(f"""
      global.window = {{}};
      const loader = require({str(LOADER_JS)!r});
      let error = null;
      try {{ loader.checkCompactShape({json.dumps(doc)}); }}
      catch (e) {{ error = e.message; }}
      console.log(JSON.stringify(error));
    """)


# (a) a stored entry carrying create_5m == fresh -> refused, naming the row.
def test_a_stored_entry_cache_write_equal_to_fresh_is_refused():
    with pytest.raises(ValueError,
                       match=r"bonsai-2-27b\[0\]: create_5m is present "
                             r"and equals fresh"):
        check_compact_shape(_entry_violation())


# (b) a stored schedule window carrying create_1h == fresh -> refused.
def test_a_stored_window_cache_write_equal_to_fresh_is_refused():
    with pytest.raises(
            ValueError,
            match=r"deepseek/deepseek-v4-1-flash via "
                  r"Alibaba\[0\]\.schedule\[0\]: create_1h is present "
                  r"and equals fresh"):
        check_compact_shape(_window_violation())


# (c) the committed raw src/pricing.json passes.
def test_the_committed_document_passes_the_compact_shape_check():
    check_compact_shape(_raw())


# (d) the expanded document still loads through the shared validation.
def test_an_expanded_document_still_loads_through_load_tables():
    """expand_pricing_doc materialises the equal pair on thousands of
    entries; load_tables must keep accepting them — the refresh validates
    its own expanded intermediate through it."""
    assert pricing.load_tables(_doc())["MODEL_RATES"]


@needs_node
def test_the_browser_check_refuses_a_stored_entry_pair():
    error = _node_compact(_entry_violation())
    assert error and "bonsai-2-27b[0]" in error, error
    assert "create_5m is present and equals fresh" in error, error


@needs_node
def test_the_browser_check_refuses_a_stored_window_pair():
    error = _node_compact(_window_violation())
    assert error and "Alibaba[0].schedule[0]" in error, error
    assert "create_1h is present and equals fresh" in error, error


@needs_node
def test_the_browser_check_accepts_the_committed_document():
    assert _node_compact(_raw()) is None


@needs_node
def test_the_node_loader_still_accepts_an_expanded_document(tmp_path):
    assert _node_load(tmp_path, _doc()) is None


@needs_node
def test_the_page_ingest_refuses_an_uncompacted_document():
    """The page's raw-ingest path CALLS the check (not just exposes it):
    an XHR body with a present equal pair fails the load naming the row."""
    got = _browser_load(body=json.dumps(_entry_violation()))
    assert got["error"] and "bonsai-2-27b[0]" in got["error"], got["error"]
    assert "is present and equals fresh" in got["error"], got["error"]