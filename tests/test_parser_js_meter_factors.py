"""Browser parity for asymmetric and one-sided long-context factors."""
from __future__ import annotations

import json
import subprocess

import pytest

from backend import parse, pricing
from tests.test_parser_js_lanes import (
    CODEX_JS, LANES_JS, LOADER_JS, PARSER_JS, RATES_JS, _long_context_blob,
)


def test_one_sided_meter_factor_matches_backend_and_browser(monkeypatch):
    model_key = "gpt-5-6-sol"
    rates = {"fresh": 7.0, "create_5m": 8.75, "create_1h": 14.0,
             "read": 0.7, "output": 35.0}
    meter = {"threshold": 100_000, "input_mult": 6.0}
    monkeypatch.setattr(pricing, "MODEL_RATES",
                        {**pricing.MODEL_RATES, model_key: rates})
    monkeypatch.setattr(
        pricing, "DATED_RATES",
        {key: value for key, value in pricing.DATED_RATES.items()
         if key != model_key})
    monkeypatch.setattr(
        pricing, "LONG_CONTEXT_MODELS",
        frozenset({*pricing.LONG_CONTEXT_MODELS, model_key}))
    monkeypatch.setattr(pricing, "LONG_CONTEXT_METERS",
                        {**pricing.LONG_CONTEXT_METERS, model_key: meter})
    backend = parse.parse_file(
        "codex/one_sided_meter.jsonl", _long_context_blob(None))["records"][0]

    script = f"""
      global.window = {{}};
      require({str(LANES_JS)!r});
      require({str(CODEX_JS)!r});
      require({str(LOADER_JS)!r});
      window.modelRates[{json.dumps(model_key)}] = {{
        fresh: 7.0, c5: 8.75, c1h: 14.0, read: 0.7, out: 35.0 }};
      delete window.datedRates[{json.dumps(model_key)}];
      window.longContextModels = [{json.dumps(model_key)}];
      window.longContextMeters = {{{json.dumps(model_key)}: {json.dumps(meter)}}};
      require({str(RATES_JS)!r});
      require({str(PARSER_JS)!r});
      const text = {json.dumps(_long_context_blob(None).decode())};
      const {{ events, meta }} = window.parseTranscript(text);
      const record = meta.find(m => m.type === 'assistant_usage');
      console.log(JSON.stringify({{
        factors: window.longContextFactorsFor(record.model),
        cost: window.computeSessionStats(events, meta).cost,
      }}));
    """
    proc = subprocess.run(["node", "-e", script], capture_output=True,
                          text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout)
    assert got["factors"] == [6.0, pricing.LONG_CONTEXT_OUTPUT_MULT]
    assert got["cost"] == pytest.approx(backend["cost_usd"], abs=1e-9)
