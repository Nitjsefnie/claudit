"""Both loaders refuse a rule-breaking pricing document, naming the row
(issues #930, #931): a history entry that repeats its predecessor, a band
that excludes the entry's own rates, a `from` after the fetch instant, and
a dual-pin tag disagreement between openrouter.vendor.resolve and the
tracked entry's own resolve.

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


# --- issue #930: an entry never repeats its predecessor -----------------------

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


# --- issue #931.1: the band contains the entry's own rates -------------------

def _excluded_band() -> dict:
    doc = _doc()
    doc["providers"][_GLM_MODEL][_GLM_HOST][-1]["band"] = {"fresh": [5.0,
                                                                     9.0]}
    return doc


def test_a_band_that_excludes_the_entry_rates_is_refused():
    with pytest.raises(ValueError,
                       match=r"glm-5-3-flash via Z\.AI\[\d+\]: "
                             r"band\[fresh\].*does not contain"):
        pricing.load_tables(_excluded_band())


@needs_node
def test_a_band_that_excludes_the_entry_rates_is_refused_in_the_browser(
        tmp_path):
    error = _node_load(tmp_path, _excluded_band())
    assert error, "the browser must refuse the excluding band"
    assert "glm-5-3-flash via Z.AI" in error, error
    assert "band[fresh]" in error and "does not contain" in error, error


# --- issue #931.2: no `from` postdates provider_rates_fetched ----------------

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


# --- issue #931.4: a dual pin agrees on its tag ------------------------------

def _disagreeing_dual_pin() -> dict:
    doc = _doc()
    doc["openrouter"]["vendor"]["resolve"]["kimi-k2-7-code"]["tag"] = \
        "moonshotai/highspeed"
    return doc


def test_dual_pin_tags_that_differ_are_refused():
    with pytest.raises(ValueError,
                       match=r"openrouter\.models\['kimi-k2-7-code'\]"):
        pricing.load_tables(_disagreeing_dual_pin())


def test_a_pin_without_a_tag_on_one_side_is_not_compared():
    doc = _doc()
    del doc["openrouter"]["models"]["kimi-k2-7-code"]["resolve"]
    tables = pricing.load_tables(doc)
    assert "MODEL_RATES" in tables


@needs_node
def test_dual_pin_tags_that_differ_are_refused_in_the_browser(tmp_path):
    error = _node_load(tmp_path, _disagreeing_dual_pin())
    assert error, "the browser must refuse the disagreeing dual pin"
    assert "kimi-k2-7-code" in error, error
    assert "vendor resolve tags" in error, error


@needs_node
def test_a_pin_without_a_tag_on_one_side_loads_in_the_browser(tmp_path):
    doc = _doc()
    del doc["openrouter"]["models"]["kimi-k2-7-code"]["resolve"]
    assert _node_load(tmp_path, doc) is None


# The committed document itself: every refusal above must leave it loading.
def test_the_committed_document_still_loads():
    assert pricing.load_tables(_doc())["MODEL_RATES"]
    assert json.loads(_pricing_json().read_text(encoding="utf-8"))
