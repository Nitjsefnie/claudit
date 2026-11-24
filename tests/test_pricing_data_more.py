from __future__ import annotations

import contextlib
import functools
import json
import re
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

from backend import app as app_mod
from backend import pricing, pricing_load
from backend import session as session_mod
from backend.pricing_document import serialize_pricing_doc
from backend.pricing_rows import encode_pricing_doc

from tests.refresh_fixture_builders import DEFAULT_ROW, seed_doc
from tests.test_pricing_vendor import _install
from tests.test_pricing_data import (
    CUT,
    NEWCOMER,
    _GLM_MODEL_ID,
    PARSER_JS,
    RATE_FIELDS,
    ROOT,
    _at,
    _copy_browser,
    _doc,
    _js_rates,
    _node,
    _pricing_json,
    _stamp,
    _trimmed,
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


_TABLE_NAMES = pricing.RateTables.__annotations__.keys()

# Factory, backend row matcher, browser row matcher, browser file-prefix check.
REFUSALS = [
    *[pytest.param(lambda case=case: _damaged(case.values[0]), r"bonsai-2-27b\[\d+\]",
                   None if case.id in UNSPELLABLE_IN_JSON
                   else r"bonsai-2-27b\[\d+\]", True, id=f"history-{case.id}")
      for case in DAMAGE],
    *[pytest.param(lambda case=case: _with_schedule(case.values[0]),
                   r"glm-5-3-flash via Novita\[\d+\]",
                   r"glm-5-3-flash via Novita\[|spells a schedule", False,
                   id=f"schedule-{case.id}") for case in SCHEDULE_DAMAGE],
    pytest.param(_provider_string_rate, "fixture/provider via Fixture",
                 "fixture/provider via Fixture", False, id="provider-string-rate"),
    pytest.param(_model_schedule, r"bonsai-2-27b\[\d+\]",
                 r"bonsai-2-27b\[", False, id="model-schedule"),
    pytest.param(lambda: seed_doc(models={"": [dict(DEFAULT_ROW)]}), r"models key ''",
                 None, False, id="empty-model-key"),
]


@pytest.mark.parametrize("factory, match", [
    pytest.param(case.values[0], case.values[1], id=case.id) for case in REFUSALS
])
def test_invalid_pricing_data_is_refused_naming_the_row(factory, match):
    with pytest.raises(ValueError, match=match):
        pricing.load_tables(factory())


@needs_node
@pytest.mark.parametrize("factory, match, file_prefix", [
    pytest.param(case.values[0], case.values[2], case.values[3], id=case.id)
    for case in REFUSALS if case.id != "empty-model-key"
])
def test_invalid_pricing_data_is_refused_in_the_browser(
        tmp_path, factory, match, file_prefix):
    error = _node_load(tmp_path, factory())
    assert error, error
    if file_prefix:
        assert error.startswith("pricing.json: "), error
    if match is not None:
        assert re.search(match, error), error


# The parse forkserver's preload is the one module that binds at import.
_IMPORT_THEN_READ = """
import importlib, pkgutil, sys, backend
from unittest.mock import Mock
from tests import bounded_pricing
seed = bounded_pricing.pricing_load.PRICING_JSON
read = seed.read_text = Mock(wraps=seed.read_text)
for module in pkgutil.walk_packages(backend.__path__, "backend."):
    if module.name != "backend.ingest_forkserver":
        importlib.import_module(module.name)
counts = [read.call_count]
for name in ("MODEL_RATES", "TRACKED_MODELS", "MODEL_RATES"):
    getattr(sys.modules["backend.pricing"], name)
    counts.append(read.call_count)
importlib.import_module("backend.ingest_forkserver")
print("backend.app" in sys.modules, *counts, read.call_count)
"""


def test_imports_read_no_rate_data_and_the_first_table_read_loads_it_once():
    done = subprocess.run([sys.executable, "-c", _IMPORT_THEN_READ], cwd=ROOT,
                          capture_output=True, text=True, check=False, timeout=60)
    assert done.stdout.split() == ["True", "0", "1", "1", "1", "1"], done.stderr


def test_a_python_child_is_refused_the_deployed_document():
    done = subprocess.run([sys.executable, "-c", "from backend import pricing; pricing.bind_tables()"], cwd=ROOT,
                          capture_output=True, text=True, check=False, timeout=60)
    assert done.returncode and "a test child opened the deployed rate document" in done.stderr


@pytest.fixture(name="unbound_document")
def _unbound_document_fixture(monkeypatch, tmp_path):
    """A fresh process's state: no table bound, over a document of the test's own."""
    for name in _TABLE_NAMES:
        monkeypatch.delattr(pricing, name)
    document = tmp_path / "pricing.json"
    document.write_text(serialize_pricing_doc(seed_doc()), encoding="utf-8", newline="\n")
    monkeypatch.setattr(pricing_load, "PRICING_JSON", document)
    monkeypatch.setattr(pricing, "deployed_tables", functools.cache(pricing_load.deployed_tables.__wrapped__))
    return document


@pytest.mark.asyncio
async def test_startup_refuses_a_bad_document_before_the_database(unbound_document, monkeypatch):
    good = unbound_document.read_text(encoding="utf-8")
    # Decoded rows: valid for load_tables, refused at the storage boundary.
    unbound_document.write_text(json.dumps(seed_doc()), encoding="utf-8", newline="\n")
    monkeypatch.setattr(app_mod.db, "apply_schema", Mock(side_effect=RuntimeError("database reached")))
    for _ in range(2):
        with pytest.raises(ValueError, match="_format"):
            async with app_mod.lifespan(app_mod.app):
                pass
        with pytest.raises(ValueError, match="_format"):
            pricing.resolve("claude-opus-4-7")
    assert not _TABLE_NAMES & vars(pricing).keys()
    unbound_document.write_text(good, encoding="utf-8", newline="\n")
    with pytest.raises(RuntimeError, match="database reached"):
        async with app_mod.lifespan(app_mod.app):
            pass
    assert _TABLE_NAMES <= vars(pricing).keys()


def test_a_table_patched_before_its_first_use_is_restored_to_the_deployed_one(unbound_document):
    want = pricing.load_tables(json.loads(unbound_document.read_text(encoding="utf-8")))
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(pricing, "MODEL_RATES", {})
        assert pricing.resolve("claude-opus-4-7").kind == "default"
    with patch.object(pricing, "DATED_RATES", {}) as dated:
        assert pricing.DATED_RATES is dated
    for name, value in want.items():
        assert getattr(pricing, name) == value
        assert getattr(pricing, name) is getattr(pricing, name)
    with pytest.raises(AttributeError, match="module 'backend.pricing' has no attribute 'NO_SUCH_TABLE'"):
        getattr(pricing, "NO_SUCH_TABLE")


def test_racing_first_uses_load_the_document_once(unbound_document, monkeypatch):
    loads, both_loading = [], threading.Barrier(2)

    def load():
        loads.append(threading.get_ident())
        with contextlib.suppress(threading.BrokenBarrierError):
            both_loading.wait(timeout=0.2)
        return pricing_load.deployed_tables.__wrapped__()

    monkeypatch.setattr(pricing, "deployed_tables", functools.cache(load))
    with ThreadPoolExecutor(2) as pool:
        first, second = pool.map(lambda _: pricing.MODEL_RATES, range(2))
    assert first is second is pricing.MODEL_RATES and len(loads) == 1


@needs_node
@pytest.mark.parametrize("attr, url", [
    pytest.param("/src/pricing.json?v=7", "/src/pricing.json?v=7", id="page-url"),
    pytest.param(None, "/src/pricing.json", id="beside-script"),
])
def test_the_browser_fetches_pricing_without_blocking(attr, url):  # live document: compares a live rate with the backend's
    got = _browser_load(pricing_attr=attr)
    assert got["error"] is None
    assert got["requests"] == [{
        "url": ORIGIN + url, "options": {"headers": {"Cache-Control": "no-cache"}},
    }]
    assert got["installedAtRequire"] is False
    assert got["fresh"] == pricing._list_rates("claude-opus-4-7")["fresh"]  # pylint: disable=protected-access


@needs_node
@pytest.mark.parametrize("response, reason", [
    pytest.param({"status": 404}, "HTTP 404", id="not-found"),
    pytest.param({"status": 200, "body": "<!doctype html><title>Sign in</title>",
                  "redirected_to": ORIGIN + "/login"},
                 "redirected to " + ORIGIN + "/login", id="signed-out"),
    pytest.param({"status": 200, "body": "{not json"}, "not JSON", id="garbled"),
    pytest.param({"fail": "network"}, "Failed to fetch", id="network"),
    pytest.param({"fail": "read"}, "body stream interrupted", id="body-read"),
])
def test_a_failed_browser_load_throws_naming_the_file(response, reason):
    """No rates means no honest price, so the load rejects rather than
    priming the resolver with nothing; the error names the file and
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
                        {"from": stamp, **rates,
                         "read": 0.8}]},
        fetched=stamp,
    )
    want = int(_at(stamp).timestamp() * 1000)
    assert want in [int(e.timestamp() * 1000)
                    for e in pricing.load_tables(doc)["RATE_EPOCHS"]]
    (tmp_path / "pricing.json").write_text(json.dumps(encode_pricing_doc(doc)), encoding="utf-8")
    _copy_browser(tmp_path)
    assert want in _node(tmp_path / "parser.js",
                         "console.log(JSON.stringify(window.rateEpochs));")


@needs_node
def test_a_provider_row_that_begins_at_a_time_prices_from_then_on_in_the_browser(
        tmp_path):
    (tmp_path / "pricing.json").write_text(
        json.dumps(encode_pricing_doc(_with_newcomer())), encoding="utf-8")
    _copy_browser(tmp_path)
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


@pytest.mark.parametrize("stamp, want", [
    *SCHEDULE_CASES,
    pytest.param(datetime(2031, 1, 4, 12), R_WEEKEND, id="naive-utc"),
    pytest.param(None, None, id="no-timestamp"),
])
def test_a_scheduled_entry_prices_by_utc_weekday_and_time(monkeypatch, stamp, want):
    model, host = SCHEDULED
    default = {f: _trimmed()["providers"][model][host][-1][f] for f in RATE_FIELDS}
    _install(monkeypatch, _with_schedule())
    when = _at(stamp) if isinstance(stamp, str) else stamp
    assert pricing.rate_for(model, when, host) == (want or default)


@needs_node
def test_both_sides_price_a_schedule_identically_across_the_week(tmp_path, monkeypatch):
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
    (tmp_path / "pricing.json").write_text(json.dumps(encode_pricing_doc(doc)), encoding="utf-8")
    _copy_browser(tmp_path)
    got = _node(tmp_path / "parser.js", f"""
      const stamps = {json.dumps(stamps)};
      console.log(JSON.stringify(stamps.map(
        ts => window.rateForModel({json.dumps(model)}, ts, {json.dumps(host)}))));
    """)
    _install(monkeypatch, doc)
    want = [pricing.rate_for(model, _at(s), host) for s in stamps]
    assert [_js_rates(r) for r in got] == want
    assert {json.dumps(w, sort_keys=True) for w in want} >= {
        json.dumps(r, sort_keys=True) for r in (R_WEEKEND, R_NIGHT, R_WRAP)}


def test_a_row_that_begins_then_moves_prices_each_span(monkeypatch):
    model, host = _GLM_MODEL_ID, "Newcomer"
    before = _at(CUT) - timedelta(seconds=1)
    _install(monkeypatch, _newcomer_then_moved())
    fallback = pricing.resolve(model, before)
    assert {_at(CUT), _at(LATER)} <= set(pricing.RATE_EPOCHS)
    assert pricing.resolve(model, before, host) == fallback
    assert pricing.rate_for(model, _at(CUT), host) == NEWCOMER
    assert pricing.rate_for(model, _at(LATER) - timedelta(seconds=1), host) == NEWCOMER
    assert pricing.rate_for(model, _at(LATER), host) == MOVED
    assert pricing.rate_for(model, None, host) == MOVED


def test_a_naive_timestamp_against_a_row_that_begins_is_read_as_utc(monkeypatch):
    model, host = _GLM_MODEL_ID, "Newcomer"
    _install(monkeypatch, _newcomer_then_moved())
    start = _at(CUT).replace(tzinfo=None)
    before_start = start - timedelta(seconds=1)
    provider_result = pricing.resolve(model, before_start, host)
    bare_result = pricing.resolve(model, before_start)
    assert provider_result == bare_result, "a first-seen host row is inactive before its start"
    assert pricing.rate_for(model, start, host) == NEWCOMER
    assert pricing.rate_for(model, _at(LATER).replace(tzinfo=None), host) == MOVED


@needs_node
def test_rate_epochs_include_provider_window_ends_and_row_starts_in_the_browser(
        tmp_path):
    (tmp_path / "pricing.json").write_text(
        json.dumps(encode_pricing_doc(_provider_only_doc())), encoding="utf-8")
    _copy_browser(tmp_path)
    got = _node(tmp_path / "parser.js",
                "console.log(JSON.stringify(window.rateEpochs));")
    start = int(_at(P_START).timestamp() * 1000)
    cut = int(_at(P_CUT).timestamp() * 1000)
    assert cut in got, got
    assert start in got, got
    assert got == [start, cut], "the synthetic row is the only one"


@needs_node
@pytest.mark.parametrize("factory, suffix, key", [
    *[pytest.param(_provider_only_doc, suffix, "acme/acme-9", id=suffix[1:])
      for suffix in V_SUFFIXES],
    pytest.param(_variant_row_doc, ":nitro", "acme/acme-9:nitro", id="exact-variant"),
])
def test_browser_variants_keep_the_matching_rows_and_dated_windows(
        tmp_path, factory, suffix, key):
    before_cut = _stamp(_at(P_CUT) - timedelta(seconds=1))
    before_start = _stamp(_at(P_START) - timedelta(seconds=1))
    got = _variant_node(tmp_path, factory(), f"""
      console.log(JSON.stringify({{
        before: window.resolveModelRate(
          'acme/acme-9{suffix}', {json.dumps(before_cut)}, 'HostCo'),
        at: window.resolveModelRate(
          'acme/acme-9{suffix}', {json.dumps(P_CUT)}, 'HostCo'),
        free: window.resolveModelRate(
          'acme/acme-9:free', {json.dumps(P_CUT)}, 'HostCo'),
        beforeStart: window.resolveModelRate(
          'acme/acme-9{suffix}', {json.dumps(before_start)}, 'HostCo'),
        fallback: window.resolveModelRate(
          'acme/acme-9', {json.dumps(before_start)}),
      }}));
    """)
    assert (got["at"]["kind"], got["at"]["key"]) == ("exact", key)
    assert _js_rates(got["at"]["rates"]) == P_AFTER
    assert _js_rates(got["at"]["rates"]) != {f: 0.0 for f in RATE_FIELDS}
    assert (got["free"]["kind"], got["free"]["key"]) == ("exact", "acme/acme-9:free")
    assert _js_rates(got["free"]["rates"]) == {f: 0.0 for f in RATE_FIELDS}
    assert _js_rates(got["before"]["rates"]) == P_BEFORE
    assert got["beforeStart"] == got["fallback"]
    assert (got["beforeStart"]["kind"], got["beforeStart"]["key"]) == ("default", None)


_METER_MODEL_A = "meter-model-a"
_METER_MODEL_B = "meter-model-b"
_METER_MODEL_C = "meter-model-c"


def _grouped_meter_doc(groups):
    doc = seed_doc(models={key: [dict(DEFAULT_ROW)] for key in (
        _METER_MODEL_A, _METER_MODEL_B, _METER_MODEL_C)})
    doc["long_context_meters"] = groups
    return doc


def test_grouped_long_context_meters_fold_to_whole_entries():
    groups = [
        {"threshold": 200_000,
         "models": [_METER_MODEL_A, _METER_MODEL_B]},
        {"threshold": 100_000,
         "models": [{_METER_MODEL_C: {"input_mult": 5.0,
                                      "output_mult": 5}}]},
    ]
    tables = pricing.load_tables(_grouped_meter_doc(groups))
    assert tables["LONG_CONTEXT_METERS"] == {
        _METER_MODEL_A: {"threshold": 200_000},
        _METER_MODEL_B: {"threshold": 200_000},
        _METER_MODEL_C: {"threshold": 100_000,
                         "input_mult": 5.0, "output_mult": 5},
    }
    assert tables["LONG_CONTEXT_MODELS"] == frozenset({
        _METER_MODEL_A, _METER_MODEL_B, _METER_MODEL_C})


def test_grouped_long_context_field_is_required_and_old_field_is_refused():
    doc = _grouped_meter_doc([])
    doc.pop("long_context_meters", None)
    with pytest.raises(ValueError, match="long_context_meters.*missing"):
        pricing.load_tables(doc)

    doc = _grouped_meter_doc([])
    doc["long_context_models"] = []
    with pytest.raises(ValueError, match="long_context_models.*removed"):
        pricing.load_tables(doc)


@pytest.mark.parametrize(("groups", "reason"), [
    pytest.param([None], "group is not an object", id="null-group"),
    pytest.param([42], "group is not an object", id="scalar-group"),
    pytest.param([{"threshold": 10**400, "models": [_METER_MODEL_A]}],
                 "positive integer threshold", id="overflowing-threshold"),
    pytest.param([{"threshold": 200_000, "models": [{"": {}}]}],
                 "models entries need non-empty model keys",
                 id="empty-object-model-key"),
    pytest.param([{"threshold": 200_000, "models": [""]}],
                 "models entries need non-empty model keys",
                 id="empty-bare-model-key"),
    pytest.param([{"threshold": 200_000, "models": [
        {_METER_MODEL_A: {"input_mult": 10**400}}]}],
                 "invalid input_mult", id="overflowing-input-multiplier"),
    ([{"threshold": 200_000, "models": [_METER_MODEL_A]},
      {"threshold": 200_000, "models": [_METER_MODEL_B]}], "duplicate threshold"),
    ([{"threshold": 200_000, "models": []}], "models is empty"),
    ([{"threshold": 200_000, "models": [_METER_MODEL_A]},
      {"threshold": 100_000, "models": [_METER_MODEL_A]}], "more than once"),
    ([{"threshold": 200_000, "models": ["unknown-meter-model"]}],
     "unknown-meter-model"),
    ([{"models": [_METER_MODEL_A]}], "positive integer threshold"),
    ([{"threshold": 0, "models": [_METER_MODEL_A]}], "positive integer threshold"),
    ([{"threshold": 200_000, "models": [42]}],
     "each model is a key or a one-key"),
    ([{"threshold": 200_000, "models": [[]]}],
     "each model is a key or a one-key"),
    ([{"threshold": 200_000, "models": [{}]}],
     "each model is a key or a one-key"),
    ([{"threshold": 200_000, "models": [
        {_METER_MODEL_A: {}, _METER_MODEL_B: {}}]}],
     "each model is a key or a one-key"),
    ([{"threshold": 200_000, "models": [
        {_METER_MODEL_C: 5.0}]}], "multipliers are not an object"),
    ([{"threshold": 200_000, "models": [
        {_METER_MODEL_C: []}]}], "multipliers are not an object"),
    ([{"threshold": 200_000, "models": [
        {_METER_MODEL_C: {"input_mult": 0}}]}], "invalid input_mult"),
    ([{"threshold": 200_000, "models": [
        {_METER_MODEL_C: {"output_mult": -1}}]}], "invalid output_mult"),
    ([{"threshold": 200_000, "models": [
        {_METER_MODEL_C: {"input_mult": True}}]}], "invalid input_mult"),
    ([{"threshold": 200_000, "models": [
        {_METER_MODEL_C: {"input_mult": "5"}}]}], "invalid input_mult"),
    ([{"threshold": 200_000, "models": [
        {_METER_MODEL_C: {"input_mult": float("inf")}}]}], "invalid input_mult"),
    ([{"threshold": 200_000, "models": [
        {_METER_MODEL_C: {"mult": 2.0}}]}], "unknown field"),
    ([{"threshold": 200_000, "models": [_METER_MODEL_A], "extra": True}],
     "unknown field"),
    ({_METER_MODEL_A: {"threshold": 200_000}}, "list of groups"),
])
def test_grouped_long_context_meter_rules_are_refused(groups, reason):
    with pytest.raises(ValueError, match=reason):
        pricing.load_tables(_grouped_meter_doc(groups))


def _extending_ids() -> list[tuple[str, str | None]]:
    """(model id, priced key or None) for ids matching overlapping keys.
    The merged view includes models-table and tracked vendor bare keys.
    Syntactic matching alone does not supply a first-party price: id-only
    entries resolve provider histories through their tracked key and have
    no vendor_host fallback. Unpriced ids name no resolved key."""
    keys = [*pricing.MODEL_RATES, *pricing.VENDOR_BARE]
    # SV-RATE-ESTIMATES: "EXACT allows only a dated-snapshot (`-YYYYMMDD`)
    # or bracket (`[1m]`) suffix after a key." The undashed six-digit
    # extension below exercises syntactic matching without promising a
    # first-party rate for an id-only key with no vendor_host fallback.
    priced = {*pricing.MODEL_RATES,
              *(bare for bare, tracked in pricing.VENDOR_BARE.items()
                if tracked in pricing.VENDOR_HOSTS
                and (tracked, pricing.VENDOR_HOSTS[tracked])
                in pricing.PROVIDER_RATES)}
    return [(longer + "202508", longer if longer in priced else None)
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
@pytest.mark.parametrize("cases, kind", [
    pytest.param(_extending_ids(), None, id="extending-keys"),
    pytest.param([("gpt-6.1-sol", "gpt-6-1-sol"),
                  ("gpt-6-1-sol", "gpt-6-1-sol"),
                  ("gpt-6-sol", "gpt-6-sol")], "exact", id="dotted-sol"),
])
def test_browser_ids_resolve_to_their_own_rows(cases, kind):  # live document: the rows are live and compared with the backend's
    ids = [model for model, _ in cases]
    got = _node(PARSER_JS, f"""
      const ids = {json.dumps(ids)};
      console.log(JSON.stringify(ids.map(m => window.resolveModelRate(m))));
    """)
    assert [g["key"] for g in got] == [key for _, key in cases]
    if kind is not None:
        assert [g["kind"] for g in got] == [kind] * len(cases)
    for g, w in zip(got, (pricing.resolve(m) for m in ids), strict=True):
        assert _js_rates(g["rates"]) == w.rates
