"""Tests moved from test_pricing_data.py to keep test modules under 700 lines."""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backend import app as app_mod
from backend import pricing
from backend import session as session_mod

from tests.test_pricing_data import (
    CUT,
    DAMAGE,
    EDGE_STAMPS,
    LATER,
    MOVED,
    NEWCOMER,
    ORIGIN,
    LOADER_JS,
    PARSER_JS,
    RATES_JS,
    P_AFTER,
    P_BEFORE,
    P_CUT,
    P_START,
    RATE_FIELDS,
    R_NIGHT,
    R_WEEKEND,
    R_WRAP,
    SCHEDULED,
    SCHEDULE_CASES,
    SCHEDULE_DAMAGE,
    UNSPELLABLE_IN_JSON,
    V_SUFFIXES,
    _at,
    _browser_load,
    _damaged,
    _doc,
    _js_rates,
    _model_row_beginning,
    _model_schedule,
    _newcomer_then_moved,
    _node,
    _node_load,
    _provider_only_doc,
    _provider_string_rate,
    _pricing_json,
    _stamp,
    _variant_node,
    _variant_row_doc,
    _when,
    _with_newcomer,
    _with_schedule,
    needs_node,
)


@pytest.mark.parametrize("damage", DAMAGE)
def test_a_malformed_history_is_refused(damage):
    """A misordered or rewritten history, or a rate that is not a finite
    non-negative number, would silently misprice or crash ingest, so the
    loader refuses it, naming the row, and the suite goes red instead."""
    with pytest.raises(ValueError, match=r"glm-5-3-flash\[\d+\]"):
        pricing.load_tables(_damaged(damage))


@needs_node
@pytest.mark.parametrize("damage", DAMAGE)
def test_a_malformed_history_is_refused_in_the_browser(tmp_path, request, damage):
    error = _node_load(tmp_path, _damaged(damage))
    assert error and error.startswith("pricing.json: "), error
    if request.node.callspec.id not in UNSPELLABLE_IN_JSON:
        assert re.search(r"glm-5-3-flash\[\d+\]", error), error


def test_a_string_provider_rate_is_refused_naming_the_row():
    with pytest.raises(ValueError, match="deepseek/deepseek-v4-flash via Azure"):
        pricing.load_tables(_provider_string_rate())


@needs_node
def test_a_string_provider_rate_is_refused_naming_the_row_in_the_browser(tmp_path):
    error = _node_load(tmp_path, _provider_string_rate())
    assert error and "deepseek/deepseek-v4-flash via Azure" in error, error


@needs_node
def test_the_browser_loads_the_url_the_page_names_synchronously():
    got = _browser_load(pricing_attr="/src/pricing.json?v=7")
    assert got["error"] is None
    assert got["requests"] == [{
        "method": "GET", "url": ORIGIN + "/src/pricing.json?v=7",
        "async": False, "headers": {"Cache-Control": "no-cache"},
    }]
    assert got["fresh"] == pricing.MODEL_RATES["claude-opus-4-7"]["fresh"]


@needs_node
def test_with_no_url_named_the_browser_loads_the_file_beside_the_script():
    """Resolved against the script, not the page: the page may sit at any
    depth, the script is always /src/parser.js."""
    got = _browser_load()
    assert got["error"] is None
    assert [r["url"] for r in got["requests"]] == [ORIGIN + "/src/pricing.json"]
    assert got["requests"][0]["async"] is False


@needs_node
@pytest.mark.parametrize("response, reason", [
    pytest.param({"status": 404}, "HTTP 404", id="not-found"),
    pytest.param({"status": 200, "body": "<!doctype html><title>Sign in</title>",
                  "redirected_to": ORIGIN + "/login"},
                 "redirected to " + ORIGIN + "/login", id="signed-out"),
    pytest.param({"status": 200, "body": "{not json"}, "not JSON", id="garbled"),
])
def test_a_failed_browser_load_throws_naming_the_file(response, reason):
    """No rates means no honest price, so the script stops rather than
    priming the resolver with nothing; the thrown error names the file and
    the cause — a signed-out redirect included, which would otherwise
    surface as a bare JSON syntax error."""
    got = _browser_load(**response)
    assert got["error"].startswith("pricing.json: "), got["error"]
    assert reason in got["error"], got["error"]


def test_the_page_names_the_file_with_its_own_cache_bust():
    """Every /src asset the page loads carries ?v=<mtime>, so an edge cache
    serves a fresh copy the moment the file changes; pricing.json too."""
    client = TestClient(app_mod.app)
    client.cookies.set(session_mod.SESSION_COOKIE_NAME,
                       session_mod.make_guest_session_token())
    page = client.get("/").text
    version = int(_pricing_json().stat().st_mtime)
    tag = re.search(
        r'<script[^>]*src="/src/pricing-loader\.js[^"]*"[^>]*>', page)
    assert tag, page
    assert f'data-pricing="/src/pricing.json?v={version}"' in tag.group(0)
    # The parser half loads after its loader and names no pricing of its own.
    parser_tag = re.search(
        r'<script[^>]*src="/src/parser\.js[^"]*"[^>]*>', page)
    assert parser_tag, page
    assert "data-pricing" not in parser_tag.group(0)


@needs_node
@pytest.mark.parametrize("stamp", EDGE_STAMPS)
def test_both_sides_read_an_edge_spelling_as_the_same_instant(
        tmp_path, stamp: str) -> None:
    rates = {"fresh": 7.0, "create_5m": 8.0, "create_1h": 9.0,
             "read": 0.7, "output": 70.0}
    model = "acme/edge-9"
    doc = {
        "models": {model: [{"from": None, **rates},
                           {"from": stamp, **rates}]},
        "providers": {},
        "provider_rates_fetched": "2030-01-01T00:00:00Z",
        "long_context_models": [],
    }
    want = int(_at(stamp).timestamp() * 1000)
    assert want in [int(e.timestamp() * 1000)
                    for e in pricing.load_tables(doc)["RATE_EPOCHS"]]
    (tmp_path / "pricing.json").write_text(json.dumps(doc), encoding="utf-8")
    shutil.copy(LOADER_JS, tmp_path / "pricing-loader.js")
    shutil.copy(RATES_JS, tmp_path / "rates.js")
    shutil.copy(PARSER_JS, tmp_path / "parser.js")
    assert want in _node(tmp_path / "parser.js",
                         "console.log(JSON.stringify(window.rateEpochs));")


def test_a_provider_row_that_begins_at_a_time_prices_from_then_on(monkeypatch):
    before = _at(CUT) - timedelta(seconds=1)
    fallback = pricing.resolve("z-ai/glm-5.3-flash", before)
    for name, value in pricing.load_tables(_with_newcomer()).items():
        monkeypatch.setattr(pricing, name, value)
    assert _at(CUT) in pricing.RATE_EPOCHS
    assert pricing.resolve("z-ai/glm-5.3-flash", before, "Newcomer") == fallback
    assert pricing.rate_for("z-ai/glm-5.3-flash", _at(CUT), "Newcomer") == NEWCOMER
    assert pricing.rate_for("z-ai/glm-5.3-flash", None, "Newcomer") == NEWCOMER


@needs_node
def test_a_provider_row_that_begins_at_a_time_prices_from_then_on_in_the_browser(
        tmp_path):
    (tmp_path / "pricing.json").write_text(
        json.dumps(_with_newcomer()), encoding="utf-8")
    shutil.copy(LOADER_JS, tmp_path / "pricing-loader.js")
    shutil.copy(RATES_JS, tmp_path / "rates.js")
    shutil.copy(PARSER_JS, tmp_path / "parser.js")
    before = _stamp(_at(CUT) - timedelta(seconds=1))
    got = _node(tmp_path / "parser.js", f"""
      const m = 'z-ai/glm-5.3-flash';
      console.log(JSON.stringify({{
        before: window.resolveModelRate(m, {json.dumps(before)}, 'Newcomer'),
        fallback: window.resolveModelRate(m, {json.dumps(before)}),
        at: window.rateForModel(m, {json.dumps(CUT)}, 'Newcomer'),
        now: window.rateForModel(m, null, 'Newcomer'),
        epochs: window.rateEpochs,
      }}));
    """)
    assert got["before"] == got["fallback"]
    assert _js_rates(got["at"]) == NEWCOMER
    assert _js_rates(got["now"]) == NEWCOMER
    assert int(_at(CUT).timestamp() * 1000) in got["epochs"]


def test_a_model_row_cannot_begin_at_a_time():
    """A model row has no honest fallback — before it, the id would price
    as a tier or default estimate — so it always covers all of time."""
    with pytest.raises(ValueError, match=r"glm-5-3-flash\[0\]"):
        pricing.load_tables(_model_row_beginning())


@needs_node
def test_a_model_row_cannot_begin_at_a_time_in_the_browser(tmp_path):
    error = _node_load(tmp_path, _model_row_beginning())
    assert error and "glm-5-3-flash[0]" in error, error


@pytest.mark.parametrize("stamp, want", SCHEDULE_CASES)
def test_a_scheduled_entry_prices_by_utc_weekday_and_time(monkeypatch, stamp, want):
    model, host = SCHEDULED
    default = {f: _doc()["providers"][model][host][-1][f] for f in RATE_FIELDS}
    for name, value in pricing.load_tables(_with_schedule()).items():
        monkeypatch.setattr(pricing, name, value)
    assert pricing.rate_for(model, _at(stamp), host) == (want or default)


def test_a_scheduled_entry_prices_a_naive_timestamp_as_utc(monkeypatch):
    model, host = SCHEDULED
    for name, value in pricing.load_tables(_with_schedule()).items():
        monkeypatch.setattr(pricing, name, value)
    assert pricing.rate_for(model, datetime(2031, 1, 4, 12), host) == R_WEEKEND


def test_a_scheduled_entry_with_no_timestamp_is_its_default(monkeypatch):
    model, host = SCHEDULED
    default = {f: _doc()["providers"][model][host][-1][f] for f in RATE_FIELDS}
    for name, value in pricing.load_tables(_with_schedule()).items():
        monkeypatch.setattr(pricing, name, value)
    assert pricing.rate_for(model, None, host) == default


@needs_node
def test_both_sides_price_a_schedule_identically_across_the_week(tmp_path):
    """Every hour of a week, one second either side of each window edge,
    the midnight wrap and both weekend days."""
    doc = _with_schedule()
    start = _at("2031-01-06T00:00:00Z")
    stamps = [_stamp(start + timedelta(minutes=30 * i)) for i in range(7 * 48)]
    for day in range(8):
        for hhmm in (0, 100, 200, 2200):
            edge = start + timedelta(days=day - 1, hours=hhmm // 100)
            stamps += [_stamp(edge + timedelta(seconds=s)) for s in (-1, 0, 1)]
    model, host = SCHEDULED
    tables = pricing.load_tables(doc)
    (tmp_path / "pricing.json").write_text(json.dumps(doc), encoding="utf-8")
    shutil.copy(LOADER_JS, tmp_path / "pricing-loader.js")
    shutil.copy(RATES_JS, tmp_path / "rates.js")
    shutil.copy(PARSER_JS, tmp_path / "parser.js")
    got = _node(tmp_path / "parser.js", f"""
      const stamps = {json.dumps(stamps)};
      console.log(JSON.stringify(stamps.map(
        ts => window.rateForModel({json.dumps(model)}, ts, {json.dumps(host)}))));
    """)
    saved = {name: getattr(pricing, name) for name in tables}
    try:
        for name, value in tables.items():
            setattr(pricing, name, value)
        want = [pricing.rate_for(model, _at(s), host) for s in stamps]
    finally:
        for name, value in saved.items():
            setattr(pricing, name, value)
    assert [_js_rates(r) for r in got] == want
    assert {json.dumps(w, sort_keys=True) for w in want} >= {
        json.dumps(r, sort_keys=True) for r in (R_WEEKEND, R_NIGHT, R_WRAP)}


@pytest.mark.parametrize("schedule", SCHEDULE_DAMAGE)
def test_a_malformed_schedule_is_refused(schedule):
    with pytest.raises(ValueError, match=r"z-ai/glm-5-3-flash via Novita\[\d+\]"):
        pricing.load_tables(_with_schedule(schedule))


@needs_node
@pytest.mark.parametrize("schedule", SCHEDULE_DAMAGE)
def test_a_malformed_schedule_is_refused_in_the_browser(tmp_path, schedule):
    error = _node_load(tmp_path, _with_schedule(schedule))
    assert error and ("z-ai/glm-5-3-flash via Novita[" in error or "spells a schedule" in error), error


def test_a_model_row_cannot_carry_a_schedule():
    with pytest.raises(ValueError, match=r"glm-5-3-flash\[\d+\]"):
        pricing.load_tables(_model_schedule())


@needs_node
def test_a_model_row_cannot_carry_a_schedule_in_the_browser(tmp_path):
    error = _node_load(tmp_path, _model_schedule())
    assert error and "glm-5-3-flash[" in error, error


def test_a_row_that_begins_then_moves_prices_each_span(monkeypatch):
    model, host = "z-ai/glm-5.3-flash", "Newcomer"
    before = _at(CUT) - timedelta(seconds=1)
    fallback = pricing.resolve(model, before)
    for name, value in pricing.load_tables(_newcomer_then_moved()).items():
        monkeypatch.setattr(pricing, name, value)
    assert {_at(CUT), _at(LATER)} <= set(pricing.RATE_EPOCHS)
    assert pricing.resolve(model, before, host) == fallback
    assert pricing.rate_for(model, _at(CUT), host) == NEWCOMER
    assert pricing.rate_for(model, _at(LATER) - timedelta(seconds=1), host) == NEWCOMER
    assert pricing.rate_for(model, _at(LATER), host) == MOVED
    assert pricing.rate_for(model, None, host) == MOVED


def test_a_naive_timestamp_against_a_row_that_begins_is_read_as_utc(monkeypatch):
    model, host = "z-ai/glm-5.3-flash", "Newcomer"
    for name, value in pricing.load_tables(_newcomer_then_moved()).items():
        monkeypatch.setattr(pricing, name, value)
    start = _at(CUT).replace(tzinfo=None)
    assert pricing.resolve(model, start - timedelta(seconds=1), host).key != model.replace(".", "-")
    assert pricing.rate_for(model, start, host) == NEWCOMER
    assert pricing.rate_for(model, _at(LATER).replace(tzinfo=None), host) == MOVED


@needs_node
def test_a_row_that_begins_then_moves_prices_alike_in_the_browser(tmp_path):
    doc = _newcomer_then_moved()
    model, host = "z-ai/glm-5.3-flash", "Newcomer"
    stamps = [_stamp(_at(CUT) - timedelta(seconds=1)), CUT,
              _stamp(_at(LATER) - timedelta(seconds=1)), LATER, None]
    (tmp_path / "pricing.json").write_text(json.dumps(doc), encoding="utf-8")
    shutil.copy(LOADER_JS, tmp_path / "pricing-loader.js")
    shutil.copy(RATES_JS, tmp_path / "rates.js")
    shutil.copy(PARSER_JS, tmp_path / "parser.js")
    got = _node(tmp_path / "parser.js", f"""
      console.log(JSON.stringify({json.dumps(stamps)}.map(
        ts => window.resolveModelRate({json.dumps(model)}, ts, {json.dumps(host)}))));
    """)
    tables = pricing.load_tables(doc)
    saved = {name: getattr(pricing, name) for name in tables}
    try:
        for name, value in tables.items():
            setattr(pricing, name, value)
        want = [pricing.resolve(model, _when(s), host) for s in stamps]
    finally:
        for name, value in saved.items():
            setattr(pricing, name, value)
    assert [(g["kind"], _js_rates(g["rates"])) for g in got] == \
        [(w.kind, w.rates) for w in want]


@needs_node
def test_rate_epochs_include_provider_window_ends_and_row_starts_in_the_browser(
        tmp_path):
    (tmp_path / "pricing.json").write_text(
        json.dumps(_provider_only_doc()), encoding="utf-8")
    shutil.copy(LOADER_JS, tmp_path / "pricing-loader.js")
    shutil.copy(RATES_JS, tmp_path / "rates.js")
    shutil.copy(PARSER_JS, tmp_path / "parser.js")
    got = _node(tmp_path / "parser.js",
                "console.log(JSON.stringify(window.rateEpochs));")
    start = int(_at(P_START).timestamp() * 1000)
    cut = int(_at(P_CUT).timestamp() * 1000)
    assert cut in got, got
    assert start in got, got
    assert got == [start, cut], "the synthetic row is the only one"


@needs_node
@pytest.mark.parametrize("suffix", V_SUFFIXES)
def test_a_variant_suffix_resolves_to_the_bare_row_in_the_browser(
        tmp_path, suffix):
    got = _variant_node(tmp_path, _provider_only_doc(), f"""
      console.log(JSON.stringify({{
        variant: window.resolveModelRate(
          'acme/acme-9{suffix}', {json.dumps(P_CUT)}, 'HostCo'),
        free: window.resolveModelRate(
          'acme/acme-9:free', {json.dumps(P_CUT)}, 'HostCo'),
      }}));
    """)
    assert (got["variant"]["kind"], got["variant"]["key"]) == \
        ("exact", "acme/acme-9")
    assert _js_rates(got["variant"]["rates"]) == P_AFTER
    assert _js_rates(got["variant"]["rates"]) != \
        {f: 0.0 for f in RATE_FIELDS}
    assert (got["free"]["kind"], got["free"]["key"]) == \
        ("exact", "acme/acme-9:free")
    assert _js_rates(got["free"]["rates"]) == {f: 0.0 for f in RATE_FIELDS}


@needs_node
@pytest.mark.parametrize("suffix", V_SUFFIXES)
def test_a_variant_suffix_prices_by_the_bare_row_dated_window_in_the_browser(
        tmp_path, suffix):
    before_cut = _stamp(_at(P_CUT) - timedelta(seconds=1))
    before_start = _stamp(_at(P_START) - timedelta(seconds=1))
    got = _variant_node(tmp_path, _provider_only_doc(), f"""
      console.log(JSON.stringify({{
        before: window.resolveModelRate(
          'acme/acme-9{suffix}', {json.dumps(before_cut)}, 'HostCo'),
        at: window.resolveModelRate(
          'acme/acme-9{suffix}', {json.dumps(P_CUT)}, 'HostCo'),
        beforeStart: window.resolveModelRate(
          'acme/acme-9{suffix}', {json.dumps(before_start)}, 'HostCo'),
        fallback: window.resolveModelRate(
          'acme/acme-9', {json.dumps(before_start)}),
      }}));
    """)
    assert _js_rates(got["before"]["rates"]) == P_BEFORE
    assert _js_rates(got["at"]["rates"]) == P_AFTER
    assert got["beforeStart"] == got["fallback"]
    assert (got["beforeStart"]["kind"], got["beforeStart"]["key"]) == \
        ("default", None)


@needs_node
def test_an_exact_variant_row_wins_over_the_bare_fold_in_the_browser(tmp_path):
    """The fold is a fallback: when the table holds the variant id itself,
    that row is the match. Pins the candidate order (exact id first)."""
    got = _variant_node(tmp_path, _variant_row_doc(), f"""
      console.log(JSON.stringify(window.resolveModelRate(
        'acme/acme-9:nitro', {json.dumps(P_CUT)}, 'HostCo')));
    """)
    assert (got["kind"], got["key"]) == ("exact", "acme/acme-9:nitro")
    assert _js_rates(got["rates"]) == P_AFTER


def test_an_unknown_long_context_member_is_refused_naming_it():
    """pricing.json's long_context_models are dashed keys of the models
    table; a name that is not one is a typo'd data edit the loader
    refuses (SV-RATE-DATA: both loaders refuse a rule-breaking file)."""
    doc = _doc()
    doc["long_context_models"] = ["gpt-5-6-sol", "gpt-9-ghost"]
    with pytest.raises(ValueError, match="gpt-9-ghost"):
        pricing.load_tables(doc)


def test_long_context_models_stay_distinct_and_string_typed():
    doc = _doc()
    doc["long_context_models"] = ["gpt-5-6-sol", "gpt-5-6-sol"]
    with pytest.raises(ValueError, match="distinct"):
        pricing.load_tables(doc)
    doc["long_context_models"] = [42]
    with pytest.raises(ValueError, match="long_context_models"):
        pricing.load_tables(doc)


def test_long_context_meters_default_to_empty_and_load_flat():
    """Absent the key the meter map is empty (a member keeps the global
    threshold); present, the loader folds each {"threshold": N} to the
    flat key -> int table the lookups read (issue #765)."""
    doc = _doc()
    doc["long_context_models"] = ["gpt-5-6-sol"]
    # The live file's meter map rides along in _doc(); the "default"
    # case is a document without the key at all (a refresh fold may
    # have populated it), so drop it here rather than assert against
    # the live map (SV-TEST-DATA).
    doc.pop("long_context_meters", None)
    assert pricing.load_tables(doc)["LONG_CONTEXT_METERS"] == {}
    doc["long_context_meters"] = {"gpt-5-6-sol": {"threshold": 200_000}}
    assert pricing.load_tables(doc)["LONG_CONTEXT_METERS"] == {
        "gpt-5-6-sol": 200_000}
    # An integral float spelling folds to the integer, so both loaders
    # accept the same bytes (JSON has already collapsed the spelling).
    doc["long_context_meters"] = {"gpt-5-6-sol": {"threshold": 200000.0}}
    assert pricing.load_tables(doc)["LONG_CONTEXT_METERS"] == {
        "gpt-5-6-sol": 200_000}


def test_a_long_context_meter_names_a_member():
    doc = _doc()
    doc["long_context_models"] = ["gpt-5-6-sol"]
    doc["long_context_meters"] = {"claude-sonnet-4-5": {"threshold": 200_000}}
    with pytest.raises(ValueError, match="names no long_context_models"):
        pricing.load_tables(doc)


def test_a_long_context_meter_value_is_a_threshold_map():
    doc = _doc()
    doc["long_context_models"] = ["gpt-5-6-sol"]
    for bad in (None, 200_000, {"threshold": 0}, {"threshold": -1},
                {"threshold": True}, {"threshold": "200000"},
                {"threshold": 2.5}, {"threshold": 200_000, "mult": 2.0}, {}):
        doc["long_context_meters"] = {"gpt-5-6-sol": bad}
        with pytest.raises(ValueError, match="long_context_meters"):
            pricing.load_tables(doc)


def test_long_context_meters_must_be_a_map():
    doc = _doc()
    doc["long_context_models"] = ["gpt-5-6-sol"]
    doc["long_context_meters"] = ["gpt-5-6-sol"]
    with pytest.raises(ValueError, match="not a map"):
        pricing.load_tables(doc)
