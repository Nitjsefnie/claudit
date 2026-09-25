"""SV-RATE-DATA: every rate lives in src/pricing.json, read by both sides.

backend/pricing.py and src/parser.js hold resolution logic only. These
tests pin the three properties that make the file the single source: both
sides price every row the file defines identically and exactly as the file
says, a price change is recorded by APPENDING an entry to a row's history,
and the file stays in the canonical layout an automated writer reproduces.
"""
import copy
import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend import app as app_mod
from backend import pricing
from backend import session as session_mod

ROOT = Path(__file__).resolve().parents[1]
PARSER_JS = ROOT / "src" / "parser.js"
PRICING_JSON = ROOT / "src" / "pricing.json"
PRICING_PY = ROOT / "backend" / "pricing.py"
RATE_FIELDS = ("fresh", "create_5m", "create_1h", "read", "output")
JS_FIELDS = {"fresh": "fresh", "c5": "create_5m", "c1h": "create_1h",
             "read": "read", "out": "output"}

needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available"
)


def _doc() -> dict:
    return json.loads(PRICING_JSON.read_text(encoding="utf-8"))


def _at(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp)


def _when(stamp: str | None) -> datetime | None:
    return _at(stamp) if stamp else None


def _stamp(when: datetime) -> str:
    return when.isoformat().replace("+00:00", "Z")


def _histories(doc: dict):
    """(model, provider or None, history) for every row the file defines."""
    for model, history in doc["models"].items():
        yield model, None, history
    for model, hosts in doc["providers"].items():
        for host, history in hosts.items():
            yield model, host, history


def _in_force(history: list[dict], stamp: str | None) -> dict:
    """The rates the file itself says apply at `stamp`: the newest entry
    whose `from` is not after it, or the newest entry when no stamp."""
    when = _when(stamp)
    current = history[0]
    for entry in history[1:]:
        if when is None or _at(entry["from"]) <= when:
            current = entry
    return {f: current[f] for f in RATE_FIELDS}


def _cases(doc: dict) -> list[tuple[str, str | None, str | None]]:
    """Every row at list price and one second either side of, and exactly
    at, every cutover any row in the file carries."""
    cutovers = sorted({
        _at(entry["from"])
        for _, _, history in _histories(doc)
        for entry in history if entry["from"]
    })
    stamps: list[str | None] = [None]
    for cut in cutovers:
        stamps += [_stamp(cut - timedelta(seconds=1)), _stamp(cut),
                   _stamp(cut + timedelta(seconds=1))]
    return [(model, stamp, host)
            for model, host, _ in _histories(doc) for stamp in stamps]


def _node_raw(script: str):
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60,
        # Return code checked by hand on the next line.
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _node(parser_js, body: str):
    return _node_raw(f"""
      global.window = {{}};
      require({str(parser_js)!r});
      {body}
    """)


def _js_rates(js: dict) -> dict:
    return {py: js[k] for k, py in JS_FIELDS.items()}


def test_the_file_has_every_cutover_the_backend_prices_by():
    doc = _doc()
    assert [_stamp(e) for e in pricing.RATE_EPOCHS] == sorted({
        entry["from"] for _, _, history in _histories(doc)
        for entry in history if entry["from"]
    })
    assert pricing.PROVIDER_RATES_FETCHED == _at(doc["provider_rates_fetched"])


def test_the_backend_prices_every_row_as_the_file_says():
    doc = _doc()
    histories = {(m, h): history for m, h, history in _histories(doc)}
    for model, stamp, host in _cases(doc):
        got = pricing.resolve(model, _when(stamp), host)
        label = f"{model} via {host} @ {stamp}"
        assert got.kind == "exact", label
        assert got.rates == _in_force(histories[model, host], stamp), label


@needs_node
def test_both_sides_resolve_every_row_the_file_defines_identically():
    cases = _cases(_doc())
    got_all = _node(PARSER_JS, f"""
      const cases = {json.dumps(cases)};
      console.log(JSON.stringify(cases.map(([m, ts, p]) =>
        window.resolveModelRate(m, ts, p))));
    """)
    for (model, stamp, host), got in zip(cases, got_all, strict=True):
        want = pricing.resolve(model, _when(stamp), host)
        label = f"{model} via {host} @ {stamp}"
        assert (got["kind"], got["key"]) == (want.kind, want.key), label
        assert _js_rates(got["rates"]) == want.rates, label


@needs_node
def test_both_sides_derive_the_same_tables_in_the_same_order():
    got = _node(PARSER_JS, """
      console.log(JSON.stringify({
        models: Object.entries(window.modelRates),
        dated: Object.entries(window.datedRates),
        providers: window.providerRates,
        providerDated: window.providerDatedRates,
      }));
    """)
    assert [(k, _js_rates(r)) for k, r in got["models"]] == \
        list(pricing.MODEL_RATES.items())
    assert {k: [(w["endExclusive"], _js_rates(w["rates"])) for w in ws]
            for k, ws in got["dated"]} == \
        {k: [(int(end.timestamp() * 1000), r) for end, r in ws]
         for k, ws in pricing.DATED_RATES.items()}
    assert {(m, h): _js_rates(r)
            for m, hosts in got["providers"].items()
            for h, r in hosts.items()} == pricing.PROVIDER_RATES
    assert {(m, h): [(w["endExclusive"], _js_rates(w["rates"])) for w in ws]
            for m, hosts in got["providerDated"].items()
            for h, ws in hosts.items()} == \
        {k: [(int(end.timestamp() * 1000), r) for end, r in ws]
         for k, ws in pricing.PROVIDER_DATED_RATES.items()}


def _extending_ids() -> list[tuple[str, str]]:
    """(model id, the key it names) where the id matches a longer key AND a
    shorter one: an undashed snapshot suffix is valid after the longer key,
    and after the shorter one the rest still reads as a snapshot
    ("claude-opus-4" + "-1202508")."""
    keys = list(pricing.MODEL_RATES)
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


def test_the_file_is_in_canonical_layout():
    """Sorted keys, two-space indent, one field per line: the layout
    json.dumps(doc, indent=2, sort_keys=True) writes, so an automated
    refresh rewrites the file byte-for-byte except for what moved."""
    text = PRICING_JSON.read_text(encoding="utf-8")
    assert text == json.dumps(json.loads(text), indent=2, sort_keys=True) + "\n"


def test_neither_side_carries_a_rate_literal():
    """A second copy of a rate is what the file replaced. The cache-write
    field is unique to rate rows, so a literal one is a copied row."""
    assert not re.search(r"""["']create_5m["']\s*:\s*[\d.]""",
                         PRICING_PY.read_text(encoding="utf-8"))
    assert not re.search(r"\bc5\s*:\s*[\d.]",
                         PARSER_JS.read_text(encoding="utf-8"))


# --- history is append-only --------------------------------------------------

CUT = "2031-01-01T00:00:00Z"
APPEND_ROWS = [
    pytest.param(("models", "glm-5-3-flash"), "glm-5.3-flash", None,
                 id="model"),
    pytest.param(("providers", "deepseek/deepseek-v4-1-flash", "Novita"),
                 "deepseek/deepseek-v4.1-flash", "Novita", id="provider"),
]


def _appended(path: tuple[str, ...]) -> tuple[dict, dict, dict]:
    """The file with one entry appended to the row at `path`: the new
    document, the rates in force before CUT, and the appended rates."""
    doc = copy.deepcopy(_doc())
    history: Any = doc
    for part in path:
        history = history[part]
    before = {f: history[-1][f] for f in RATE_FIELDS}
    after = {f: round(v * 3 + 1, 6) for f, v in before.items()}
    history.append({"from": CUT, **after})
    return doc, before, after


@pytest.mark.parametrize("path, model, host", APPEND_ROWS)
def test_an_appended_entry_prices_from_its_cutover_on_in_the_backend(
        monkeypatch, path, model, host):
    doc, before, after = _appended(path)
    cut = _at(CUT)
    earlier = {epoch: pricing.rate_for(model, epoch - timedelta(seconds=1), host)
               for epoch in pricing.RATE_EPOCHS}
    for name, value in pricing.load_tables(doc).items():
        monkeypatch.setattr(pricing, name, value)
    assert pricing.RATE_EPOCHS == sorted([*earlier, cut])
    assert pricing.rate_for(model, cut - timedelta(seconds=1), host) == before
    assert pricing.rate_for(model, cut, host) == after
    assert pricing.rate_for(model, None, host) == after, "no ts => newest"
    for epoch, want in earlier.items():
        assert pricing.rate_for(model, epoch - timedelta(seconds=1), host) == want


@needs_node
@pytest.mark.parametrize("path, model, host", APPEND_ROWS)
def test_an_appended_entry_prices_from_its_cutover_on_in_the_browser(
        tmp_path, path, model, host):
    doc, before, after = _appended(path)
    (tmp_path / "pricing.json").write_text(json.dumps(doc), encoding="utf-8")
    shutil.copy(PARSER_JS, tmp_path / "parser.js")
    cut = _at(CUT)
    stamps = [_stamp(cut - timedelta(seconds=1)), CUT, None]
    got = _node(tmp_path / "parser.js", f"""
      const stamps = {json.dumps(stamps)};
      console.log(JSON.stringify({{
        rates: stamps.map(ts => window.rateForModel(
          {json.dumps(model)}, ts, {json.dumps(host)})),
        epochs: window.rateEpochs,
      }}));
    """)
    assert [_js_rates(r) for r in got["rates"]] == [before, after, after]
    assert int(cut.timestamp() * 1000) in got["epochs"]


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
    damage(doc["models"]["glm-5-3-flash"])
    return doc


@pytest.mark.parametrize("damage", DAMAGE)
def test_a_malformed_history_is_refused(damage):
    """A misordered or rewritten history, or a rate that is not a finite
    non-negative number, would silently misprice or crash ingest, so the
    loader refuses it, naming the row, and the suite goes red instead."""
    with pytest.raises(ValueError, match=r"glm-5-3-flash\[\d+\]"):
        pricing.load_tables(_damaged(damage))


def _node_load(tmp_path, doc: dict) -> str | None:
    """Require the real parser.js beside `doc`; the load error, if any."""
    (tmp_path / "pricing.json").write_text(json.dumps(doc), encoding="utf-8")
    shutil.copy(PARSER_JS, tmp_path / "parser.js")
    return _node_raw(f"""
      global.window = {{}};
      let error = null;
      try {{ require({str(tmp_path / "parser.js")!r}); }}
      catch (e) {{ error = e.message; }}
      console.log(JSON.stringify(error));
    """)


@needs_node
@pytest.mark.parametrize("damage", DAMAGE)
def test_a_malformed_history_is_refused_in_the_browser(tmp_path, request, damage):
    error = _node_load(tmp_path, _damaged(damage))
    assert error and error.startswith("pricing.json: "), error
    if request.node.callspec.id not in UNSPELLABLE_IN_JSON:
        assert re.search(r"glm-5-3-flash\[\d+\]", error), error


def _provider_string_rate() -> dict:
    doc = copy.deepcopy(_doc())
    doc["providers"]["deepseek/deepseek-v4-flash"]["Azure"][-1]["read"] = "0.031"
    return doc


def test_a_string_provider_rate_is_refused_naming_the_row():
    with pytest.raises(ValueError, match="deepseek/deepseek-v4-flash via Azure"):
        pricing.load_tables(_provider_string_rate())


@needs_node
def test_a_string_provider_rate_is_refused_naming_the_row_in_the_browser(tmp_path):
    error = _node_load(tmp_path, _provider_string_rate())
    assert error and "deepseek/deepseek-v4-flash via Azure" in error, error


# --- the browser load path ---------------------------------------------------
# node has no document or XMLHttpRequest, so parser.js takes its require path
# there. These stub both, so the path the browser actually runs is exercised.

ORIGIN = "https://claudit.example"


def _browser_load(*, pricing_attr: str | None = None, status: int = 200,
                  body: str | None = None, redirected_to: str | None = None):
    """Load the real parser.js as a page would: currentScript is
    /src/parser.js?v=1 on a page at /dashboard/deep/path."""
    dataset = {"pricing": pricing_attr} if pricing_attr else {}
    body = PRICING_JSON.read_text(encoding="utf-8") if body is None else body
    return _node_raw(f"""
      global.window = {{}};
      const requests = [];
      global.document = {{
        baseURI: {json.dumps(ORIGIN + "/dashboard/deep/path")},
        currentScript: {{ src: {json.dumps(ORIGIN + "/src/parser.js?v=1")},
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
      try {{ require({str(PARSER_JS)!r}); }} catch (e) {{ error = e.message; }}
      console.log(JSON.stringify({{
        requests, error,
        fresh: typeof window.rateForModel === 'function'
          ? window.rateForModel('claude-opus-4-7').fresh : null,
      }}));
    """)


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
    version = int(PRICING_JSON.stat().st_mtime)
    tag = re.search(r'<script src="/src/parser\.js[^"]*"[^>]*>', page)
    assert tag, page
    assert f'data-pricing="/src/pricing.json?v={version}"' in tag.group(0)


# Spellings at the edge of the accepted set: both loaders take them, and
# must read each as the same instant.
EDGE_STAMPS = [
    "2032-02-29T00:00:00Z",
    "2033-01-01T00:00:00+23:59",
    "2033-06-01T00:00:00-00:00",
    "2033-12-31T23:59:59+05:30",
    "2034-01-01T00:00:00-12:00",
]


@needs_node
@pytest.mark.parametrize("stamp", EDGE_STAMPS)
def test_both_sides_read_an_edge_spelling_as_the_same_instant(tmp_path, stamp):
    doc = copy.deepcopy(_doc())
    history = doc["models"]["glm-5-3-flash"]
    history.append({**history[-1], "from": stamp})
    want = int(_at(stamp).timestamp() * 1000)
    assert want in [int(e.timestamp() * 1000)
                    for e in pricing.load_tables(doc)["RATE_EPOCHS"]]
    (tmp_path / "pricing.json").write_text(json.dumps(doc), encoding="utf-8")
    shutil.copy(PARSER_JS, tmp_path / "parser.js")
    assert want in _node(tmp_path / "parser.js",
                         "console.log(JSON.stringify(window.rateEpochs));")


# --- a provider row may begin at a time -------------------------------------
# A host first seen at T was never priced by its own row before T, so the
# row starts there: earlier records from it price by the model alone, as
# they did when they were ingested.

NEWCOMER = {"create_1h": 0.2, "create_5m": 0.2, "fresh": 0.2,
            "output": 0.9, "read": 0.05}


def _with_newcomer() -> dict:
    doc = copy.deepcopy(_doc())
    doc["providers"]["z-ai/glm-5-3-flash"]["Newcomer"] = [
        {"from": CUT, **NEWCOMER}]
    return doc


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


def _model_row_beginning() -> dict:
    doc = copy.deepcopy(_doc())
    doc["models"]["glm-5-3-flash"][0]["from"] = "2020-01-01T00:00:00Z"
    return doc


def test_a_model_row_cannot_begin_at_a_time():
    """A model row has no honest fallback — before it, the id would price
    as a tier or default estimate — so it always covers all of time."""
    with pytest.raises(ValueError, match=r"glm-5-3-flash\[0\]"):
        pricing.load_tables(_model_row_beginning())


@needs_node
def test_a_model_row_cannot_begin_at_a_time_in_the_browser(tmp_path):
    error = _node_load(tmp_path, _model_row_beginning())
    assert error and "glm-5-3-flash[0]" in error, error
