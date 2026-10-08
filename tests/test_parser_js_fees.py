"""The browser half of the per-request fee (issue #469).

src/parser.js reads the same pricing.json (SV-RATE-DATA) and prices each
record exactly as backend/pricing.py does, fees included — or the
Inspector's per-record costs and the stored cost_usd disagree. Driven
through node like test_parser_js_mirror.py: a copy of parser.js beside a
stand-alone synthetic pricing.json in a tmp dir — never the repo's real
file (SV-TEST-DATA: the live fee rows move under the refresh).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LOADER_JS = ROOT / "src" / "pricing-loader.js"
VENDOR_TABLES_JS = ROOT / "src" / "vendor-tables.js"
PARSER_JS = ROOT / "src" / "parser.js"
RATES_JS = ROOT / "src" / "rates.js"
RATE_FIELDS = ("fresh", "create_5m", "create_1h", "read", "output")

FEE = 0.0137
FEE2 = 0.045
CUTOVER = "2026-08-01T00:00:00Z"
RATES = {"fresh": 2.0, "create_5m": 2.5, "create_1h": 4.0,
         "read": 0.2, "output": 10.0}


def _entry(rates: dict, **extra) -> dict:
    return {"from": None, **{f: rates[f] for f in RATE_FIELDS}, **extra}


def _fee_note(amount: float) -> str:
    return (f"web_search ${amount}/request not modelled: per-request, "
            "unpriceable from token counts")


def _doc() -> dict:
    return {
        "models": {
            "acme/acme-9": [_entry(RATES)],
            # The default estimate's row the loaders require (frozen
            # synthetic rates, the sibling seed docs' shape).
            "claude-opus-4-7": [{"from": None, "fresh": 5.0,
                                 "create_5m": 6.25, "create_1h": 10.0,
                                 "read": 0.5, "output": 25.0}],
        },
        "providers": {
            "acme/acme-9": {
                "HostCo": [
                    _entry(RATES, note=_fee_note(FEE)),
                    {**_entry(RATES), "from": CUTOVER,
                     "note": "17% off; " + _fee_note(FEE2)},
                ],
            },
        },
        "openrouter": {"data_region": "global", "models": {},
                       "vendor": {"prefixes": ["anthropic", "openai",
                                               "moonshotai", "z-ai"]}},
        "long_context_models": [],
        "provider_rates_fetched": "2026-09-24T22:03:13Z",
    }


@pytest.fixture(name="sandbox")
def _sandbox(tmp_path, monkeypatch):
    """parser.js + the synthetic pricing.json, alone in a tmp dir."""
    shutil.copy(LOADER_JS, tmp_path / "pricing-loader.js")
    shutil.copy(VENDOR_TABLES_JS, tmp_path / "vendor-tables.js")
    shutil.copy(PARSER_JS, tmp_path / "parser.js")
    shutil.copy(RATES_JS, tmp_path / "rates.js")
    (tmp_path / "pricing.json").write_text(
        json.dumps(_doc(), indent=2, sort_keys=True), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _node(sandbox, script: str) -> dict:
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60,
        # Return code checked by hand on the next line.
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_browser_exposes_the_parsed_fees(sandbox):
    got = _node(sandbox, f"""
      global.window = {{}};
      require({str(sandbox / 'pricing-loader.js')!r});
      require({str(sandbox / 'rates.js')!r});
      require({str(sandbox / 'parser.js')!r});
      console.log(JSON.stringify({{
        modelFees: window.modelFees,
        providerFees: window.providerFees,
      }}));
    """)
    assert got["modelFees"] == {}
    assert got["providerFees"] == {
        "acme/acme-9": {"HostCo": {"0": FEE, "1": FEE2}},
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_browser_resolves_the_entry_in_force_fee(sandbox):
    got = _node(sandbox, f"""
      global.window = {{}};
      require({str(sandbox / 'pricing-loader.js')!r});
      require({str(sandbox / 'rates.js')!r});
      require({str(sandbox / 'parser.js')!r});
      const r = (m, ts, p) => window.resolveModelRate(m, ts, p).fee;
      console.log(JSON.stringify({{
        before: r('acme/acme-9', '2026-07-01T00:00:00Z', 'HostCo'),
        after: r('acme/acme-9', '2026-08-02T00:00:00Z', 'HostCo'),
        list: r('acme/acme-9', null, 'HostCo'),
        bare: r('acme/acme-9', null, null),
      }}));
    """)
    assert got == {"before": FEE, "after": FEE2, "list": FEE2, "bare": 0.0}


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_browser_compute_session_stats_folds_the_fee(sandbox):
    # One record on the fee row, 1M fresh tokens at 2.0/M plus the fee
    # in force at the record's own ts; the rounded shape mirrors the
    # stored cost_usd column's round(x, 6).
    got = _node(sandbox, f"""
      global.window = {{}};
      require({str(sandbox / 'pricing-loader.js')!r});
      require({str(sandbox / 'rates.js')!r});
      require({str(sandbox / 'parser.js')!r});
      const m = {{ type: 'assistant_usage', line: 1,
                   ts: '2026-07-01T12:00:00Z', model: 'acme/acme-9',
                   provider: 'HostCo',
                   usage: {{ input_tokens: 1000000,
                             cache_creation_input_tokens: 0,
                             cache_read_input_tokens: 0,
                             output_tokens: 0 }} }};
      console.log(JSON.stringify(window.computeSessionStats([], [m]).cost));
    """)
    assert got == pytest.approx(2.0 + FEE)
