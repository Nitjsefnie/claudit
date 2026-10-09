"""Sparse pricing.json cache tiers fold to the same effective document."""
from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from backend import pricing, rate_fingerprint
from backend.pricing_load import load_tables
from tests.test_parser_js_pricing_loader import HOST, ROW_KEY, _doc
from tests.test_provider_rate_refresh import Run, _move_openinference

ROOT = Path(__file__).resolve().parents[1]
CONVERTER = ROOT / "scripts" / "ci" / "convert_pricing_doc.py"
LOADER_JS = ROOT / "src" / "pricing-loader.js"


def _synthetic_doc() -> dict:
    """An old-spelling document with rates, a fee, schedule, band, and meter."""
    doc = _doc()
    row = doc["providers"][ROW_KEY][HOST][0]
    row.update({"from": None, "fresh": 2.0, "create_5m": 2.0,
                "create_1h": 2.0, "read": 0.2, "output": 6.0,
                "web_search": 0.015})
    row["band"] = {
        "fresh": [1.0, 3.0], "create_5m": [1.0, 3.0],
        "create_1h": [1.0, 3.0], "read": [0.1, 0.3],
        "output": [4.0, 8.0],
    }
    row["schedule"] = [
        {"days": ["monday"], "rates": {
            "fresh": 2.0, "create_5m": 2.0, "create_1h": 2.0,
            "read": 0.2, "output": 6.0,
        }},
        {"days": ["tuesday"], "rates": {
            "fresh": 2.0, "create_5m": 2.5, "create_1h": 3.0,
            "read": 0.2, "output": 6.0,
        }},
    ]
    doc["providers"][ROW_KEY][HOST].append({
        "from": "2027-01-01T00:00:00Z", "fresh": 3.0,
        "create_5m": 3.5, "create_1h": 3.0, "read": 0.3,
        "output": 7.0, "web_search": 0.015,
    })
    doc["long_context_meters"] = [{
        "threshold": 100_000,
        "models": [{"claude-opus-4-8": {"input_mult": 3.5}}],
    }]
    return doc


def _sparse_doc(old: dict) -> dict:
    """Spell the conversion independently of production code."""
    doc = copy.deepcopy(old)
    for history in [*doc["models"].values(),
                    *(history for hosts in doc["providers"].values()
                      for history in hosts.values())]:
        for index, entry in enumerate(history):
            if index == 0 and entry.get("from") is None:
                entry.pop("from", None)
            _omit_equal_tiers(entry)
            for window in entry.get("schedule", []):
                _omit_equal_tiers(window["rates"])
            band = entry.get("band")
            if band and "fresh" in band:
                for field in ("create_5m", "create_1h"):
                    if band.get(field) == band["fresh"]:
                        band.pop(field)
    return doc


def _omit_equal_tiers(rates: dict) -> None:
    for field in ("create_5m", "create_1h"):
        if rates.get(field) == rates.get("fresh"):
            rates.pop(field, None)


def _js_tables(tmp_path: Path, doc: dict) -> dict:
    where = tmp_path / "node"
    where.mkdir(parents=True)
    (where / "pricing.json").write_text(
        json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for name in ("pricing-loader.js", "vendor-tables.js", "hhmm-spelling.js"):
        shutil.copy(ROOT / "src" / name, where / name)
    script = f"""
      global.window = {{}};
      require({str(where / 'pricing-loader.js')!r});
      console.log(JSON.stringify({{
        models: window.modelRates,
        dated: window.datedRates,
        providers: window.providerRates,
        providerDated: window.providerDatedRates,
        providerStarts: window.providerStarts,
        providerSchedules: window.providerSchedules,
        providerBands: window.providerBands,
        vendorBare: window.vendorBare,
        vendorPrefixes: window.vendorPrefixes,
        vendorHosts: window.vendorHosts,
        longContextMeters: window.longContextMeters,
      }}));
    """
    proc = subprocess.run(["node", "-e", script], capture_output=True,
                          text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_python_loader_folds_sparse_rates_bands_and_leading_from():
    old, sparse = _synthetic_doc(), _sparse_doc(_synthetic_doc())

    old_tables = load_tables(old)
    sparse_tables = load_tables(sparse)

    assert sparse_tables == old_tables
    assert sparse_tables["PROVIDER_RATES"][ROW_KEY, HOST] == {
        "fresh": 3.0, "create_5m": 3.5, "create_1h": 3.0,
        "read": 0.3, "output": 7.0, "web_search": 0.015,
    }
    assert sparse_tables["PROVIDER_STARTS"] == {}
    end, window_rates = sparse_tables["PROVIDER_DATED_RATES"][ROW_KEY, HOST][0]
    assert end.isoformat() == "2027-01-01T00:00:00+00:00"
    assert window_rates == {
        "fresh": 2.0, "create_5m": 2.0, "create_1h": 2.0,
        "read": 0.2, "output": 6.0, "web_search": 0.015,
    }
    assert sparse_tables["PROVIDER_BANDS"][ROW_KEY, HOST][0] == {
        "fresh": [1.0, 3.0], "create_5m": [1.0, 3.0],
        "create_1h": [1.0, 3.0], "read": [0.1, 0.3],
        "output": [4.0, 8.0],
    }
    schedule = sparse_tables["PROVIDER_SCHEDULES"][ROW_KEY, HOST][0]
    assert schedule[0][3] == {
        "fresh": 2.0, "create_5m": 2.0, "create_1h": 2.0,
        "read": 0.2, "output": 6.0,
    }
    assert schedule[1][3] == {
        "fresh": 2.0, "create_5m": 2.5, "create_1h": 3.0,
        "read": 0.2, "output": 6.0,
    }
    assert sparse_tables["LONG_CONTEXT_METERS"] == {
        "claude-opus-4-8": {"threshold": 100_000, "input_mult": 3.5},
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_browser_loader_folds_sparse_rates_bands_and_leading_from(tmp_path):
    old, sparse = _synthetic_doc(), _sparse_doc(_synthetic_doc())

    old_tables = _js_tables(tmp_path / "old", old)
    sparse_tables = _js_tables(tmp_path / "sparse", sparse)

    assert sparse_tables == old_tables
    assert sparse_tables["providers"][ROW_KEY][HOST] == {
        "fresh": 3.0, "c5": 3.5, "c1h": 3.0,
        "read": 0.3, "out": 7.0, "search": 0.015,
    }
    assert sparse_tables["providerStarts"] == {}
    assert sparse_tables["providerDated"][ROW_KEY][HOST][0] == {
        "endExclusive": 1_798_761_600_000,
        "rates": {"fresh": 2.0, "c5": 2.0, "c1h": 2.0,
                  "read": 0.2, "out": 6.0, "search": 0.015},
    }
    assert sparse_tables["providerBands"][ROW_KEY][HOST]["0"] == {
        "fresh": [1.0, 3.0], "create_5m": [1.0, 3.0],
        "create_1h": [1.0, 3.0], "read": [0.1, 0.3],
        "output": [4.0, 8.0],
    }
    assert [window["rates"] for window in
            sparse_tables["providerSchedules"][ROW_KEY][HOST]["0"]] == [
        {"fresh": 2.0, "c5": 2.0, "c1h": 2.0, "read": 0.2, "out": 6.0},
        {"fresh": 2.0, "c5": 2.5, "c1h": 3.0, "read": 0.2, "out": 6.0},
    ]
    assert sparse_tables["longContextMeters"] == {
        "claude-opus-4-8": {"threshold": 100_000, "input_mult": 3.5},
    }


def test_rate_fingerprints_are_equal_for_old_and_sparse_spellings(monkeypatch):
    old, sparse = _synthetic_doc(), _sparse_doc(_synthetic_doc())
    pairs = [("claude-opus-4-7", None), ("claude-opus-4-8", None),
             (ROW_KEY, HOST), (ROW_KEY, None)]
    fingerprints = []

    for doc in (old, sparse):
        for name, value in load_tables(doc).items():
            if hasattr(pricing, name):
                monkeypatch.setattr(pricing, name, value)
        rate_fingerprint.clear_fingerprint_cache()
        fingerprints.append({pair: rate_fingerprint.pair_fingerprint(*pair)
                             for pair in pairs})

    assert fingerprints[0] == fingerprints[1]


def _convert(path: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(CONVERTER), "--pricing", str(path)],
                          capture_output=True, text=True, timeout=60, check=False)


def test_conversion_is_idempotent_and_keeps_canonical_layout(tmp_path):
    path = tmp_path / "pricing.json"
    path.write_text(json.dumps(_synthetic_doc(), indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")

    first = _convert(path)
    assert first.returncode == 0, first.stderr
    converted = path.read_bytes()
    second = _convert(path)

    assert second.returncode == 0, second.stderr
    assert path.read_bytes() == converted
    text = converted.decode("utf-8")
    doc = json.loads(text)
    assert text == json.dumps(doc, indent=2, sort_keys=True) + "\n"
    first_entry = doc["providers"][ROW_KEY][HOST][0]
    assert "from" not in first_entry
    assert "create_5m" not in first_entry and "create_1h" not in first_entry
    assert doc["providers"][ROW_KEY][HOST][1]["create_5m"] == 3.5
    assert "create_1h" not in doc["providers"][ROW_KEY][HOST][1]
    assert "create_5m" not in first_entry["band"]
    assert "create_1h" not in first_entry["band"]
    assert "create_5m" not in first_entry["schedule"][0]["rates"]
    assert "create_1h" not in first_entry["schedule"][0]["rates"]
    assert first_entry["schedule"][1]["rates"]["create_5m"] == 2.5


def test_conversion_refuses_malformed_document_without_overwriting_it(tmp_path):
    doc = _synthetic_doc()
    doc["models"]["claude-opus-4-8"][0]["fresh"] = -1
    path = tmp_path / "pricing.json"
    original = (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.write_bytes(original)

    result = _convert(path)

    assert result.returncode != 0
    assert "claude-opus-4-8" in result.stderr
    assert path.read_bytes() == original


def test_a_quiet_run_over_a_converted_document_keeps_its_bytes(tmp_path, capsys):
    run = Run(tmp_path)
    converted = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "ci" / "convert_pricing_doc.py"),
         "--pricing", str(run.pricing)], capture_output=True, text=True,
        timeout=60, check=False)
    assert converted.returncode == 0, converted.stderr
    before = run.pricing.read_bytes()

    rc, out, err = run(capsys)

    assert rc == 0, err
    assert "no rate moved" in out
    assert run.pricing.read_bytes() == before


def test_a_written_move_omits_cache_rates_equal_to_fresh(tmp_path, capsys):
    run = Run(tmp_path)
    _move_openinference(run)

    rc, _out, err = run(capsys)

    assert rc == 0, err
    entry = json.loads(run.pricing.read_text(encoding="utf-8"))[
        "providers"]["glm-5-3-flash"]["OpenInference"][-1]
    assert entry.get("create_5m", entry["fresh"]) == entry["fresh"]
    assert entry.get("create_1h", entry["fresh"]) == entry["fresh"]
    assert "create_5m" not in entry
    assert "create_1h" not in entry
