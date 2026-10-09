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
from backend.pricing_document import expand_pricing_doc

ROOT = Path(__file__).resolve().parents[1]
LOADER_JS = ROOT / "src" / "pricing-loader.js"
VENDOR_TABLES_JS = ROOT / "src" / "vendor-tables.js"
HHMM_JS = ROOT / "src" / "hhmm-spelling.js"
RATES_JS = ROOT / "src" / "rates.js"
PARSER_USAGE_JS = ROOT / "src" / "parser-usage.js"
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
    return expand_pricing_doc(
        json.loads(_pricing_json().read_text(encoding="utf-8")))


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
                rates = dict(window["rates"])
                if "web_search" in current:
                    rates["web_search"] = current["web_search"]
                return rates
    rates = {f: current[f] for f in RATE_FIELDS}
    if "web_search" in current:
        rates["web_search"] = current["web_search"]
    return rates


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
    shutil.copy(VENDOR_TABLES_JS, tmp_path / "vendor-tables.js")
    shutil.copy(HHMM_JS, tmp_path / "hhmm-spelling.js")
    shutil.copy(RATES_JS, tmp_path / "rates.js")
    shutil.copy(PARSER_USAGE_JS, tmp_path / "parser-usage.js")
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
    rates = {py: js[k] for k, py in JS_FIELDS.items()}
    if "search" in js:
        rates["web_search"] = js["search"]
    return rates


def test_the_file_has_every_cutover_the_backend_prices_by():
    doc = _doc()
    # Offset spellings are loader-accepted; compare instants (SV-RATE-DATA).
    assert pricing.RATE_EPOCHS == sorted({
        _at(entry["from"]) for _, _, history in _histories(doc)
        for entry in history if entry["from"]
    })
    assert pricing.PROVIDER_RATES_FETCHED == _at(doc["provider_rates_fetched"])


NEWCOMER = {"create_1h": 0.2, "create_5m": 0.2, "fresh": 0.2,
            "output": 0.9, "read": 0.05}


def _with_newcomer() -> dict:
    doc = copy.deepcopy(_doc())
    doc["providers"][_GLM_MODEL]["Newcomer"] = [
        {"from": CUT, **NEWCOMER}]
    return doc


def _moved() -> dict:
    """The file after a busy hour: a new host, its later move, and a move
    on a seeded row."""
    doc = _with_newcomer()
    novita = doc["providers"][_GLM_MODEL]["Novita"]
    newcomer = doc["providers"][_GLM_MODEL]["Newcomer"]
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
        vendorPrefixes: window.vendorPrefixes,
        vendorBareForms: window.vendorBareForms,
        vendorHosts: window.vendorHosts,
      }));
    """)
    assert got["vendorBare"] == pricing.VENDOR_BARE
    assert got["vendorPrefixes"] == pricing.VENDOR_PREFIXES
    assert got["vendorBareForms"] == list(pricing.VENDOR_BARE)
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


def test_the_file_is_in_canonical_layout():
    """Sorted keys, two-space indent, one field per line: the layout
    json.dumps(doc, indent=2, sort_keys=True) writes, so an automated
    refresh rewrites the file byte-for-byte except for what moved."""
    text = _pricing_json().read_text(encoding="utf-8")
    assert text == json.dumps(json.loads(text), indent=2, sort_keys=True) + "\n"


def test_tracked_vendor_model_keys_use_bare_forms_only():
    doc = _doc()
    prefixes = doc["openrouter"]["vendor"]["prefixes"]
    prefixed = [key for key in doc["openrouter"]["models"]
                if any(key.startswith(f"{prefix}/") for prefix in prefixes)]
    assert prefixed == []


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
_GLM_MODEL_ID = _doc()["openrouter"]["models"][_GLM_MODEL]["id"]
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
