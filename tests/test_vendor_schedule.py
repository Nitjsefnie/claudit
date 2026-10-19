"""Scheduled vendor selection leaves lossless history to the provider pass."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from backend import pricing
from backend.pricing_rows import decode_pricing_doc, encode_pricing_doc
from tests.test_pricing_vendor import _copy_browser, _install, _node_raw, needs_node
from tests.vendor_rate_refresh_helpers import (
    GLM_ID, GLM_KEY, GPT_ID, NOW, _band, _catalog, _doc, _endpoint, _payload,
    _price, _run,
)
from tests.test_vendor_rate_refresh import MainRun


def test_a_scheduled_meter_band_still_refuses():
    doc, out = _run(_doc(), _catalog(GPT_ID), {GPT_ID: _payload(_endpoint(
        "openai", _price(1, 5, overrides=[
            _band(1, 5), {"utc_days": ["monday"], "prompt": "0.000001"}])))})
    assert len(out.refusals) == 1 and "schedule" in out.refusals[0]
    assert not doc["openrouter"]["models"]


@needs_node
def test_scheduled_vendor_history_prices_each_window_losslessly(
        tmp_path, capsys, monkeypatch):
    listing = _price(2, 8, overrides=[{
        "utc_days": ["monday"], "utc_start": 100, "utc_end": 200,
        "prompt": "0.000001", "completion": "0.000004"}])
    run = MainRun(tmp_path, _doc(), _catalog(GLM_ID), {GLM_ID: _payload(
        _endpoint("z-ai", listing), _endpoint("z-ai/fast", _price(9, 9)))})
    for _ in range(2):  # auto-add, then provider pickup
        rc, _, err = run(capsys)
        assert rc == 0, err
    raw = json.loads(run.pricing_path.read_text())
    doc = decode_pricing_doc(raw)
    assert doc["openrouter"]["models"][GLM_KEY]["vendor_host"] == "Vendor"
    history = doc["providers"][GLM_KEY]["Vendor"]
    assert history[-1]["schedule"] == [{
        "days": ["monday"], "start": 100, "end": 200,
        "rates": {"fresh": 1, "read": 1.0, "output": 4}}]
    listing["overrides"][0]["prompt"] = "0.0000005"
    monkeypatch.setattr("tests.test_vendor_rate_refresh.NOW", NOW + timedelta(hours=1))
    rc, _, err = run(capsys)
    assert rc == 0, err
    doc = decode_pricing_doc(json.loads(run.pricing_path.read_text()))
    assert doc["providers"][GLM_KEY]["Vendor"][:-1] == history
    assert decode_pricing_doc(encode_pricing_doc(doc)) == doc
    _install(monkeypatch, doc)
    _copy_browser(tmp_path)
    stamps = ["2031-01-06T00:59:59Z", "2031-01-06T01:00:00Z",
              "2031-01-06T02:00:00Z"]
    got = _node_raw(f"""
      global.window = {{}};
      require({str(tmp_path / 'pricing-loader.js')!r});
      require({str(tmp_path / 'rates.js')!r});
      console.log(JSON.stringify({json.dumps(stamps)}.map(ts =>
        window.resolveModelRate({GLM_KEY!r}, ts, 'Vendor').rates.fresh)));
    """)
    assert [pricing.resolve(GLM_KEY, datetime.fromisoformat(ts), "Vendor").rates["fresh"]
            for ts in stamps] == got == [2, 0.5, 2]
