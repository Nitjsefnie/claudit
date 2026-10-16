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
from backend.pricing_document import omit_default_rates, serialize_pricing_doc
from backend.pricing_load import load_tables
from backend.pricing_rows import encode_pricing_doc, decode_pricing_doc
from scripts.ci import perturb_test_data
from tests.test_parser_js_pricing_loader import HOST, ROW_KEY, _doc
from tests.test_perturb_test_data import _run as _perturb_run
from tests.test_provider_rate_refresh import Run, _move_openinference

ROOT = Path(__file__).resolve().parents[1]
CONVERTER = ROOT / "scripts" / "ci" / "convert_pricing_doc.py"


def _synthetic_doc() -> dict:
    """An old-spelling document with rates, a fee, schedule, and meter."""
    doc = _doc()
    row = doc["providers"][ROW_KEY][HOST][0]
    rates = {"fresh": 2.0, "create_5m": 2.0, "create_1h": 2.0,
             "read": 0.2, "output": 6.0}
    row.update({"from": None, **rates, "web_search": 0.015})
    row["schedule"] = [
        {"days": ["monday"], "rates": dict(rates)},
        {"days": ["tuesday"], "rates": {
            **rates, "create_5m": 2.5, "create_1h": 3.0}},
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
    # The appended cutover must not postdate the fetch stamp (#931).
    doc["provider_rates_fetched"] = "2027-01-01T00:00:00Z"
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
    return doc


def _omit_equal_tiers(rates: dict) -> None:
    for field in ("create_5m", "create_1h"):
        if rates.get(field) == rates.get("fresh"):
            rates.pop(field, None)


def test_omitting_cache_defaults_without_fresh_returns_an_independent_copy():
    rates = {"create_5m": 2.0, "create_1h": 2.0, "read": 0.2, "output": 6.0}
    expected = dict(rates)

    copied = omit_default_rates(rates)

    assert copied == expected
    assert copied is not rates
    assert rates == expected
    copied["create_5m"] = 9.0
    assert rates == expected


def _js_tables(tmp_path: Path, doc: dict) -> dict:
    where = tmp_path / "node"
    where.mkdir(parents=True)
    (where / "pricing.json").write_text(
        json.dumps(encode_pricing_doc(doc), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for name in ("pricing-loader.js", "vendor-tables.js", "hhmm-spelling.js", "pricing-rows.js"):
        shutil.copy(ROOT / "src" / name, where / name)
    script = f"""
      global.window = {{}};
      require({str(where / 'pricing-loader.js')!r});
      console.log(JSON.stringify(window));
    """
    proc = subprocess.run(["node", "-e", script], capture_output=True,
                          text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_python_loader_folds_sparse_rates_and_leading_from():
    old, sparse = _synthetic_doc(), _sparse_doc(_synthetic_doc())

    old_tables = load_tables(old)
    sparse_tables = load_tables(sparse)

    assert sparse_tables == old_tables
    assert sparse_tables["PROVIDER_RATES"][ROW_KEY, HOST]["create_5m"] == 3.5
    end, window_rates = sparse_tables["PROVIDER_DATED_RATES"][ROW_KEY, HOST][0]
    assert end.isoformat() == "2027-01-01T00:00:00+00:00"
    assert window_rates["create_1h"] == 2.0
    schedule = sparse_tables["PROVIDER_SCHEDULES"][ROW_KEY, HOST][0]
    assert schedule[1][3]["create_5m"] == 2.5
    assert sparse_tables["LONG_CONTEXT_METERS"] == {
        "claude-opus-4-8": {"threshold": 100_000, "input_mult": 3.5},
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_browser_loader_folds_sparse_rates_and_leading_from(tmp_path):
    old, sparse = _synthetic_doc(), _sparse_doc(_synthetic_doc())

    old_tables = _js_tables(tmp_path / "old", old)
    sparse_tables = _js_tables(tmp_path / "sparse", sparse)

    assert sparse_tables == old_tables
    assert sparse_tables["providerRates"][ROW_KEY][HOST]["c5"] == 3.5
    assert sparse_tables["providerStarts"] == {}
    dated_rates = sparse_tables["providerDatedRates"][ROW_KEY][HOST][0]["rates"]
    assert dated_rates["c1h"] == 2.0
    schedules = sparse_tables["providerSchedules"][ROW_KEY][HOST]["0"]
    assert schedules[1]["rates"]["c5"] == 2.5
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
    assert text == serialize_pricing_doc(doc)
    expected = _sparse_doc(_synthetic_doc())
    expected["providers"][ROW_KEY][HOST][0]["from"] = None
    assert decode_pricing_doc(doc) == expected


def test_conversion_refuses_malformed_document_without_overwriting_it(tmp_path):
    doc = _synthetic_doc()
    del doc["providers"][ROW_KEY][HOST][0]["fresh"]
    path = tmp_path / "pricing.json"
    original = (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.write_bytes(original)

    result = _convert(path)

    assert result.returncode != 0
    assert f"{ROW_KEY} via {HOST}[0]" in result.stderr
    assert path.read_bytes() == original


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_perturber_writes_compact_entries_accepted_by_both_loaders(tmp_path):
    path = tmp_path / "pricing.json"
    path.write_text(json.dumps(_sparse_doc(_synthetic_doc()), indent=2,
                               sort_keys=True) + "\n", encoding="utf-8")

    perturb_test_data.perturb_pricing(path, seed=42)

    perturbed = decode_pricing_doc(json.loads(path.read_text(encoding="utf-8")))
    entries = perturbed["providers"][ROW_KEY][HOST]
    doubled = entries[-5]
    assert doubled["fresh"] == 6.0
    assert doubled["create_5m"] == 7.0
    assert "create_1h" not in doubled
    python_rates = load_tables(perturbed)["PROVIDER_RATES"][ROW_KEY, HOST]
    browser_rates = _js_tables(tmp_path, perturbed)["providerRates"][ROW_KEY][HOST]
    latest = entries[-1]
    assert browser_rates["fresh"] == python_rates["fresh"] == latest["fresh"]
    assert browser_rates["c1h"] == python_rates["create_1h"]


def test_a_quiet_run_over_a_converted_document_keeps_its_bytes(tmp_path, capsys):
    run = Run(tmp_path)
    converted = _convert(run.pricing)
    assert converted.returncode == 0, converted.stderr
    before = run.pricing.read_bytes()

    rc, out, err = run(capsys)

    assert rc == 0, err
    assert "no rate moved" in out
    assert run.pricing.read_bytes() == before


def test_the_perturbers_fetch_stamp_covers_its_appends(tmp_path):
    """The perturber moves provider_rates_fetched exactly to
    max(its old value, its newest appended stamp) — never backwards,
    never further (issue #931: no `from` postdates the fetch stamp) —
    and never touches the openrouter section. (Relocated here from the
    perturber's own file, which sits at its 700-line size ceiling.)"""
    # pylint: disable=protected-access
    doc, perturbed, _text, _path = _perturb_run(tmp_path)
    newest = max(perturb_test_data._parse_stamp(entry["from"])
                 for _key, entries, _may in perturb_test_data._rows(perturbed)
                 for entry in entries if entry.get("from"))
    fetched = perturb_test_data._parse_stamp(
        perturbed["provider_rates_fetched"])
    previous = perturb_test_data._parse_stamp(doc["provider_rates_fetched"])
    assert fetched == max(previous, newest), (
        "the stamp moves exactly to max(its old value, the newest append)")
    assert perturbed["openrouter"] == doc["openrouter"]


def test_a_written_move_omits_cache_rates_equal_to_fresh(tmp_path, capsys):
    run = Run(tmp_path)
    _move_openinference(run)

    rc, _out, err = run(capsys)

    assert rc == 0, err
    entry = decode_pricing_doc(json.loads(run.pricing.read_text(encoding="utf-8")))[
        "providers"]["glm-5-3-flash"]["OpenInference"][-1]
    assert "create_5m" not in entry
    assert "create_1h" not in entry


@pytest.mark.parametrize("alternate", [False, True])
def test_converter_bootstraps_with_a_legacy_canonical_file(tmp_path, alternate):
    repo = tmp_path / "checkout"
    (repo / "backend").mkdir(parents=True)
    (repo / "scripts" / "ci").mkdir(parents=True)
    (repo / "src").mkdir()
    for source in (ROOT / "backend").glob("*.py"):
        shutil.copy(source, repo / "backend" / source.name)
    script = repo / "scripts" / "ci" / CONVERTER.name
    shutil.copy(CONVERTER, script)
    canonical = repo / "src" / "pricing.json"
    legacy = json.dumps(_synthetic_doc()) + "\n"
    canonical.write_text(legacy, encoding="utf-8")
    target = tmp_path / "alternate.json" if alternate else canonical
    target.write_text(legacy, encoding="utf-8")
    args = [sys.executable, str(script), *(["--pricing", str(target)] if alternate else [])]
    first = subprocess.run(args, cwd=repo, capture_output=True, text=True,
                           check=False, timeout=60)
    assert first.returncode == 0, first.stderr
    converted = target.read_bytes()
    stored = json.loads(converted)
    assert stored["providers"][ROW_KEY][HOST][0][:4] == [None, 2.0, 6.0, 0.2]
    assert load_tables(stored) == load_tables(_synthetic_doc())
    second = subprocess.run(args, cwd=repo, capture_output=True, text=True,
                            check=False, timeout=60)
    assert second.returncode == 0, second.stderr
    assert target.read_bytes() == converted
    if alternate:
        assert canonical.read_text(encoding="utf-8") == legacy
