"""SV-RATE-DATA: every rate lives in src/pricing.json, read by both sides.

backend/pricing.py and src/parser.js hold resolution logic only. These
tests pin the three properties that make the file the single source: both
sides price every row the file defines identically and exactly as the file
says, a price change is recorded by APPENDING an entry to a row's history,
and the file stays in the canonical layout an automated writer reproduces.
"""
from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from backend import pricing

ROOT = Path(__file__).resolve().parents[1]
LOADER_JS = ROOT / "src" / "pricing-loader.js"
RATES_JS = ROOT / "src" / "rates.js"
PARSER_JS = ROOT / "src" / "parser.js"
PRICING_PY = ROOT / "backend" / "pricing.py"
RATE_FIELDS = ("fresh", "create_5m", "create_1h", "read", "output")
JS_FIELDS = {"fresh": "fresh", "c5": "create_5m", "c1h": "create_1h",
             "read": "read", "out": "output"}

needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available"
)


def _pricing_json() -> Path:
    return ROOT / "src" / "pricing.json"


def _doc() -> dict:
    return json.loads(_pricing_json().read_text(encoding="utf-8"))


def _at(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp)


def _when(stamp: str | None) -> datetime | None:
    return _at(stamp) if stamp else None


def _stamp(when: datetime) -> str:
    return when.isoformat().replace("+00:00", "Z")


def _bare_forms(doc: dict) -> dict[str, str]:
    """Tracked key -> bare form, by the file's own configured prefixes."""
    prefixes = (doc.get("openrouter") or {}).get("vendor", {}).get("prefixes", [])

    def bare(key: str) -> str:
        for prefix in prefixes:
            if key.startswith(prefix + "/"):
                return key[len(prefix) + 1:]
        return key

    return {key: bare(key)
            for key, entry in (doc.get("openrouter") or {}).get("models", {}).items()
            if entry.get("vendor_host")}


def _histories(doc: dict):
    """(model, provider or None, history) for every row the file defines,
    at every surface it prices: each models row bare, each provider row
    through its host, and each tracked vendor row's bare first-party id."""
    for model, history in doc["models"].items():
        yield model, None, history
    for model, hosts in doc["providers"].items():
        for host, history in hosts.items():
            yield model, host, history
    for key, bare in _bare_forms(doc).items():
        host = doc["openrouter"]["models"][key]["vendor_host"]
        history = doc["providers"].get(key, {}).get(host)
        if history is not None:
            yield bare, None, history


def _latest_document_stamp(doc: dict) -> datetime:
    """Latest rate or fetch stamp, so synthetic rows follow the whole file."""
    stamps = [_at(entry["from"]) for _, _, history in _histories(doc)
              for entry in history if entry["from"]]
    stamps.append(_at(doc["provider_rates_fetched"]))
    return max(stamps, default=datetime.min.replace(tzinfo=timezone.utc))


def _in_force(model: str, history: list[dict], stamp: str | None) -> dict:
    """Rates in force at `stamp`; a free-shaped id prices by shape."""
    if pricing._is_free(model, pricing._normalise(model)):  # pylint: disable=protected-access
        return dict(pricing.FREE_RATES)
    when = _when(stamp)
    current = history[0]
    for entry in history[1:]:
        if when is None or _at(entry["from"]) <= when:
            current = entry
    if when is not None:
        utc = when.astimezone(timezone.utc)
        day, hhmm = utc.strftime("%A").lower(), utc.hour * 100 + utc.minute
        for window in current.get("schedule", []):
            start, end = window.get("start"), window.get("end")
            if day in window.get("days", [day]) and (
                    start is None or (start <= hhmm < end if start < end
                                      else hhmm >= start or hhmm < end)):
                return dict(window["rates"])
    return {f: current[f] for f in RATE_FIELDS}


def _around(cutovers) -> list[str]:
    return [_stamp(cut + timedelta(seconds=d)) for cut in cutovers for d in (-1, 0, 1)]


def _cases(doc: dict) -> list[tuple[str, str | None, str | None]]:
    """Every row at list price, and one second either side of, and exactly
    at, each of its own cutovers and every model row's. Its own, not every
    row's: the refresh appends cutovers every hour, and rows × all cutovers
    would grow without bound with them."""
    shared = {_at(e["from"]) for history in doc["models"].values()
              for e in history if e["from"]}
    return [(model, stamp, host)
            for model, host, history in _histories(doc)
            for stamp in [None, *_around(sorted(
                shared | {_at(e["from"]) for e in history if e["from"]}))]]


def _node_raw(script: str):
    # The program goes on stdin: inlined cases outgrow one argv string.
    proc = subprocess.run(
        ["node", "-"], input=script, capture_output=True, text=True, timeout=60,
        # Return code checked by hand on the next line.
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _copy_browser(tmp_path):
    shutil.copy(LOADER_JS, tmp_path / "pricing-loader.js")
    shutil.copy(RATES_JS, tmp_path / "rates.js")
    shutil.copy(PARSER_JS, tmp_path / "parser.js")


def _node(parser_js, body: str):
    # A sandboxed parser.js requires its loader and rates.js beside it first.
    pre = "".join(
        f"require({str(p)!r});\n      "
        for p in (parser_js.parent / "pricing-loader.js",
                  parser_js.parent / "rates.js") if p.exists())
    return _node_raw(f"""
      global.window = {{}};
      {pre}require({str(parser_js)!r});
      {body}
    """)


def _js_rates(js: dict) -> dict:
    return {py: js[k] for k, py in JS_FIELDS.items()}


def test_the_file_has_every_cutover_the_backend_prices_by():
    doc = _doc()
    # Offset spellings are loader-accepted; compare instants (SV-RATE-DATA).
    assert pricing.RATE_EPOCHS == sorted({
        _at(entry["from"]) for _, _, history in _histories(doc)
        for entry in history if entry["from"]
    })
    assert pricing.PROVIDER_RATES_FETCHED == _at(doc["provider_rates_fetched"])


def _moved() -> dict:
    """The file after a busy hour: a new host, its later move, and a move
    on a seeded row."""
    doc = _with_newcomer()
    novita = doc["providers"]["z-ai/glm-5-3-flash"]["Novita"]
    newcomer = doc["providers"]["z-ai/glm-5-3-flash"]["Newcomer"]
    newcomer_start = _latest_document_stamp(doc) + timedelta(seconds=1)
    newcomer[0]["from"] = _stamp(newcomer_start)
    cut = _stamp(newcomer_start + timedelta(seconds=1))
    moved = _stamp(_at(cut) + timedelta(seconds=1))
    newcomer.append(
        {"from": moved, "create_1h": 0.4, "create_5m": 0.4,
         "fresh": 0.4, "output": 1.8, "read": 0.1})
    novita.append({**novita[-1], "from": cut, "read": 0.5})
    doc["provider_rates_fetched"] = moved
    return doc


# The committed file, and the same file after the refresh has committed:
# a data-coupled assertion must hold for both, or the first bot commit
# fails its own suite.
DOCUMENTS = [pytest.param(None, id="committed"), pytest.param(_moved, id="moved")]


def _install(monkeypatch, factory) -> dict:
    doc = _doc() if factory is None else factory()
    for name, value in pricing.load_tables(doc).items():
        monkeypatch.setattr(pricing, name, value)
    return doc


@pytest.mark.parametrize("factory", DOCUMENTS)
def test_the_backend_prices_every_row_as_the_file_says(monkeypatch, factory):
    doc = _install(monkeypatch, factory)
    histories = {(m, h): history for m, h, history in _histories(doc)}
    for model, stamp, host in _cases(doc):
        got = pricing.resolve(model, _when(stamp), host)
        label = f"{model} via {host} @ {stamp}"
        begins = histories[model, host][0]["from"]
        if stamp is not None and begins is not None and _at(stamp) < _at(begins):
            # A host first seen by a refresh: before its row begins, the
            # record prices by the model alone.
            assert got == pricing.resolve(model, _when(stamp)), label
            continue
        assert got.kind == "exact", label
        assert got.rates == _in_force(model, histories[model, host], stamp), label


@needs_node
@pytest.mark.parametrize("factory", DOCUMENTS)
def test_both_sides_resolve_every_row_the_file_defines_identically(
        monkeypatch, tmp_path, factory):
    doc = _install(monkeypatch, factory)
    (tmp_path / "pricing.json").write_text(json.dumps(doc), encoding="utf-8")
    _copy_browser(tmp_path)
    cases = _cases(doc)
    got_all = _node(tmp_path / "parser.js", f"""
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
    got = _node(LOADER_JS, """
      console.log(JSON.stringify({
        models: Object.entries(window.modelRates),
        dated: Object.entries(window.datedRates),
        providers: window.providerRates,
        providerDated: window.providerDatedRates,
        vendorBare: window.vendorBare,
        vendorHosts: window.vendorHosts,
      }));
    """)
    assert got["vendorBare"] == pricing.VENDOR_BARE
    assert got["vendorHosts"] == pricing.VENDOR_HOSTS
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


def test_the_file_is_in_canonical_layout():
    """Sorted keys, two-space indent, one field per line: the layout
    json.dumps(doc, indent=2, sort_keys=True) writes, so an automated
    refresh rewrites the file byte-for-byte except for what moved."""
    text = _pricing_json().read_text(encoding="utf-8")
    assert text == json.dumps(json.loads(text), indent=2, sort_keys=True) + "\n"


def test_neither_side_carries_a_rate_literal():
    """A second copy of a rate is what the file replaced. The cache-write
    field is unique to rate rows, so a literal one is a copied row."""
    assert not re.search(r"""["']create_5m["']\s*:\s*[\d.]""",
                         PRICING_PY.read_text(encoding="utf-8"))
    for js in (PARSER_JS, RATES_JS, LOADER_JS):
        assert not re.search(r"\bc5\s*:\s*[\d.]",
                             js.read_text(encoding="utf-8"))


# --- history is append-only --------------------------------------------------

_GLM_MODEL = "glm-5-3-flash"
_GLM_HOST = "Z.AI"
_GLM_HISTORY = _doc()["providers"][_GLM_MODEL][_GLM_HOST]
CUT = _stamp(max(
    [_at(entry["from"]) for entry in _GLM_HISTORY if entry["from"]]
    + [_at(_doc()["provider_rates_fetched"])]
    + [datetime.min.replace(tzinfo=timezone.utc)]) + timedelta(seconds=1))
APPEND_ROWS = [
    pytest.param(("providers", "glm-5-3-flash", "Z.AI"), "glm-5.3-flash",
                 None, id="bare-vendor"),
    pytest.param(("providers", "deepseek/deepseek-v4-1-flash", "Novita"),
                 "deepseek/deepseek-v4.1-flash", "Novita", id="provider"),
]


def _appended(path: tuple[str, ...]) -> tuple[dict, dict, dict]:
    """The file with one entry appended to the row at `path`: the new
    document, the rates in force before the cutover, and the new rates."""
    doc = copy.deepcopy(_doc())
    history: Any = doc
    for part in path:
        history = history[part]
    before = {f: history[-1][f] for f in RATE_FIELDS}
    after = {field: (0.123456 if value != 0.123456 else 0.654321)
             for field, value in before.items()}
    newest = max((_at(entry["from"]) for entry in history if entry["from"]),
                 default=datetime.min.replace(tzinfo=timezone.utc))
    cut = _stamp(max(newest, _at(doc["provider_rates_fetched"]))
                 + timedelta(seconds=1))
    history.append({"from": cut, **after})
    doc["provider_rates_fetched"] = cut
    return doc, before, after


@pytest.mark.parametrize("path, model, host", APPEND_ROWS)
def test_an_appended_entry_prices_from_its_cutover_on_in_the_backend(
        monkeypatch, path, model, host):
    doc, before, after = _appended(path)
    cut = _at(doc["provider_rates_fetched"])
    previous_epochs = set(pricing.RATE_EPOCHS)
    earlier = {epoch: pricing.rate_for(model, epoch - timedelta(seconds=1), host)
               for epoch in pricing.RATE_EPOCHS}
    for name, value in pricing.load_tables(doc).items():
        monkeypatch.setattr(pricing, name, value)
    assert previous_epochs.issubset(pricing.RATE_EPOCHS)
    assert cut in pricing.RATE_EPOCHS
    assert pricing.rate_for(model, cut - timedelta(seconds=1), host) == before
    assert pricing.rate_for(model, cut, host) == after
    assert pricing.rate_for(model, None, host) == after, "no ts => newest"
    for epoch, want in earlier.items():
        if epoch <= cut:
            assert pricing.rate_for(model, epoch - timedelta(seconds=1), host) == want


@needs_node
@pytest.mark.parametrize("path, model, host", APPEND_ROWS)
def test_an_appended_entry_prices_from_its_cutover_on_in_the_browser(
        tmp_path, path, model, host):
    doc, before, after = _appended(path)
    (tmp_path / "pricing.json").write_text(json.dumps(doc), encoding="utf-8")
    _copy_browser(tmp_path)
    cut = _at(doc["provider_rates_fetched"])
    stamps = [_stamp(cut - timedelta(seconds=1)),
              doc["provider_rates_fetched"], None]
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
    damage(doc["models"]["bonsai-2-27b"])
    return doc


def _node_load(tmp_path, doc: dict) -> str | None:
    """Require the real pricing-loader.js beside `doc`; the load error."""
    (tmp_path / "pricing.json").write_text(json.dumps(doc), encoding="utf-8")
    shutil.copy(LOADER_JS, tmp_path / "pricing-loader.js")
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

NEWCOMER = {"create_1h": 0.2, "create_5m": 0.2, "fresh": 0.2,
            "output": 0.9, "read": 0.05}


def _with_newcomer() -> dict:
    doc = copy.deepcopy(_doc())
    doc["providers"]["z-ai/glm-5-3-flash"]["Newcomer"] = [
        {"from": CUT, **NEWCOMER}]
    return doc


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
SCHEDULED = ("z-ai/glm-5-3-flash", "Novita")


def _with_schedule(schedule=None) -> dict:
    doc = copy.deepcopy(_doc())
    model, host = SCHEDULED
    source = doc["providers"]["z-ai/glm-5-3-flash"]["Novita"][-1]
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
    doc["providers"]["z-ai/glm-5-3-flash"]["Newcomer"].append({"from": LATER, **MOVED})
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
    return {
        "models": {"claude-opus-4-7": [_CLAUDE_DEFAULT_ENTRY]},
        "providers": {"acme/acme-9": {"HostCo": [
            {"from": P_START, **P_BEFORE},
            {"from": P_CUT, **P_AFTER},
        ]}},
        "openrouter": {"data_region": "global", "models": {},
                       "vendor": {"prefixes": ["anthropic", "openai",
                                               "moonshotai", "z-ai"]}},
    }


# The default-estimate row a synthetic document carries: frozen list rates
# at no cutover, so it adds no epoch and asserts nothing about prices.
_CLAUDE_DEFAULT_ENTRY = {"from": None, "fresh": 5.0, "create_5m": 6.25,
                         "create_1h": 10.0, "read": 0.5, "output": 25.0}


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
