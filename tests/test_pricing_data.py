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

from backend import pricing

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


def _node(parser_js, body: str):
    script = f"""
      global.window = {{}};
      require({str(parser_js)!r});
      {body}
    """
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60,
        # Return code checked by hand on the next line.
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


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


@pytest.mark.parametrize("damage", [
    pytest.param(lambda h: h[0].update({"from": CUT}), id="first-has-from"),
    pytest.param(lambda h: h.append({**h[-1], "from": None}),
                 id="later-lacks-from"),
    pytest.param(lambda h: h.extend([{**h[-1], "from": CUT},
                                     {**h[-1], "from": CUT}]),
                 id="from-not-increasing"),
    pytest.param(lambda h: h[-1].pop("read"), id="missing-rate"),
    pytest.param(lambda h: h[-1].update({"reed": 1.0}), id="unknown-field"),
    pytest.param(lambda h: h.append({**h[-1], "from": "2031-01-01T00:00:00"}),
                 id="naive-from"),
])
def test_a_malformed_history_is_refused(damage):
    """A misordered or rewritten history would silently misprice, so the
    loader refuses it and the suite goes red instead."""
    doc = copy.deepcopy(_doc())
    damage(doc["models"]["glm-5-3-flash"])
    with pytest.raises(ValueError, match="glm-5-3-flash"):
        pricing.load_tables(doc)
