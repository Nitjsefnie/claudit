"""Shared provider-history fixtures for pricing loader tests."""
from __future__ import annotations

import copy
import json
import shutil
from datetime import timedelta

import pytest
from tests.refresh_fixture_builders import DEFAULT_ROW, seed_doc
from tests.test_pricing_data import (
    CUT, HHMM_JS, LOADER_JS, RATE_FIELDS,
    VENDOR_TABLES_JS, _GLM_MODEL, _at, _copy_browser, _doc,
    _node, _node_raw, _pricing_json, _stamp, _with_newcomer,
)


def _set_rate(value):
    return lambda h: h[-1].update({"read": value})


def _later(**fields):
    return lambda h: h.append({**h[-1], **fields})


DAMAGE = [
    pytest.param(lambda h: h[0].update({"from": CUT}), id="first-has-from"),
    pytest.param(lambda h: h[0].pop("from"), id="first-lacks-from-key"),
    pytest.param(_later(**{"from": None}), id="later-lacks-from"),
    pytest.param(lambda h: h.append(
        {k: v for k, v in h[-1].items() if k != "from"}),
        id="later-lacks-from-key"),
    pytest.param(lambda h: h.extend([{**h[-1], "from": CUT},
                                     {**h[-1], "from": CUT}]),
                 id="from-not-increasing"),
    pytest.param(lambda h: h[-1].pop("read"), id="missing-rate"),
    pytest.param(lambda h: h[-1].update({"reed": 1.0}), id="unknown-field"),
    pytest.param(_later(**{"from": "2031-01-01T00:00:00"}), id="naive-from"),
    pytest.param(_later(**{"from": "2031-01-01 00:00:00Z"}),
                 id="space-separated-from"),
    pytest.param(_later(**{"from": "20310101T000000Z"}), id="basic-format-from"),
    pytest.param(_later(**{"from": 1924992000}), id="numeric-from"),
    pytest.param(_set_rate("0.031"), id="string-rate"),
    pytest.param(_set_rate(None), id="null-rate"),
    pytest.param(_set_rate(-0.01), id="negative-rate"),
    pytest.param(_set_rate(True), id="bool-rate"),
    pytest.param(_set_rate(float("inf")), id="infinite-rate"),
    pytest.param(_set_rate(float("nan")), id="nan-rate"),
    pytest.param(lambda h: h[-1].update({"note": 5}), id="non-string-note"),
    # Well spelled but out of range: V8's Date.parse rolls most of these
    # over (24:00 to the next midnight, 02-30 to 03-02) where Python refuses,
    # and Python reads +14:60 as +15:00 where V8 refuses; both loaders must
    # refuse the same set or one file prices differently on each side.
    *[pytest.param(_later(**{"from": stamp}), id=name) for name, stamp in [
        ("hour-24", "2031-08-21T24:00:00Z"),
        ("february-30", "2031-02-30T00:00:00Z"),
        ("february-29-common-year", "2031-02-29T00:00:00Z"),
        ("april-31", "2031-04-31T12:00:00+02:00"),
        ("month-13", "2031-13-01T00:00:00Z"),
        ("month-0", "2031-00-10T00:00:00Z"),
        ("day-0", "2031-01-00T00:00:00Z"),
        ("minute-60", "2031-01-01T00:60:00Z"),
        ("second-60", "2031-01-01T00:00:60Z"),
        ("offset-24h", "2031-01-01T00:00:00+24:00"),
        ("offset-minute-60", "2031-01-01T00:00:00+14:60"),
        ("year-0", "0000-01-01T00:00:00Z"),
    ]],
]
# JSON has no spelling for these, so the browser refuses them at parse
# time, before any row is read: the error names the file, not the row.
UNSPELLABLE_IN_JSON = {"infinite-rate", "nan-rate"}


def _damaged(damage) -> dict:
    doc = copy.deepcopy(_doc())
    damage(doc["models"]["bonsai-2-27b"])
    return doc


def _node_load(tmp_path, doc: dict) -> str | None:
    """Require the real pricing-loader.js beside `doc`; the load error."""
    (tmp_path / "pricing.json").write_text(json.dumps(doc), encoding="utf-8")
    shutil.copy(LOADER_JS, tmp_path / "pricing-loader.js")
    shutil.copy(VENDOR_TABLES_JS, tmp_path / "vendor-tables.js")
    shutil.copy(HHMM_JS, tmp_path / "hhmm-spelling.js")
    return _node_raw(f"""
      global.window = {{}};
      let error = null;
      try {{ require({str(tmp_path / "pricing-loader.js")!r}); }}
      catch (e) {{ error = e.message; }}
      console.log(JSON.stringify(error));
    """)


def _provider_string_rate() -> dict:
    doc = copy.deepcopy(_doc())
    doc["providers"]["deepseek/deepseek-v4-flash"]["Azure"][-1]["read"] = "0.031"
    return doc


# --- the browser load path ---------------------------------------------------
# node has no document or XMLHttpRequest, so parser.js takes its require path
# there. These stub both, so the path the browser actually runs is exercised.

ORIGIN = "https://claudit.example"


def _browser_load(*, pricing_attr: str | None = None, status: int = 200,
                  body: str | None = None, redirected_to: str | None = None):
    """Load the real pricing-loader.js as a page would: currentScript
    is /src/pricing-loader.js?v=1 on a page at /dashboard/deep/path."""
    dataset = {"pricing": pricing_attr} if pricing_attr else {}
    body = _pricing_json().read_text(encoding="utf-8") if body is None else body
    return _node_raw(f"""
      global.window = {{}};
      const requests = [];
      global.document = {{
        baseURI: {json.dumps(ORIGIN + "/dashboard/deep/path")},
        currentScript: {{ src: {json.dumps(ORIGIN + "/src/pricing-loader.js?v=1")},
                          dataset: {json.dumps(dataset)} }},
      }};
      global.XMLHttpRequest = class {{
        open(method, url, async) {{
          this.url = String(url);
          requests.push({{ method, url: this.url, async, headers: {{}} }});
        }}
        setRequestHeader(name, value) {{
          requests[requests.length - 1].headers[name] = value;
        }}
        send() {{
          this.status = {status};
          this.responseText = {json.dumps(body)};
          this.responseURL = {json.dumps(redirected_to)} || this.url;
        }}
      }};
      let error = null;
      try {{ require({str(LOADER_JS)!r}); }} catch (e) {{ error = e.message; }}
      console.log(JSON.stringify({{
        requests, error,
        fresh: window.keyListRates && window.keyListRates('claude-opus-4-7')
          ? window.keyListRates('claude-opus-4-7').fresh : null,
      }}));
    """)


# Spellings at the edge of the accepted set: both loaders take them, and
# must read each as the same instant.
EDGE_STAMPS = [
    "2032-02-29T00:00:00Z",
    "2033-01-01T00:00:00+23:59",
    "2033-06-01T00:00:00-00:00",
    "2033-12-31T23:59:59+05:30",
    "2034-01-01T00:00:00-12:00",
]


# --- a provider row may begin at a time -------------------------------------
# A host first seen at T was never priced by its own row before T, so the
# row starts there: earlier records from it price by the model alone, as
# they did when they were ingested.

def _model_row_beginning() -> dict:
    doc = copy.deepcopy(_doc())
    doc["models"]["bonsai-2-27b"][0]["from"] = "2020-01-01T00:00:00Z"
    return doc


# --- a provider entry may carry a weekly UTC schedule ------------------------
# OpenRouter lists some hosts at time-of-day prices (pricing.overrides): a
# default price, plus windows by UTC weekday and HHMM time. Both loaders
# resolve a record by its UTC weekday and time: first matching window, else
# the entry's default rates.

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday"]


def _flat(fresh, read, output):
    return {"fresh": fresh, "create_5m": fresh, "create_1h": fresh,
            "read": read, "output": output}


R_WEEKEND = _flat(0.1, 0.01, 0.4)
R_NIGHT = _flat(0.2, 0.02, 0.8)
R_WRAP = _flat(0.3, 0.03, 1.2)
SCHEDULE = [
    {"days": ["saturday", "sunday"], "rates": R_WEEKEND},
    {"days": WEEKDAYS, "start": 0, "end": 100, "rates": R_NIGHT},
    {"start": 2200, "end": 200, "rates": R_WRAP},
]
SCHEDULED = (_GLM_MODEL, "Novita")


def _with_schedule(schedule=None) -> dict:
    doc = copy.deepcopy(_doc())
    model, host = SCHEDULED
    source = doc["providers"][model][host][-1]
    rates = {f: source[f] for f in RATE_FIELDS}
    doc["providers"][model] = {host: [{
        "from": None, **rates,
        "schedule": copy.deepcopy(SCHEDULE if schedule is None else schedule),
    }]}
    return doc


# 2031-01-06 is a Monday.
SCHEDULE_CASES = [
    ("2031-01-04T12:00:00Z", R_WEEKEND),     # Saturday, all day
    ("2031-01-05T23:30:00Z", R_WEEKEND),     # Sunday: first match wins over wrap
    ("2031-01-06T00:00:00Z", R_NIGHT),       # Monday 00:00, window start
    ("2031-01-06T00:59:59Z", R_NIGHT),
    ("2031-01-06T01:00:00Z", R_WRAP),        # night ends exclusive; wrap still open
    ("2031-01-06T01:59:59Z", R_WRAP),
    ("2031-01-06T02:00:00Z", None),          # wrap ends exclusive: default
    ("2031-01-06T21:59:59Z", None),
    ("2031-01-06T22:00:00Z", R_WRAP),        # wrap opens
    ("2031-01-06T23:59:59Z", R_WRAP),
    ("2031-01-07T00:30:00+02:00", R_WRAP),   # Monday 22:30 UTC
    ("2031-01-10T23:59:59Z", R_WRAP),        # Friday night into Saturday
    ("2031-01-11T00:00:00Z", R_WEEKEND),     # Saturday 00:00
]


SCHEDULE_DAMAGE = [
    pytest.param([], id="empty"),
    pytest.param([{"days": ["funday"], "rates": R_NIGHT}], id="unknown-day"),
    pytest.param([{"days": [], "rates": R_NIGHT}], id="no-days"),
    pytest.param([{"days": ["monday", "monday"], "rates": R_NIGHT}], id="repeated-day"),
    pytest.param([{"start": 100, "rates": R_NIGHT}], id="start-without-end"),
    pytest.param([{"start": 160, "end": 200, "rates": R_NIGHT}], id="minute-60"),
    pytest.param([{"start": 2400, "end": 100, "rates": R_NIGHT}], id="hour-24"),
    pytest.param([{"start": 100, "end": 100, "rates": R_NIGHT}], id="empty-window"),
    pytest.param([{"start": "0100", "end": 200, "rates": R_NIGHT}], id="string-time"),
    pytest.param([{"start": 1400.0, "end": 0, "rates": R_NIGHT}], id="float-time"),
    pytest.param([{"days": WEEKDAYS}], id="no-rates"),
    pytest.param([{"rates": {**R_NIGHT, "read": "0.02"}}], id="string-rate"),
    pytest.param([{"rates": R_NIGHT, "min_prompt_tokens": 1000}], id="unknown-key"),
    pytest.param({"rates": R_NIGHT}, id="not-a-list"),
]


def _model_schedule() -> dict:
    doc = copy.deepcopy(_doc())
    doc["models"]["bonsai-2-27b"][-1]["schedule"] = SCHEDULE
    return doc


# --- a row that begins at a time, then moves ---------------------------------

LATER = _stamp(_at(CUT) + timedelta(days=1))
MOVED = {"create_1h": 0.4, "create_5m": 0.4, "fresh": 0.4, "output": 1.8, "read": 0.1}


def _newcomer_then_moved() -> dict:
    doc = _with_newcomer()
    doc["providers"][_GLM_MODEL]["Newcomer"].append({"from": LATER, **MOVED})
    return doc


# --- the loaded rate epochs carry the provider terms -------------------------
# RATE_EPOCHS is the union every read-time fold groups by (SV-DATED-RATES);
# window.rateEpochs is its browser twin. A provider row's window ends and
# its row start must survive into it — the mutant drops the provider terms
# from the union in parser.js. The document carries ONLY the synthetic row,
# so the instants are asserted straight out of the loaded window.rateEpochs,
# never against a parallel derivation of the same union in the test.

P_START = "2026-05-01T00:00:00Z"
P_CUT = "2026-06-15T12:00:00Z"
P_BEFORE = {"fresh": 7.40, "create_5m": 9.25, "create_1h": 14.80,
            "read": 0.74, "output": 37.00}
P_AFTER = {"fresh": 3.20, "create_5m": 4.00, "create_1h": 6.40,
           "read": 0.32, "output": 16.00}


def _provider_only_doc() -> dict:
    """A file whose only row is a synthetic (model, host) pair that begins
    at P_START and moves at P_CUT, at rates unlike any real price (plus the
    claude-opus-4-7 row the default estimate needs, at no cutover)."""
    return seed_doc(providers={"acme/acme-9": {"HostCo": [
        {"from": P_START, **P_BEFORE},
        {"from": P_CUT, **P_AFTER},
    ]}})


# The default-estimate row a synthetic document carries: the shared seed
# literal, copied so DAMAGE variants never mutate the source.
_CLAUDE_DEFAULT_ENTRY = dict(DEFAULT_ROW)


# --- a variant suffix folds to the bare id (issue 72) ------------------------
# A variant suffix (:nitro, :floor) is a service tier, not a price: the
# tiered id resolves to the bare model's row, exactly as pricing.
# _provider_key resolves it. Only :free changes price (zero), and
# resolveModelRate prices it before the provider lookup. Same synthetic
# row, same assertions as tests/test_pricing.py, browser side.

V_SUFFIXES = [":nitro", ":floor"]


def _variant_row_doc() -> dict:
    """The synthetic row, plus a row keyed by the variant id itself: the
    fold must not shadow an exact (model, host) row when the table holds
    one."""
    doc = _provider_only_doc()
    doc["providers"]["acme/acme-9:nitro"] = {"HostCo": [
        {"from": P_START, **P_BEFORE},
        {"from": P_CUT, **P_AFTER},
    ]}
    return doc


def _variant_node(tmp_path, doc: dict, body: str):
    (tmp_path / "pricing.json").write_text(json.dumps(doc), encoding="utf-8")
    _copy_browser(tmp_path)
    return _node(tmp_path / "parser.js", body)
