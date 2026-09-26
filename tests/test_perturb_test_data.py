"""SV-TEST-DATA's second half: scripts/ci/perturb_test_data.py under test.

The CI leg this script serves runs the suite against a perturbed tree
that simulates what the automated refresh does — every rate row gains a
doubled newest entry and the three version constants move up one — so a
test that pins repository-managed data fails as a test failure. These
tests drive the perturbation over a MINIMAL synthetic pricing.json in
tmp_path (never the repo's real file) and a synthetic constants bumper.
"""
from __future__ import annotations

import importlib.util
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend import pricing

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ci" / "perturb_test_data.py"

RATE_FIELDS = ("fresh", "create_5m", "create_1h", "read", "output")
RATES_A = {"fresh": 1.0, "create_5m": 1.25, "create_1h": 2.0,
           "read": 0.1, "output": 5.0}
RATES_B = {"fresh": 2.0, "create_5m": 2.5, "create_1h": 4.0,
           "read": 0.2, "output": 10.0}
RATES_ZERO = {"fresh": 0, "create_5m": 0, "create_1h": 0, "read": 0,
              "output": 0}
NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=timezone.utc)


def _load():
    spec = importlib.util.spec_from_file_location("perturb_test_data", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["perturb_test_data"] = module
    spec.loader.exec_module(module)
    return module


perturb_module = _load()


def _seed_doc() -> dict:
    """One dated model row, one all-zero row, one scheduled provider row."""
    return {
        "models": {
            "acme/acme-9": [
                {"from": None, **RATES_A},
                {"from": "2026-06-01T00:00:00Z", **RATES_B},
            ],
            "free/acme-0": [{"from": None, **RATES_ZERO}],
        },
        "providers": {
            "acme/acme-9": {
                "HostCo": [{"from": "2026-01-01T00:00:00Z", **RATES_A,
                            "schedule": [{"days": ["saturday", "sunday"],
                                          "start": 2200, "end": 200,
                                          "rates": RATES_B}]}],
            },
        },
        "provider_rates_fetched": "2026-06-01T00:00:00Z",
        "openrouter": {"data_region": "global", "models": {}},
    }


def _seed_tree(tmp_path: Path) -> tuple[Path, Path, dict]:
    doc = _seed_doc()
    pricing_path = tmp_path / "pricing.json"
    constants_path = tmp_path / "constants.py"
    pricing_path.write_text(
        json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    constants_path.write_text(
        '"""Synthetic constants."""\n'
        'PARSER_VERSION = "81"\n'
        'PRICING_VERSION = "5"\n'
        'MARKER_READER_VERSION = "1"\n'
        'OTHER = "untouched"\n',
        encoding="utf-8")
    return pricing_path, constants_path, doc


def _run(tmp_path: Path) -> tuple[dict, dict, str, Path]:
    """Seed, run the perturbation once, read the tree back."""
    pricing_path, constants_path, doc = _seed_tree(tmp_path)
    perturb_module.perturb_pricing(pricing_path, now=NOW)
    perturb_module.bump_constants(constants_path)
    perturbed = json.loads(pricing_path.read_text(encoding="utf-8"))
    text = pricing_path.read_text(encoding="utf-8")
    return doc, perturbed, text, constants_path


def _run_twice(tmp_path: Path) -> tuple[dict, dict, Path]:
    """Seed, run the perturbation twice, read the tree back."""
    pricing_path, constants_path, doc = _seed_tree(tmp_path)
    for _ in range(2):
        perturb_module.perturb_pricing(pricing_path, now=NOW)
        perturb_module.bump_constants(constants_path)
    perturbed = json.loads(pricing_path.read_text(encoding="utf-8"))
    return doc, perturbed, constants_path


def test_every_row_gains_exactly_one_entry(tmp_path):
    doc, perturbed, _text, _path = _run(tmp_path)
    for key, entries in doc["models"].items():
        assert len(perturbed["models"][key]) == len(entries) + 1
        assert perturbed["models"][key][:-1] == entries
    for model, hosts in doc["providers"].items():
        for host, entries in hosts.items():
            got = perturbed["providers"][model][host]
            assert len(got) == len(entries) + 1
            assert got[:-1] == entries


def test_every_new_entry_comes_after_its_predecessor(tmp_path):
    _doc, perturbed, _text, _path = _run(tmp_path)
    histories = [perturbed["models"][key] for key in perturbed["models"]]
    histories += [perturbed["providers"][m][h]
                  for m, hosts in perturbed["providers"].items()
                  for h in hosts]
    for entries in histories:
        for before, after in zip(entries, entries[1:]):
            previous = before["from"]
            stamp = after["from"]
            assert isinstance(stamp, str)
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            assert parsed.tzinfo is not None
            if previous is not None:
                earlier = datetime.fromisoformat(
                    previous.replace("Z", "+00:00"))
                assert parsed > earlier


def test_the_new_rates_differ_and_double_the_previous_newest(tmp_path):
    doc, perturbed, _text, _path = _run(tmp_path)
    histories = ([perturbed["models"][key] for key in perturbed["models"]]
                 + [perturbed["providers"][m][h]
                    for m, hosts in perturbed["providers"].items()
                    for h in hosts])
    originals = ([doc["models"][key] for key in doc["models"]]
                 + [doc["providers"][m][h]
                    for m, hosts in doc["providers"].items()
                    for h in hosts])
    for entries, original in zip(histories, originals):
        newest, previous = entries[-1], original[-1]
        for field in RATE_FIELDS:
            assert newest[field] != previous[field]
            assert newest[field] == previous[field] * 2 or (
                previous[field] == 0 and newest[field] == 1)
            assert math.isfinite(newest[field]) and newest[field] >= 0
        assert "perturbation" in newest["note"]


def test_the_schedule_is_untouched(tmp_path):
    doc, perturbed, _text, _path = _run(tmp_path)
    original = doc["providers"]["acme/acme-9"]["HostCo"][0]["schedule"]
    kept = perturbed["providers"]["acme/acme-9"]["HostCo"][0]["schedule"]
    assert kept == original
    appended = perturbed["providers"]["acme/acme-9"]["HostCo"][-1]
    assert "schedule" not in appended


def test_the_perturbed_doc_passes_the_loaders_validation(tmp_path):
    _doc, perturbed, _text, _path = _run(tmp_path)
    tables = pricing.load_tables(perturbed)
    doubled = {field: value * 2 for field, value in RATES_B.items()}
    assert tables["MODEL_RATES"]["acme/acme-9"] == doubled
    ones = {field: 1 for field in RATE_FIELDS}
    assert tables["MODEL_RATES"]["free/acme-0"] == ones
    assert len(tables["RATE_EPOCHS"]) > 0


def test_the_file_stays_in_the_canonical_layout(tmp_path):
    _doc, perturbed, text, _path = _run(tmp_path)
    assert text == json.dumps(perturbed, indent=2, sort_keys=True) + "\n"


def test_the_constants_file_bumps_exactly_plus_one(tmp_path):
    _doc, _perturbed, _text, constants_path = _run(tmp_path)
    lines = constants_path.read_text(encoding="utf-8").splitlines()
    assert 'PARSER_VERSION = "82"' in lines
    assert 'PRICING_VERSION = "6"' in lines
    assert 'MARKER_READER_VERSION = "2"' in lines
    assert 'OTHER = "untouched"' in lines
    assert len(lines) == 5


def test_a_future_newest_from_advances_by_one_second(tmp_path):
    pricing_path, _constants_path, doc = _seed_tree(tmp_path)
    future = NOW + timedelta(days=30)
    doc["models"]["acme/acme-9"][-1]["from"] = future.strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    pricing_path.write_text(
        json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    perturb_module.perturb_pricing(pricing_path, now=NOW)
    perturbed = json.loads(pricing_path.read_text(encoding="utf-8"))
    stamped = perturbed["models"]["acme/acme-9"][-1]["from"]
    assert stamped == (future + timedelta(seconds=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def test_a_second_run_appends_a_further_entry_without_corrupting(tmp_path):
    doc, perturbed, constants_path = _run_twice(tmp_path)
    for key, entries in doc["models"].items():
        assert len(perturbed["models"][key]) == len(entries) + 2
    for model, hosts in doc["providers"].items():
        for host, entries in hosts.items():
            assert len(perturbed["providers"][model][host]) == len(entries) + 2
    assert pricing.load_tables(perturbed)
    lines = constants_path.read_text(encoding="utf-8").splitlines()
    assert 'PARSER_VERSION = "83"' in lines
    assert 'PRICING_VERSION = "7"' in lines
    assert 'MARKER_READER_VERSION = "3"' in lines


def test_the_script_answers_help(capsys):
    with pytest.raises(SystemExit) as exit_info:
        perturb_module.main(["--help"])
    assert exit_info.value.code == 0
    assert "perturb" in capsys.readouterr().out
