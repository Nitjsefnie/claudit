"""Fuzz appends use chronological occurrences while retaining stored groups."""
from __future__ import annotations

import copy
import json
import random
from datetime import datetime, timedelta

from backend import pricing
from backend.pricing_document import expand_pricing_doc, serialize_pricing_doc
from tests.test_fuzz_test_data import fuzz_module
from tests.test_pricing_rows import HOST, ROW_KEY, _fixture


class Draws(random.Random):
    def __init__(self):
        super().__init__(0)
        self.values = iter([1, 1, 1, 0, 2, 3, 3, 3, 0, 4])

    def random(self):
        return 1.0

    def choice(self, seq):
        return next(self.values)


def test_grouped_floor_and_predecessor_use_the_latest_occurrence():
    history = [
        {"from": ["2031-01-01T00:00:00Z", "2031-01-01T00:20:00Z"],
         "fresh": 1, "read": 0, "output": 2, "note": fuzz_module.NOTE},
        {"from": "2031-01-01T00:10:00Z", "fresh": 2, "read": 0,
         "output": 3, "note": fuzz_module.NOTE},
    ]
    # pylint: disable=protected-access
    entry = fuzz_module._appended_entry(history, None, Draws())
    assert entry == {"from": "2031-01-01T00:20:01Z", "fresh": 3,
                     "read": 0, "output": 4, "note": fuzz_module.NOTE}


def test_grouped_document_max_and_fetch_stamp_include_every_occurrence():
    doc = _fixture()
    doc["providers"][ROW_KEY][HOST][1]["from"][1] = "2026-01-05T00:00:00Z"
    doc["provider_rates_fetched"] = "2026-01-01T00:00:00Z"
    # pylint: disable=protected-access
    assert fuzz_module._max_real_stamp(fuzz_module._rows(doc)) == datetime.fromisoformat(
        "2026-01-05T00:00:00+00:00")
    fuzz_module._bump_fetch_stamp(doc)
    assert doc["provider_rates_fetched"] == "2026-01-05T00:00:00Z"


def test_grouped_storage_survives_a_real_fuzz_iteration(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    path = root / "src" / "pricing.json"
    original = _fixture()
    original["providers"][ROW_KEY][HOST][1]["from"][1] = "2026-01-05T00:00:00Z"
    pristine = serialize_pricing_doc(original)
    path.write_text(pristine)
    monkeypatch.setattr(fuzz_module, "restore_baseline", lambda _root: path.write_text(pristine))
    monkeypatch.setattr(fuzz_module, "run_suite", lambda _root: (0, "suite seam"))
    monkeypatch.setattr(fuzz_module, "_choose_rows", lambda rows, _rng: rows)
    before = copy.deepcopy(original)
    result = fuzz_module.fuzz_iteration(root, 0, 7, tmp_path)
    saved = json.loads(path.read_text())
    decoded = expand_pricing_doc(saved)
    history = decoded["providers"][ROW_KEY][HOST]
    assert result["ok"]
    assert history[:-1] == expand_pricing_doc(before)["providers"][ROW_KEY][HOST]
    assert datetime.fromisoformat(history[-1]["from"]) == datetime.fromisoformat(
        "2026-01-05T00:00:00+00:00") + timedelta(seconds=1)
    assert before == original
    pricing.load_tables(saved)
