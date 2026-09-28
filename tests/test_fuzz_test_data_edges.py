"""Focused coverage for reserved fuzz rows and shard crash cleanup."""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

from backend import pricing

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ci" / "fuzz_test_data.py"
RESERVED_NAMESPACE = "zz-fuzz-local/"
RATE_FIELDS = pricing.RATE_FIELDS
RATES_A = {"fresh": 1.0, "create_5m": 1.25, "create_1h": 2.0,
           "read": 0.1, "output": 5.0}
RATES_B = {"fresh": 2.0, "create_5m": 2.5, "create_1h": 4.0,
           "read": 0.2, "output": 10.0}
FLOOR_STAMP = "2026-06-01T00:00:00Z"


def _load_fuzzer():
    spec = importlib.util.spec_from_file_location(
        "fuzz_test_data_edges", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["fuzz_test_data_edges"] = module
    spec.loader.exec_module(module)
    return module


fuzz_module = _load_fuzzer()


def _seed_document() -> dict:
    return {
        "models": {
            "acme/existing-9": [
                {"from": None, **RATES_A},
                {"from": FLOOR_STAMP, **RATES_B},
            ],
        },
        "providers": {
            "acme/existing-9": {
                "KnownHost": [{"from": FLOOR_STAMP, **RATES_B}],
            },
        },
        "provider_rates_fetched": FLOOR_STAMP,
        "openrouter": {"data_region": "global", "models": {}},
    }


def _all_provider_rows(doc: dict) -> set[tuple[str, str]]:
    return {(model, host) for model, hosts in doc["providers"].items()
            for host in hosts}


def _stamp(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _prepare_fuzz_tree(monkeypatch: pytest.MonkeyPatch,
                       tmp_path: Path) -> tuple[Path, Path, dict]:
    repo = tmp_path / "repo"
    pricing_path = repo / "src" / "pricing.json"
    pricing_path.parent.mkdir(parents=True)
    original = _seed_document()
    pristine = json.dumps(original, indent=2, sort_keys=True) + "\n"
    pricing_path.write_text(pristine, encoding="utf-8")

    def restore(_root: Path) -> None:
        pricing_path.write_text(pristine, encoding="utf-8")

    monkeypatch.setattr(fuzz_module, "restore_baseline", restore)
    monkeypatch.setattr(fuzz_module, "run_suite",
                        lambda _root: (0, "suite ok"))
    return repo, pricing_path, original


def _assert_new_model_row(doc: dict, key: str, result: dict) -> None:
    entry = doc["models"][key][0]
    assert key.startswith(RESERVED_NAMESPACE)
    assert entry["from"] is None
    assert set(entry) == {"from", "note", *RATE_FIELDS}
    assert key in result["keys"]


def _assert_new_host_row(doc: dict, row: tuple[str, str],
                         result: dict) -> None:
    model, host = row
    entry = doc["providers"][model][host][0]
    assert host.startswith(RESERVED_NAMESPACE)
    assert set(entry) == {"from", "note", *RATE_FIELDS}
    assert (entry["from"] is None
            or _stamp(entry["from"]) > _stamp(FLOOR_STAMP))
    assert f"{model} via {host}" in result["keys"]


def _check_seeded_iteration(repo: Path, pricing_path: Path, original: dict,
                            iteration: int) -> tuple[set[str], bool]:
    result = fuzz_module.fuzz_iteration(repo, iteration, 263, pricing_path.parent)
    doc = json.loads(pricing_path.read_text(encoding="utf-8"))
    new_models = set(doc["models"]) - original["models"].keys()
    new_hosts = _all_provider_rows(doc) - _all_provider_rows(original)
    assert len(new_models) + len(new_hosts) <= 1
    if new_models:
        _assert_new_model_row(doc, next(iter(new_models)), result)
    if new_hosts:
        _assert_new_host_row(doc, next(iter(new_hosts)), result)
    assert result["rows_touched"] == len(result["keys"])
    assert pricing.load_tables(doc)
    assert pricing_path.read_text(encoding="utf-8") == (
        json.dumps(doc, indent=2, sort_keys=True) + "\n")
    return ({"model"} if new_models else set()) | \
        ({"host"} if new_hosts else set()), bool(new_models or new_hosts)


def test_seeded_iterations_add_valid_reserved_model_or_host_rows(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """New rows are valid and use names isolated from live test models."""
    repo, pricing_path, original = _prepare_fuzz_tree(monkeypatch, tmp_path)
    seen_kinds: set[str] = set()
    iterations_with_new_rows = 0
    for iteration in range(100):
        kinds, added_row = _check_seeded_iteration(
            repo, pricing_path, original, iteration)
        seen_kinds.update(kinds)
        iterations_with_new_rows += added_row

    assert seen_kinds == {"model", "host"}
    assert 0 < iterations_with_new_rows < 100


class _OwnedChild:
    """A child whose wait call records whether the parent joined it."""

    def __init__(self, exit_code: int):
        self.exit_code = exit_code
        self.waited = False

    def wait(self) -> int:
        self.waited = True
        return self.exit_code


def test_a_crashed_shard_waits_for_all_owned_children_before_raising(
        tmp_path: Path) -> None:
    missing = tmp_path / "crashed-result.json"
    successful = tmp_path / "successful-result.json"
    successful.write_text(json.dumps({"results": []}), encoding="utf-8")
    crashed_child = _OwnedChild(2)
    remaining_child = _OwnedChild(0)

    with pytest.raises(RuntimeError, match="without a result file"):
        # pylint: disable-next=protected-access
        fuzz_module._collect_shards([
            (tmp_path / "shard-crashed", missing, crashed_child),
            (tmp_path / "shard-running", successful, remaining_child),
        ])

    assert crashed_child.waited
    assert remaining_child.waited
