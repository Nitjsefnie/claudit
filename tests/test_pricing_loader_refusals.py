"""Both loaders refuse a rule-breaking pricing document, naming the row
including repeated states, removed band keys, post-fetch timestamps, and
dual-pin tag disagreements.

Backend and browser sides each get the refusing case plus the boundary
the refusal must NOT cross, driven through the shared harnesses in
tests/test_pricing_data.py and tests/pricing_data_loader_helpers.py.
"""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from backend import pricing
from tests.pricing_data_loader_helpers import _node_load
from tests.refresh_fixture_builders import seed_doc
from tests.test_pricing_data import (
    _GLM_HOST,
    _GLM_MODEL,
    _doc,
    _latest_document_stamp,
    _pricing_json,
    _stamp,
    needs_node,
)


def _cut(doc: dict) -> str:
    return _stamp(_latest_document_stamp(doc) + timedelta(seconds=1))


# --- an entry never repeats its predecessor -----------------------

def _repeated_append() -> dict:
    """The newest provider entry appended a second time under a later
    `from`: nothing moved but the instant, the no-op the loader refuses."""
    doc = _doc()
    history = doc["providers"][_GLM_MODEL][_GLM_HOST]
    cut = _cut(doc)
    history.append({**{k: v for k, v in history[-1].items() if k != "from"},
                    "from": cut})
    doc["provider_rates_fetched"] = cut
    return doc


def _schedule_only_append() -> dict:
    """The same append with ONE schedule the row did not carry: rates
    identical, but a schedule is a real difference, so it must load."""
    doc = _doc()
    history = doc["providers"][_GLM_MODEL][_GLM_HOST]
    cut = _cut(doc)
    history.append({**{k: v for k, v in history[-1].items() if k != "from"},
                    "from": cut,
                    "schedule": [{"days": ["monday"], "start": 0,
                                  "end": 100,
                                  "rates": {"fresh": 0.5, "read": 0.05,
                                            "output": 2.0}}]})
    doc["provider_rates_fetched"] = cut
    return doc


def test_a_repeated_entry_under_a_later_from_is_refused():
    with pytest.raises(ValueError,
                       match=r"glm-5-3-flash via Z\.AI\[\d+\]: equal to "
                             r"the previous entry except 'from'"):
        pricing.load_tables(_repeated_append())


def test_an_append_that_changes_only_the_schedule_is_accepted():
    doc = _schedule_only_append()
    tables = pricing.load_tables(doc)
    history = doc["providers"][_GLM_MODEL][_GLM_HOST]
    schedules = tables["PROVIDER_SCHEDULES"][_GLM_MODEL, _GLM_HOST]
    assert len(history) - 1 in schedules


@needs_node
def test_a_repeated_entry_under_a_later_from_is_refused_in_the_browser(
        tmp_path):
    error = _node_load(tmp_path, _repeated_append())
    assert error, "the browser must refuse the repeated entry"
    assert "glm-5-3-flash via Z.AI" in error, error
    assert "equal to the previous entry except 'from'" in error, error


@needs_node
def test_an_append_that_changes_only_the_schedule_loads_in_the_browser(
        tmp_path):
    assert _node_load(tmp_path, _schedule_only_append()) is None


# --- removed bands are refused regardless of their shape --------------------

@pytest.mark.parametrize("band", [{}, {"fresh": [0, 100]}, None, "obsolete"])
@needs_node
def test_any_band_key_is_refused_by_both_loaders_naming_the_same_row(tmp_path, band):
    doc = _doc()
    history = doc["providers"][_GLM_MODEL][_GLM_HOST]
    history[-1]["band"] = band
    where = f"{_GLM_MODEL} via {_GLM_HOST}[{len(history) - 1}]"
    error = _node_load(tmp_path, doc)
    assert error and where in error and "band" in error, error
    with pytest.raises(ValueError) as caught:
        pricing.load_tables(doc)
    assert where in str(caught.value) and "band" in str(caught.value)


# ---.2: no `from` postdates provider_rates_fetched ----------------

def _dated_past_fetch() -> dict:
    """A move appended after the fetch instant: refused until the fetch
    stamp catches up, then accepted at the boundary (`==`)."""
    doc = _doc()
    history = doc["providers"][_GLM_MODEL][_GLM_HOST]
    cut = _cut(doc)
    history.append({**{k: v for k, v in history[-1].items() if k != "from"},
                    "from": cut, "read": 0.031})
    return doc


def test_a_from_after_provider_rates_fetched_is_refused_then_accepted():
    doc = _dated_past_fetch()
    with pytest.raises(
            ValueError,
            match=r"glm-5-3-flash via Z\.AI\[\d+\]: 'from' .* is after "
                  r"provider_rates_fetched"):
        pricing.load_tables(doc)
    doc["provider_rates_fetched"] = doc["providers"][_GLM_MODEL][_GLM_HOST][-1]["from"]
    pricing.load_tables(doc)


@needs_node
def test_a_from_after_provider_rates_fetched_is_refused_in_the_browser(
        tmp_path):
    error = _node_load(tmp_path, _dated_past_fetch())
    assert error, "the browser must refuse the post-fetch instant"
    assert "glm-5-3-flash via Z.AI" in error, error
    assert "is after provider_rates_fetched" in error, error


# ---.4: a dual pin agrees on its tag ------------------------------

_PIN_MODEL = "acme/synthetic-9"


def _dual_pin_doc() -> dict:
    return seed_doc(
        prefixes=["acme"],
        providers={_PIN_MODEL: {"Fixture": [{"fresh": 1.0, "read": 0.1, "output": 2.0}]}},
        tracked={_PIN_MODEL: {
            "id": _PIN_MODEL, "vendor_host": "Fixture",
            "resolve": {"Fixture": {"tag": "fixture/base", "why": "synthetic host pin"}},
        }},
        resolve={_PIN_MODEL: {"tag": "fixture/base", "why": "synthetic vendor pin"}},
    )


def _disagreeing_dual_pin() -> dict:
    doc = _dual_pin_doc()
    doc["openrouter"]["vendor"]["resolve"][_PIN_MODEL]["tag"] = "fixture/fast"
    return doc


def test_dual_pin_tags_that_differ_are_refused():
    assert pricing.load_tables(_dual_pin_doc())["VENDOR_HOSTS"][_PIN_MODEL] == "Fixture"
    with pytest.raises(ValueError,
                       match=r"openrouter\.models\['acme/synthetic-9'\].*vendor resolve tags"):
        pricing.load_tables(_disagreeing_dual_pin())


def test_a_pin_without_a_tag_on_one_side_is_not_compared():
    doc = _dual_pin_doc()
    doc["openrouter"]["models"][_PIN_MODEL]["resolve"]["Fixture"] = {
        "select": "cheapest", "why": "synthetic tagless pin"}
    tables = pricing.load_tables(doc)
    assert "MODEL_RATES" in tables


@needs_node
def test_dual_pin_tags_that_differ_are_refused_in_the_browser(tmp_path):
    assert _node_load(tmp_path, _dual_pin_doc()) is None
    error = _node_load(tmp_path, _disagreeing_dual_pin())
    assert error, "the browser must refuse the disagreeing dual pin"
    assert _PIN_MODEL in error, error
    assert "vendor resolve tags" in error, error


@needs_node
def test_a_pin_without_a_tag_on_one_side_loads_in_the_browser(tmp_path):
    doc = _dual_pin_doc()
    doc["openrouter"]["models"][_PIN_MODEL]["resolve"]["Fixture"] = {
        "select": "cheapest", "why": "synthetic tagless pin"}
    assert _node_load(tmp_path, doc) is None


# The committed document itself: every refusal above must leave it loading.
def test_the_committed_document_still_loads():
    assert pricing.load_tables(_doc())["MODEL_RATES"]
    assert json.loads(_pricing_json().read_text(encoding="utf-8"))
