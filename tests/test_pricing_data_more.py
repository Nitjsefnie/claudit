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

from tests.refresh_fixture_builders import seed_doc
from tests.test_pricing_data import (
    CUT,
    NEWCOMER,
    _GLM_MODEL_ID,
    LOADER_JS,
    VENDOR_TABLES_JS,
    HHMM_JS,
    PARSER_USAGE_JS,
    PARSER_JS,
    RATES_JS,
    RATE_FIELDS,
    _at,
    _doc,
    _js_rates,
    _node,
    _pricing_json,
    _stamp,
    _when,
    _with_newcomer,
    needs_node,
)
from tests.pricing_data_loader_helpers import (
    DAMAGE,
    EDGE_STAMPS,
    LATER,
    MOVED,
    ORIGIN,
    P_AFTER,
    P_BEFORE,
    P_CUT,
    P_START,
    R_NIGHT,
    R_WEEKEND,
    R_WRAP,
    SCHEDULED,
    SCHEDULE_CASES,
    SCHEDULE_DAMAGE,
    UNSPELLABLE_IN_JSON,
    V_SUFFIXES,
    _browser_load,
    _damaged,
    _model_row_beginning,
    _model_schedule,
    _newcomer_then_moved,
    _node_load,
    _provider_only_doc,
    _provider_string_rate,
    _variant_node,
    _variant_row_doc,
    _with_schedule,
)


@pytest.mark.parametrize("damage", DAMAGE)
def test_a_malformed_history_is_refused(damage):
    """A misordered or rewritten history, or a rate that is not a finite
    non-negative number, would silently misprice or crash ingest, so the
    loader refuses it, naming the row, and the suite goes red instead."""
    with pytest.raises(ValueError, match=r"bonsai-2-27b\[\d+\]"):
        pricing.load_tables(_damaged(damage))


@needs_node
@pytest.mark.parametrize("damage", DAMAGE)
def test_a_malformed_history_is_refused_in_the_browser(tmp_path, request, damage):
    error = _node_load(tmp_path, _damaged(damage))
    assert error and error.startswith("pricing.json: "), error
    if request.node.callspec.id not in UNSPELLABLE_IN_JSON:
        assert re.search(r"bonsai-2-27b\[\d+\]", error), error


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
    assert got["fresh"] == pricing._list_rates("claude-opus-4-7")["fresh"]  # pylint: disable=protected-access


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
    doc = seed_doc(
        models={model: [{"from": None, **rates},
                        {"from": stamp, **rates}]},
        fetched="2030-01-01T00:00:00Z",
    )
    want = int(_at(stamp).timestamp() * 1000)
    assert want in [int(e.timestamp() * 1000)
                    for e in pricing.load_tables(doc)["RATE_EPOCHS"]]
    (tmp_path / "pricing.json").write_text(json.dumps(doc), encoding="utf-8")
    shutil.copy(LOADER_JS, tmp_path / "pricing-loader.js")
    shutil.copy(VENDOR_TABLES_JS, tmp_path / "vendor-tables.js")
    shutil.copy(HHMM_JS, tmp_path / "hhmm-spelling.js")
    shutil.copy(RATES_JS, tmp_path / "rates.js")
    shutil.copy(PARSER_USAGE_JS, tmp_path / "parser-usage.js")
    shutil.copy(PARSER_JS, tmp_path / "parser.js")
    assert want in _node(tmp_path / "parser.js",
                         "console.log(JSON.stringify(window.rateEpochs));")


def test_a_provider_row_that_begins_at_a_time_prices_from_then_on(monkeypatch):
    before = _at(CUT) - timedelta(seconds=1)
    fallback = pricing.resolve(_GLM_MODEL_ID, before)
    for name, value in pricing.load_tables(_with_newcomer()).items():
        monkeypatch.setattr(pricing, name, value)
    assert _at(CUT) in pricing.RATE_EPOCHS
    assert pricing.resolve(_GLM_MODEL_ID, before, "Newcomer") == fallback
    assert pricing.rate_for(_GLM_MODEL_ID, _at(CUT), "Newcomer") == NEWCOMER
    assert pricing.rate_for(_GLM_MODEL_ID, None, "Newcomer") == NEWCOMER


@needs_node
def test_a_provider_row_that_begins_at_a_time_prices_from_then_on_in_the_browser(
        tmp_path):
    (tmp_path / "pricing.json").write_text(
        json.dumps(_with_newcomer()), encoding="utf-8")
    shutil.copy(LOADER_JS, tmp_path / "pricing-loader.js")
    shutil.copy(VENDOR_TABLES_JS, tmp_path / "vendor-tables.js")
    shutil.copy(HHMM_JS, tmp_path / "hhmm-spelling.js")
    shutil.copy(RATES_JS, tmp_path / "rates.js")
    shutil.copy(PARSER_USAGE_JS, tmp_path / "parser-usage.js")
    shutil.copy(PARSER_JS, tmp_path / "parser.js")
    before = _stamp(_at(CUT) - timedelta(seconds=1))
    got = _node(tmp_path / "parser.js", f"""
      const m = {json.dumps(_GLM_MODEL_ID)};
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
    with pytest.raises(ValueError, match=r"bonsai-2-27b\[0\]"):
        pricing.load_tables(_model_row_beginning())


@needs_node
def test_a_model_row_cannot_begin_at_a_time_in_the_browser(tmp_path):
    error = _node_load(tmp_path, _model_row_beginning())
    assert error and "bonsai-2-27b[0]" in error, error


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
    shutil.copy(VENDOR_TABLES_JS, tmp_path / "vendor-tables.js")
    shutil.copy(HHMM_JS, tmp_path / "hhmm-spelling.js")
    shutil.copy(RATES_JS, tmp_path / "rates.js")
    shutil.copy(PARSER_USAGE_JS, tmp_path / "parser-usage.js")
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
    with pytest.raises(ValueError, match=r"glm-5-3-flash via Novita\[\d+\]"):
        pricing.load_tables(_with_schedule(schedule))


@needs_node
@pytest.mark.parametrize("schedule", SCHEDULE_DAMAGE)
def test_a_malformed_schedule_is_refused_in_the_browser(tmp_path, schedule):
    error = _node_load(tmp_path, _with_schedule(schedule))
    assert error and ("glm-5-3-flash via Novita[" in error or "spells a schedule" in error), error


def test_a_model_row_cannot_carry_a_schedule():
    with pytest.raises(ValueError, match=r"bonsai-2-27b\[\d+\]"):
        pricing.load_tables(_model_schedule())


@needs_node
def test_a_model_row_cannot_carry_a_schedule_in_the_browser(tmp_path):
    error = _node_load(tmp_path, _model_schedule())
    assert error and "bonsai-2-27b[" in error, error


def test_a_row_that_begins_then_moves_prices_each_span(monkeypatch):
    model, host = _GLM_MODEL_ID, "Newcomer"
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
    model, host = _GLM_MODEL_ID, "Newcomer"
    for name, value in pricing.load_tables(_newcomer_then_moved()).items():
        monkeypatch.setattr(pricing, name, value)
    start = _at(CUT).replace(tzinfo=None)
    before_start = start - timedelta(seconds=1)
    provider_result = pricing.resolve(model, before_start, host)
    bare_result = pricing.resolve(model, before_start)
    assert provider_result == bare_result, "a first-seen host row is inactive before its start"
    assert pricing.rate_for(model, start, host) == NEWCOMER
    assert pricing.rate_for(model, _at(LATER).replace(tzinfo=None), host) == MOVED


@needs_node
def test_a_row_that_begins_then_moves_prices_alike_in_the_browser(tmp_path):
    doc = _newcomer_then_moved()
    model, host = _GLM_MODEL_ID, "Newcomer"
    stamps = [_stamp(_at(CUT) - timedelta(seconds=1)), CUT,
              _stamp(_at(LATER) - timedelta(seconds=1)), LATER, None]
    (tmp_path / "pricing.json").write_text(json.dumps(doc), encoding="utf-8")
    shutil.copy(LOADER_JS, tmp_path / "pricing-loader.js")
    shutil.copy(VENDOR_TABLES_JS, tmp_path / "vendor-tables.js")
    shutil.copy(HHMM_JS, tmp_path / "hhmm-spelling.js")
    shutil.copy(RATES_JS, tmp_path / "rates.js")
    shutil.copy(PARSER_USAGE_JS, tmp_path / "parser-usage.js")
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
    shutil.copy(VENDOR_TABLES_JS, tmp_path / "vendor-tables.js")
    shutil.copy(HHMM_JS, tmp_path / "hhmm-spelling.js")
    shutil.copy(RATES_JS, tmp_path / "rates.js")
    shutil.copy(PARSER_USAGE_JS, tmp_path / "parser-usage.js")
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


def _grouped_meter_doc(groups):
    doc = _doc()
    doc.pop("long_context_models", None)
    doc["long_context_meters"] = groups
    return doc


def test_grouped_long_context_meters_fold_to_whole_entries():
    groups = [
        {"threshold": 200_000,
         "models": ["claude-sonnet-4-5", "gpt-5-4"]},
        {"threshold": 100_000,
         "models": [{"gpt-5-6-sol": {"input_mult": 5.0,
                                      "output_mult": 5}}]},
    ]
    tables = pricing.load_tables(_grouped_meter_doc(groups))
    assert tables["LONG_CONTEXT_METERS"] == {
        "claude-sonnet-4-5": {"threshold": 200_000},
        "gpt-5-4": {"threshold": 200_000},
        "gpt-5-6-sol": {"threshold": 100_000,
                        "input_mult": 5.0, "output_mult": 5},
    }
    assert tables["LONG_CONTEXT_MODELS"] == frozenset({
        "claude-sonnet-4-5", "gpt-5-4", "gpt-5-6-sol"})


def test_grouped_long_context_field_is_required_and_old_field_is_refused():
    doc = _doc()
    doc.pop("long_context_models", None)
    doc.pop("long_context_meters", None)
    with pytest.raises(ValueError, match="long_context_meters.*missing"):
        pricing.load_tables(doc)

    doc = _grouped_meter_doc([])
    doc["long_context_models"] = []
    with pytest.raises(ValueError, match="long_context_models.*removed"):
        pricing.load_tables(doc)


@pytest.mark.parametrize(("groups", "reason"), [
    ([{"threshold": 200_000, "models": ["gpt-5-6-sol"]},
      {"threshold": 200_000, "models": ["gpt-5-4"]}], "duplicate threshold"),
    ([{"threshold": 200_000, "models": []}], "models is empty"),
    ([{"threshold": 200_000, "models": ["gpt-5-6-sol"]},
      {"threshold": 100_000, "models": ["gpt-5-6-sol"]}], "more than once"),
    ([{"threshold": 200_000, "models": ["gpt-9-ghost"]}], "gpt-9-ghost"),
    ([{"models": ["gpt-5-6-sol"]}], "positive integer threshold"),
    ([{"threshold": 0, "models": ["gpt-5-6-sol"]}], "positive integer threshold"),
    ([{"threshold": 200_000, "models": [
        {"gpt-5-6-sol": {"input_mult": 0}}]}], "invalid input_mult"),
    ([{"threshold": 200_000, "models": [
        {"gpt-5-6-sol": {"input_mult": float("inf")}}]}], "invalid input_mult"),
    ([{"threshold": 200_000, "models": [
        {"gpt-5-6-sol": {"mult": 2.0}}]}], "unknown field"),
    ([{"threshold": 200_000, "models": ["gpt-5-6-sol"], "extra": True}],
     "unknown field"),
    ([{"threshold": 200_000, "models": ["gpt-5-6-sol"]},
      {"threshold": 100_000, "models": ["gpt-5-6-sol"]}], "more than once"),
    ({"gpt-5-6-sol": {"threshold": 200_000}}, "list of groups"),
])
def test_grouped_long_context_meter_rules_are_refused(groups, reason):
    with pytest.raises(ValueError, match=reason):
        pricing.load_tables(_grouped_meter_doc(groups))


def _extending_ids() -> list[tuple[str, str]]:
    """(model id, the key it names) where the id matches a longer key AND a
    shorter one: an undashed snapshot suffix is valid after the longer key,
    and after the shorter one the rest still reads as a snapshot
    ("claude-opus-4" + "-1202508"). The keys are the merged view's:
    models-table keys and tracked vendor bare keys alike."""
    keys = [*pricing.MODEL_RATES, *pricing.VENDOR_BARE]
    return [(longer + "202508", longer)
            for shorter in keys for longer in keys
            if longer != shorter and longer.startswith(shorter)]


def test_the_longest_matching_key_wins_in_the_backend():
    """The file is sorted, which puts every key AFTER the shorter key it
    extends, so matching must not take the first key that fits."""
    assert list(pricing.MODEL_RATES) == list(_doc()["models"])
    cases = _extending_ids()
    assert cases, "the table has keys that extend other keys"
    for model, key in cases:
        assert pricing.resolve(model).key == key, model


@needs_node
def test_the_longest_matching_key_wins_in_the_browser():
    cases = _extending_ids()
    got = _node(PARSER_JS, f"""
      const ids = {json.dumps([m for m, _ in cases])};
      console.log(JSON.stringify(ids.map(m => window.resolveModelRate(m).key)));
    """)
    assert got == [key for _, key in cases]


@needs_node
def test_both_sides_resolve_the_dotted_gpt_6_1_sol_id_to_its_own_row():
    """issue #357: the dotted transcript id and its dashed form both hit
    the new exact key in the browser too — never the shorter gpt-6-sol
    row — at the rates the committed file defines, priced identically on
    both sides. The ids are literals in a plain list, not rate-call
    arguments, so no row value is pinned."""
    ids = ["gpt-6.1-sol", "gpt-6-1-sol", "gpt-6-sol"]
    got = _node(PARSER_JS, f"""
      const ids = {json.dumps(ids)};
      console.log(JSON.stringify(ids.map(m => window.resolveModelRate(m))));
    """)
    assert [(g["kind"], g["key"]) for g in got] == [
        ("exact", "gpt-6-1-sol"), ("exact", "gpt-6-1-sol"),
        ("exact", "gpt-6-sol")]
    for g, w in zip(got, (pricing.resolve(m) for m in ids), strict=True):
        assert _js_rates(g["rates"]) == w.rates
