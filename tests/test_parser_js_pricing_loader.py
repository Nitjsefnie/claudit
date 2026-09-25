"""The pricing.json loader in src/parser.js refuses a fractional or
exponent HHMM spelling under a schedule window's start/end, in BOTH load
paths, and leaves every other start/end value alone.

Python reads 1400.0 as a float and pricing._hhmm refuses it; JSON.parse
reads 1400.0 as the integer 1400 and would silently price what Python
refuses to load. The loader therefore checks the RAW text before parsing,
scoped by structure (a start/end that is a member of an object that is a
direct element of a "schedule" array), not by key name alone — a future
openrouter.start or a rates object's start parses untouched.

Driven through node like test_parser_js_mirror.py and
test_provider_rate_refresh.py: a copy of parser.js beside a stand-alone
copy of pricing.json in a tmp dir — never the repo's real file.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PARSER_JS = ROOT / "src" / "parser.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available"
)

# The model id a transcript carries, and the row key it resolves to (the
# same shapes the real pricing.json and test_provider_rate_refresh.py use).
MODEL_ID = "z-ai/glm-5.3-flash"
ROW_KEY = "z-ai/glm-5-3-flash"
HOST = "OpenInference"

DAY = {"fresh": 0.3, "create_5m": 0.3, "create_1h": 0.3, "read": 0.03,
       "output": 1.2}
NIGHT = {k: v / 2 for k, v in DAY.items()}

# A placeholder for a start/end whose spelling a test controls: rendered
# as this integer, then replaced in the JSON text by a raw token.
SENTINEL = 31337


def _doc(start1=0, end1=1400, start2=1400, end2=0, rates1=None,
         openrouter_start=None) -> dict:
    """A stand-alone, shape-valid pricing.json: one model row, one provider
    row with a two-window day/night schedule, in the layout
    json.dumps(indent=2, sort_keys=True) writes."""
    rates1 = rates1 if rates1 is not None else dict(DAY)
    doc = {
        "models": {
            "claude-opus-4-8": [{"from": None, "fresh": 5.0,
                                 "create_5m": 6.25, "create_1h": 10.0,
                                 "read": 0.5, "output": 25.0}],
        },
        "providers": {
            ROW_KEY: {
                HOST: [{
                    "from": None,
                    **DAY,
                    "schedule": [
                        {"start": start1, "end": end1, "rates": rates1},
                        {"start": start2, "end": end2, "rates": NIGHT},
                    ],
                }],
            },
        },
        "openrouter": {
            "data_region": "global",
            "models": {ROW_KEY: {"id": MODEL_ID}},
        },
        "provider_rates_fetched": "2026-09-24T22:03:13Z",
    }
    if openrouter_start is not None:
        doc["openrouter"]["start"] = openrouter_start
    return doc


def _render(doc: dict, spelling: str | None = None) -> str:
    """The doc as its JSON text, with the SENTINEL placeholder replaced by
    a raw number token (`spelling`) where a test controls the spelling."""
    text = json.dumps(doc, indent=2, sort_keys=True) + "\n"
    if spelling is not None:
        assert str(SENTINEL) in text, "no placeholder to spell"
        text = text.replace(str(SENTINEL), spelling, 1)
    return text


def _run(tmp_path: Path, text: str, browser: bool = False) -> dict:
    """Require the real parser.js beside `text` as pricing.json in one
    `node -e` run, on the node path or the fake-browser (XHR) path.

    Returns {error, untouched, rates}: the require's error message, whether
    the doc the loader parsed equals a plain JSON.parse of the same text
    (the value survived as a number), and — when the loader completed — the
    day/night schedule rates window.rateForModel resolves, mapped back to
    the five backend field names.
    """
    where = tmp_path / ("browser" if browser else "node")
    where.mkdir(exist_ok=True)
    fixture = where / "pricing.json"
    fixture.write_text(text, encoding="utf-8")
    shutil.copy(PARSER_JS, where / "parser.js")
    # The loader's own JSON.parse call is captured and compared against a
    # plain parse: a reviver that rewrote values (the old _hhmmSpelling)
    # shows up as untouched=false even when nothing else observes the doc.
    browser_setup = ""
    if browser:
        browser_setup = (
            "global.document = {\n"
            "  currentScript: { dataset: { pricing: 'pricing.json' },\n"
            f"                   src: 'file://{where}/parser.js' }},\n"
            "};\n"
            "global.XMLHttpRequest = class {\n"
            "  open(method, url) { this.responseURL = url; }\n"
            "  setRequestHeader() {}\n"
            "  send() { this.status = 200; this.responseText = text; }\n"
            "};"
        )
    script = f"""
      global.window = {{}};
      const fs = require('fs');
      const text = fs.readFileSync({json.dumps(str(fixture))}, 'utf8');
      {browser_setup}
      let captured = null;
      let error = null;
      const realParse = JSON.parse;
      JSON.parse = function (t, reviver) {{
        const got = realParse(t, reviver);
        if (captured === null) captured = {{doc: got, text: t}};
        return got;
      }};
      try {{ require({json.dumps(str(where / "parser.js"))}); }}
      catch (e) {{ error = String(e.message); }}
      JSON.parse = realParse;
      let untouched = null;
      if (captured !== null) {{
        untouched = JSON.stringify(captured.doc)
                    === JSON.stringify(realParse(captured.text));
      }}
      let rates = null;
      if (window.rateForModel) {{
        const K = {{fresh: 'fresh', c5: 'create_5m', c1h: 'create_1h',
                   read: 'read', out: 'output'}};
        const map = (r) => Object.fromEntries(
          Object.entries(K).map(([a, b]) => [b, r[a]]));
        rates = {{
          day: map(window.rateForModel(
            {json.dumps(MODEL_ID)}, '2026-09-21T10:00:00Z', {json.dumps(HOST)})),
          night: map(window.rateForModel(
            {json.dumps(MODEL_ID)}, '2026-09-21T20:00:00Z', {json.dumps(HOST)})),
        }};
      }}
      console.log(JSON.stringify({{error, untouched, rates}}));
    """
    proc = subprocess.run(["node", "-e", script], capture_output=True,
                          text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# --- a schedule time spelled with an exponent or a fraction --------------------


def test_an_exponent_spelled_schedule_start_throws_naming_pricing_json(
        tmp_path):
    out = _run(tmp_path, _render(_doc(start1=SENTINEL), spelling="14e2"))
    assert out["error"], "the loader must refuse an exponent spelling"
    assert "pricing.json" in out["error"]
    assert "14e2" in out["error"], out["error"]
    assert "offset" in out["error"], out["error"]


def test_a_fractional_schedule_end_throws_naming_pricing_json(tmp_path):
    out = _run(tmp_path, _render(_doc(end1=SENTINEL), spelling="30.0"))
    assert out["error"], "the loader must refuse a fractional spelling"
    assert "pricing.json" in out["error"]
    assert "30.0" in out["error"], out["error"]
    assert "offset" in out["error"], out["error"]


# --- a valid file --------------------------------------------------------------


def test_plain_integer_schedule_times_load_and_price_the_windows(tmp_path):
    out = _run(tmp_path, _render(_doc()))
    assert out["error"] is None, out["error"]
    assert out["rates"] == {"day": DAY, "night": NIGHT}


# --- a fractional start outside a schedule -------------------------------------


def test_a_fractional_start_outside_a_schedule_loads_as_a_number(tmp_path):
    out = _run(tmp_path, _render(
        _doc(openrouter_start=SENTINEL), spelling="30.0"))
    assert out["error"] is None, out["error"]
    assert out["untouched"] is True, "the value must survive as a number"
    assert out["rates"]["day"] == DAY


# --- the browser load path -----------------------------------------------------


def test_the_browser_load_path_checks_the_spelling_and_prices_a_valid_file(
        tmp_path):
    bad = _run(tmp_path, _render(_doc(end1=SENTINEL), spelling="30.0"),
               browser=True)
    assert bad["error"], "the XHR path must refuse a fractional spelling too"
    assert "pricing.json" in bad["error"]
    assert "30.0" in bad["error"], bad["error"]
    assert "offset" in bad["error"], bad["error"]
    good = _run(tmp_path, _render(_doc()), browser=True)
    assert good["error"] is None, good["error"]
    assert good["rates"] == {"day": DAY, "night": NIGHT}


# --- scoping: a start inside a window's rates object ---------------------------


def test_a_start_inside_a_window_rates_object_is_not_an_offense(tmp_path):
    rates = {**DAY, "start": SENTINEL}
    out = _run(tmp_path, _render(_doc(rates1=rates), spelling="30.0"))
    # The extra rates key is refused on SHAPE, never on spelling: the error
    # is the five-fields error, with no offset named.
    assert out["error"], out
    assert "rates" in out["error"]
    assert "offset" not in out["error"], out["error"]
    # And the value itself survived as the number 30: what the loader
    # parsed equals a plain parse of the same text.
    assert out["untouched"] is True, "the value must survive as a number"
