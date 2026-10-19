"""The stored document's compact shape, checked at the raw-file
boundaries only (storage half): a cache-write rate stored in
src/pricing.json must differ from `fresh` — compact_pricing_doc strips an
equal pair at every write, so a present pair is a hand edit. The shared
validation (`load_tables`, the node loader path) stays shape-agnostic and
keeps accepting the expanded intermediate every writer and test hands it;
the page's ingest calls the check on the fetched file itself.
"""
from __future__ import annotations

import json

import pytest

from backend.pricing_document import check_compact_shape
from backend.pricing_rows import encode_pricing_doc
from tests.pricing_data_loader_helpers import _browser_load
from tests.refresh_fixture_builders import RATES_B, seed_provider_doc
from tests.test_pricing_data import (
    LOADER_JS,
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
    doc = seed_provider_doc()
    hosts = next(iter(doc["providers"].values()))
    history = next(iter(hosts.values()))
    rates = dict(RATES_B)
    rates["create_1h"] = rates["fresh"]
    history[0]["schedule"] = [{"rates": rates}]
    return encode_pricing_doc(doc)


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


# A stored entry or schedule window repeating fresh is refused, naming the row.
@pytest.mark.parametrize("document,message", [
    pytest.param(_entry_violation,
                 r"bonsai-2-27b\[0\]: create_5m is present and equals fresh",
                 id="entry"),
    pytest.param(_window_violation,
                 r"fixture/provider via Fixture\[0\]\.schedule\[0\]: "
                 r"create_1h is present and equals fresh", id="window"),
])
def test_a_stored_cache_write_equal_to_fresh_is_refused(document, message):
    with pytest.raises(ValueError, match=message):
        check_compact_shape(document())


# (c) the committed raw src/pricing.json passes.
def test_the_committed_document_passes_the_compact_shape_check():
    check_compact_shape(_raw())


@pytest.mark.parametrize("document,spot,message", [
    pytest.param(_entry_violation, "bonsai-2-27b[0]",
                 "create_5m is present and equals fresh", id="entry"),
    pytest.param(_window_violation, "Fixture[0].schedule[0]",
                 "create_1h is present and equals fresh", id="window"),
])
@needs_node
def test_the_browser_check_refuses_a_stored_pair(document, spot, message):
    error = _node_compact(document())
    assert error and spot in error, error
    assert message in error, error


@needs_node
def test_the_browser_check_accepts_the_committed_document():
    assert _node_compact(_raw()) is None


@needs_node
def test_the_page_ingest_refuses_an_uncompacted_document():
    """The page's raw-ingest path CALLS the check (not just exposes it):
    an XHR body with a present equal pair fails the load naming the row."""
    got = _browser_load(body=json.dumps(_entry_violation()))
    assert got["error"] and "bonsai-2-27b[0]" in got["error"], got["error"]
    assert "is present and equals fresh" in got["error"], got["error"]
