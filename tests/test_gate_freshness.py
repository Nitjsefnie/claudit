"""Unit tests for the gate-freshness head selection.

Once the aggregate becomes a required check, heads that predate it (or
never ran it) cannot go green until they rebase or rerun; this script
names that set so only they rebase, instead of the manager rebinding
blindly. The tests pin the selection logic: a head is stale when its
latest `ci gate / aggregate` run predates the first commit carrying
ci-gate.yml, or when it has none.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    """Import scripts/ci/<name>.py by path (scripts/ci is not a package)."""
    path = REPO_ROOT / "scripts" / "ci" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gf = _load("gate_freshness")

GATE_TIME = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)
BEFORE = "2026-09-25T11:00:00+00:00"
AFTER = "2026-09-25T13:00:00+00:00"


def _pr(number, base="master", sha="a" * 40):
    return {"number": number, "base": base, "sha": sha}


def test_no_run_at_all_means_stale():
    stale = gf.select_stale([_pr(7)], {"a" * 40: []}, GATE_TIME)
    assert [entry["number"] for entry in stale] == [7]
    assert "no" in stale[0]["reason"]


def test_run_predating_the_gate_commit_means_stale():
    stale = gf.select_stale(
        [_pr(7)], {"a" * 40: [{"id": 11, "run_started_at": BEFORE}]}, GATE_TIME)
    assert [entry["number"] for entry in stale] == [7]
    assert "11" in stale[0]["reason"]
    assert BEFORE in stale[0]["reason"]


def test_run_at_or_after_the_gate_commit_is_fresh():
    stale = gf.select_stale(
        [_pr(7)], {"a" * 40: [{"id": 11, "run_started_at": AFTER}]}, GATE_TIME)
    assert stale == []


def test_latest_run_decides():
    sha = "a" * 40
    runs = [{"id": 11, "run_started_at": BEFORE},
            {"id": 12, "run_started_at": AFTER}]
    assert gf.select_stale([_pr(7)], {sha: runs}, GATE_TIME) == []

    ordered = list(reversed(runs))
    assert gf.select_stale([_pr(7)], {sha: ordered}, GATE_TIME) == []


def test_earlier_run_order_in_the_list_does_not_decide():
    # The latest run is selected by (started_at, id), never by list order.
    sha = "a" * 40
    runs = [{"id": 99, "run_started_at": BEFORE},
            {"id": 12, "run_started_at": BEFORE}]
    stale = gf.select_stale([_pr(7)], {sha: runs}, GATE_TIME)
    assert len(stale) == 1
    assert "99" in stale[0]["reason"]


def test_unreadable_start_time_counts_as_stale():
    # A run whose start time cannot be read cannot be proven at or after
    # the gate commit, so it must not read as fresh.
    stale = gf.select_stale(
        [_pr(7)], {"a" * 40: [{"id": 11, "started_at": "garbage"}]},
        GATE_TIME)
    assert len(stale) == 1


def test_pull_requests_off_the_gated_base_are_not_reported():
    stale = gf.select_stale(
        [_pr(1, base="main"), _pr(2, base="master")], {}, GATE_TIME)
    assert [entry["number"] for entry in stale] == [2]


def test_unusable_head_sha_is_skipped():
    stale = gf.select_stale(
        [_pr(1, sha="nope"), _pr(2)], {}, GATE_TIME)
    assert [entry["number"] for entry in stale] == [2]


def test_two_stale_heads_are_both_reported():
    stale = gf.select_stale([_pr(1, sha="a" * 40), _pr(2, sha="b" * 40)],
                            {}, GATE_TIME)
    assert [entry["number"] for entry in stale] == [1, 2]


def test_summary_renders_the_stale_list(tmp_path):
    stale = gf.select_stale([_pr(7)], {"a" * 40: []}, GATE_TIME)
    summary = tmp_path / "summary.md"
    gf.write_summary(str(summary), stale, scanned=1)
    text = summary.read_text(encoding="utf-8")
    assert "#7" in text
    assert "no" in text


def test_summary_with_no_stale_heads_says_so(tmp_path):
    summary = tmp_path / "summary.md"
    gf.write_summary(str(summary), [], scanned=3)
    text = summary.read_text(encoding="utf-8")
    assert "3" in text
