"""Source-level guards for the provider dimension in src/app.jsx.

node cannot parse JSX, so these read the source (the same boundary
test_panel_wiring.py documents). The failure they close is silent: a
browser pricing site that drops the provider prices an OpenRouter row at
the model's rate, and the Token Breakdown's bars stop summing to the
stored cost the dashboard shows beside them.
"""
from __future__ import annotations

import re
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "src" / "app.jsx"


def _src() -> str:
    return re.sub(r"(?<![:'\"\w])//.*$", "", APP.read_text(encoding="utf-8"),
                  flags=re.M)


def test_every_browser_pricing_site_passes_the_provider():
    calls = re.findall(r"\b(?:window\.rateForModel|rateFor)\(([^)]*)\)", _src())
    assert len(calls) == 2, calls
    for args in calls:
        assert "provider" in args.split(",")[-1], args


def test_backend_hourly_rows_carry_the_provider_onto_events():
    assert re.search(r"provider:\s*h\.provider\s*\|\|\s*null", _src())


def test_model_panels_key_by_model_and_provider():
    src = _src()
    assert "b.cost_by_model_provider" in src
    assert re.search(r"label = modelProviderLabel\(e\.model,\s*e\.provider\)", src)
    assert "byModel[label]" in src and "tokensByModel[label]" in src
    assert "modelProviderLabel(short(r.model), r.provider)" in src
