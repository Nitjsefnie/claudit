"""Long-context band folding and reporting through vendor refresh."""
from __future__ import annotations

import json

import pytest

from tests.test_vendor_rate_refresh import (
    GPT_ID, GPT_KEY, RATE_FIELDS, RATES, STAMP, TRACKED, MainRun,
    _band, _catalog, _doc, _endpoint, _load, _main_doc, _per_token, _run,
    _payload, _price, vendor,
)


@pytest.mark.parametrize(("field", "label", "base_rate", "band_rate"), [
    ("input_cache_read", "read", 0.1, 0.7),
    ("input_cache_write", "5m write", 1.25, 8.75),
    ("input_cache_write_1h", "1h write", 2.0, 14.0),
])
def test_band_with_inconsistent_cache_ratio_is_not_silently_folded(
        field, label, base_rate, band_rate):
    """A single input factor must represent every listed cache component."""
    base = _price(1.0, 5.0, read=0.1, write=1.25, write_1h=2.0)
    band = _price(5.0, 25.0, read=0.5, write=6.25, write_1h=10.0,
                  min_prompt_tokens=100_000)
    band[field] = _per_token(band_rate)
    base["overrides"] = [band]
    doc, out = _run(_doc(), _catalog(GPT_ID), {
        GPT_ID: _payload(_endpoint("openai", base))})
    assert len(out.refusals) == 1 and not out.notices
    reason = out.refusals[0]
    assert f"cache {label}" in reason
    assert f"base {base_rate:g}" in reason and f"band {band_rate:g}" in reason
    assert "implied factor x7" in reason
    assert "meter factor x5" in reason
    assert GPT_KEY not in doc["long_context_models"]
    assert GPT_KEY not in doc["long_context_meters"]


def test_main_unrepresentable_cache_band_refuses_the_refresh(
        tmp_path, capsys):
    base = _price(1.0, 5.0, read=0.1, overrides=[
        {"min_prompt_tokens": 100_000, "prompt": _per_token(5.0),
         "completion": _per_token(25.0), "input_cache_read": _per_token(0.7)}])
    run = MainRun(tmp_path, _main_doc({}, []), _catalog(GPT_ID), {
        GPT_ID: _payload(_endpoint("openai", base))})

    rc, _out, err = run(capsys)

    assert rc == 1
    assert "cache read" in err and "implied factor x7" in err


def test_main_reprices_provider_row_when_its_band_factors_change(
        tmp_path, capsys):
    """Provider main accepts a changed coherent band while learning it."""
    old_meter = {"threshold": 100_000, "input_mult": 2.0,
                 "output_mult": 1.5}
    doc = _doc(tracked={GPT_KEY: dict(TRACKED)}, members=[GPT_KEY],
               meters={GPT_KEY: old_meter})
    doc["providers"] = {GPT_KEY: {"Vendor": [
        {"from": None, **RATES}]}}
    base = _price(2.0, 10.0, read=0.2, write=2.5, write_1h=4.0)
    base["overrides"] = [_band(
        2.0, 10.0, read=0.2, write=2.5, write_1h=4.0,
        threshold=100_000, input_mult=6.0, output_mult=3.0)]
    run = MainRun(tmp_path, doc, _catalog(GPT_ID), {
        GPT_ID: _payload(_endpoint("openai", base))})

    rc, _out, err = run(capsys)

    assert rc == 0, err
    updated = json.loads(run.pricing_path.read_text(encoding="utf-8"))
    history = updated["providers"][GPT_KEY]["Vendor"]
    assert len(history) == 2
    assert history[-1]["from"] == STAMP
    assert {field: history[-1][field] for field in RATE_FIELDS} == {
        "fresh": 2.0, "create_5m": 2.5, "create_1h": 4.0,
        "read": 0.2, "output": 10.0}
    assert updated["long_context_meters"][GPT_KEY] == {
        "threshold": 100_000, "input_mult": 6.0, "output_mult": 3.0}


def test_existing_meter_rewrite_report_keeps_meter_on():
    move = vendor.VendorMove(
        GPT_ID, GPT_KEY,
        meter={"threshold": 100_000, "input_mult": 5.0,
               "output_mult": 5.0})
    report = _load("refresh_report").vendor_report(
        "2031-01-01T00:00:00Z", vendor.VendorOutcome(moves=[move]))
    assert "[threshold 100000, input x5, output x5]" in report
    assert "long-context meter on" in report
    assert "long-context meter off" not in report
