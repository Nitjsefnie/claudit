"""Tests for the test-speed comparator.

The comparator is a gate, so its own failure modes matter more than most
code here: a false red teaches people to ignore it, and a false green
means the gate is decorative. The cases below are exactly the ways it
could be wrong — added tests, removed tests, flaky rounds, non-passing
testcases — plus the pairing arithmetic and the median verdict.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


def _load():
    """Import scripts/ci/compare_durations.py by path.

    scripts/ci is not a package and deliberately has no __init__.py — it
    holds standalone CI entry points, not an importable library.
    """
    path = REPO_ROOT / "scripts" / "ci" / "compare_durations.py"
    spec = importlib.util.spec_from_file_location("compare_durations", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["compare_durations"] = module
    spec.loader.exec_module(module)
    return module


cd = _load()


def write_junit(path: Path, cases: dict, not_passed: dict | None = None) -> Path:
    """Minimal but real pytest --junitxml output."""
    not_passed = not_passed or {}
    parts = ['<?xml version="1.0" encoding="utf-8"?>', "<testsuites><testsuite>"]
    for nid, seconds in cases.items():
        classname, _, name = nid.partition("::")
        child = not_passed.get(nid)
        body = f"<{child}/>" if child else ""
        parts.append(
            f'<testcase classname="{classname}" name="{name}" '
            f'time="{seconds}">{body}</testcase>'
        )
    parts.append("</testsuite></testsuites>")
    path.write_text("".join(parts), encoding="utf-8")
    return path


def test_node_id_joins_class_and_name(tmp_path):
    report = write_junit(tmp_path / "r.xml", {"tests.test_a::test_one": 1.0})
    assert set(cd.parse_junit(report)) == {"tests.test_a::test_one"}


def test_non_passing_cases_are_excluded(tmp_path):
    report = write_junit(
        tmp_path / "r.xml",
        {"m::ok": 1.0, "m::bad": 9.0, "m::skip": 9.0},
        not_passed={"m::bad": "failure", "m::skip": "skipped"},
    )
    # A failure can be fast or slow for reasons unrelated to speed; a skip
    # is not a measurement at all.
    assert set(cd.parse_junit(report)) == {"m::ok"}


def test_pair_ratio_is_head_over_base_over_the_shared_tests():
    base = {"m::a": 2.0, "m::b": 8.0}
    head = {"m::a": 3.0, "m::b": 9.0}

    pair = cd.pair_totals(base, head)

    assert pair["ratio"] == pytest.approx(12.0 / 10.0)
    assert pair["base_total"] == pytest.approx(10.0)
    assert pair["head_total"] == pytest.approx(12.0)


def test_added_tests_cannot_trip_the_gate():
    """The whole reason a pair intersects on node id."""
    base = {"m::a": 1.0, "m::b": 1.0}
    head = {"m::a": 1.0, "m::b": 1.0, "m::brand_new": 50.0}

    pair = cd.pair_totals(base, head)

    assert pair["ratio"] == pytest.approx(1.0)


def test_removed_tests_cannot_hide_a_regression():
    base = {"m::a": 1.0, "m::slow_one_being_deleted": 100.0}
    head = {"m::a": 2.0}

    pair = cd.pair_totals(base, head)

    # Total wall time fell from 101s to 2s; the shared test still doubled.
    assert pair["ratio"] == pytest.approx(2.0)


def test_no_shared_tests_in_a_pair_is_an_error_not_a_pass():
    with pytest.raises(cd.ComparisonError, match="share no passing test"):
        cd.pair_totals({"m::a": 1.0}, {"m::z": 1.0})


def test_zero_baseline_total_is_an_error_not_a_division_by_zero():
    with pytest.raises(cd.ComparisonError, match="baseline total is zero"):
        cd.pair_totals({"m::a": 0.0}, {"m::a": 1.0})


def test_pairs_are_formed_in_argument_order(tmp_path):
    """base-i pairs with head-i: the run step emits rounds in that order."""
    base_slow = write_junit(tmp_path / "base-1.xml", {"m::a": 1.0})
    base_fast = write_junit(tmp_path / "base-2.xml", {"m::a": 3.0})
    head_slow = write_junit(tmp_path / "head-1.xml", {"m::a": 2.0})
    head_fast = write_junit(tmp_path / "head-2.xml", {"m::a": 2.4})

    result = cd.compare([str(base_slow), str(base_fast)],
                        [str(head_slow), str(head_fast)])

    assert result["pairs"][0]["ratio"] == pytest.approx(2.0)
    assert result["pairs"][1]["ratio"] == pytest.approx(0.8)


def test_unequal_round_counts_are_an_error(tmp_path):
    base = write_junit(tmp_path / "base-1.xml", {"m::a": 1.0})
    base2 = write_junit(tmp_path / "base-2.xml", {"m::a": 1.0})
    head = write_junit(tmp_path / "head-1.xml", {"m::a": 1.0})

    with pytest.raises(cd.ComparisonError, match="same number"):
        cd.compare([str(base), str(base2)], [str(head)])


def test_no_reports_at_all_is_an_error():
    with pytest.raises(cd.ComparisonError, match="no JUnit reports given"):
        cd.compare([], ["something.xml"])


def test_verdict_is_the_median_of_the_paired_ratios(tmp_path):
    """ROUNDS=2 gives two pairs; their median is the mean of the two."""
    base1 = write_junit(tmp_path / "base-1.xml", {"m::a": 1.0})
    head1 = write_junit(tmp_path / "head-1.xml", {"m::a": 1.0})
    base2 = write_junit(tmp_path / "base-2.xml", {"m::a": 1.0})
    head2 = write_junit(tmp_path / "head-2.xml", {"m::a": 2.0})

    result = cd.compare([str(base1), str(base2)], [str(head1), str(head2)])

    assert result["median_ratio"] == pytest.approx(1.5)


def test_the_all_round_common_set_feeds_the_reported_counts(tmp_path):
    """A test that did not pass in every round is context, not a mover.

    "Removed" and "new" mean stable on one side and absent from the
    other; a test that flaked out of one round on both sides is neither.
    """
    base1 = write_junit(tmp_path / "base-1.xml",
                        {"m::t": 1.0, "m::flaky": 1.0, "m::gone": 2.0})
    base2 = write_junit(tmp_path / "base-2.xml", {"m::t": 1.0, "m::gone": 2.0})
    head1 = write_junit(tmp_path / "head-1.xml",
                        {"m::t": 1.0, "m::flaky": 1.0, "m::brand": 50.0})
    head2 = write_junit(tmp_path / "head-2.xml",
                        {"m::t": 1.0, "m::brand": 50.0})

    result = cd.compare([str(base1), str(base2)], [str(head1), str(head2)])

    assert result["shared"] == 1
    assert result["base_only"] == ["m::gone"]
    assert result["head_only"] == ["m::brand"]
    # The flaky test appears in neither count, and still counts inside
    # pair 1's totals — there it passed in both reports.
    assert "m::flaky" not in result["base_only"] + result["head_only"]
    assert result["pairs"][0]["shared"] == 2


def test_sub_50ms_tests_stay_out_of_the_table_but_count_in_the_pair_totals(tmp_path):
    rounds = []
    for i, cases in enumerate(
        [
            ({"m::tiny": 0.002, "m::real": 1.0}, {"m::tiny": 0.002, "m::real": 1.0}),
            ({"m::tiny": 0.008, "m::real": 1.0}, {"m::tiny": 0.008, "m::real": 1.0}),
        ],
        start=1,
    ):
        base, head = cases
        rounds.append(
            (
                write_junit(tmp_path / f"base-{i}.xml", base),
                write_junit(tmp_path / f"head-{i}.xml", head),
            )
        )

    result = cd.compare(
        [str(b) for b, _ in rounds], [str(h) for _, h in rounds]
    )

    # 4x on a 2 ms test is noise and would bury genuine entries.
    assert [row[2] for row in result["per_test"]] == ["m::real"]
    # ...but it is still in the pair totals, where it belongs.
    assert result["pairs"][1]["head_total"] == pytest.approx(1.008)


def test_per_test_rows_compare_round_medians(tmp_path):
    """The indicative table folds each test to its median across rounds."""
    base1 = write_junit(tmp_path / "base-1.xml", {"m::x": 1.0})
    base2 = write_junit(tmp_path / "base-2.xml", {"m::x": 3.0})
    head1 = write_junit(tmp_path / "head-1.xml", {"m::x": 1.0})
    head2 = write_junit(tmp_path / "head-2.xml", {"m::x": 5.0})

    result = cd.compare([str(base1), str(base2)], [str(head1), str(head2)])

    assert result["per_test"] == [(1.0, pytest.approx(0.5), "m::x", 2.0, 3.0)]


def test_render_reports_one_row_per_pair_and_the_median():
    base = {"m::a": 1.0, "m::b": 1.0}
    pairs = [cd.pair_totals(base, {"m::a": 1.1, "m::b": 1.0}),
             cd.pair_totals(base, {"m::a": 1.2, "m::b": 1.0})]
    result = {
        "shared": 2,
        "base_only": [],
        "head_only": [],
        "pairs": pairs,
        "median_ratio": 1.075,
        "per_test": [],
    }

    text = cd.render(result, 0.30, "v1.2.3")

    assert "| round | baseline | this commit | ratio |" in text
    assert "| 1 | 2.00s | 2.10s | 1.050 |" in text
    assert "| 2 | 2.00s | 2.20s | 1.100 |" in text
    assert "median paired ratio" in text
    assert "v1.2.3" in text


def test_render_marks_a_regression_and_names_the_budget():
    base = {"m::a": 1.0, "m::b": 1.0}
    result = {
        "shared": 2,
        "base_only": [],
        "head_only": [],
        "pairs": [cd.pair_totals(base, {"m::a": 2.0, "m::b": 2.0})],
        "median_ratio": 2.0,
        "per_test": [],
    }

    text = cd.render(result, 0.30, "v1.2.3")

    assert "REGRESSION" in text
    assert "+100.0%" in text
    assert "v1.2.3" in text


def test_render_marks_an_acceptable_change():
    base = {"m::a": 1.0, "m::b": 1.0}
    result = {
        "shared": 2,
        "base_only": [],
        "head_only": [],
        "pairs": [cd.pair_totals(base, {"m::a": 1.10, "m::b": 1.0})],
        "median_ratio": 1.05,
        "per_test": [],
    }

    text = cd.render(result, 0.30, "v1.2.3")

    assert "within budget" in text
    assert "REGRESSION" not in text


def test_render_disclaims_the_per_test_table():
    """The table misleads without this, and that is measured, not assumed.

    Two runs of identical code on one machine moved individual tests by up
    to +370% while the total moved 1.7%. A reader who takes a row as a
    regression is reading noise.
    """
    base = {"m::a": 1.0, "m::b": 1.0}
    result = {
        "shared": 2,
        "base_only": [],
        "head_only": [],
        "pairs": [cd.pair_totals(base, {"m::a": 1.0, "m::b": 1.4})],
        "median_ratio": 1.2,
        "per_test": [(0.4, 0.4, "m::b", 1.0, 1.4)],
    }

    text = cd.render(result, 0.30, "v1.0.0")

    assert "not a regression" in text.lower()
    assert "<details>" in text and "</details>" in text


def test_render_survives_a_result_with_no_movers():
    base = {"m::a": 1.0}
    result = {
        "shared": 1,
        "base_only": [],
        "head_only": [],
        "pairs": [cd.pair_totals(base, base)],
        "median_ratio": 1.0,
        "per_test": [],
    }

    text = cd.render(result, 0.30, "v0.1.0")

    assert "| Test |" not in text
    assert "within budget" in text


def test_main_gates_on_the_median_not_the_worst_pair(tmp_path):
    """One noisy pair must not fail an otherwise clean branch — but a
    median over the budget still fails even when no single pair alone
    would trip the threshold used."""
    base1 = write_junit(tmp_path / "base-1.xml", {"m::a": 1.0})
    base2 = write_junit(tmp_path / "base-2.xml", {"m::a": 1.0})
    head1 = write_junit(tmp_path / "head-1.xml", {"m::a": 2.0})
    head2 = write_junit(tmp_path / "head-2.xml", {"m::a": 1.0})

    argv = sys.argv
    try:
        # Median 1.5 = +50%, under a 60% budget even though pair 1 doubled.
        sys.argv = ["compare_durations.py", "--base", str(base1), str(base2),
                    "--head", str(head1), str(head2),
                    "--max-regression", "0.60"]
        assert cd.main() == 0

        # Median 1.5 = +50%, over a 40% budget even though pair 2 is flat.
        sys.argv = ["compare_durations.py", "--base", str(base1), str(base2),
                    "--head", str(head1), str(head2),
                    "--max-regression", "0.40"]
        assert cd.main() == 1
    finally:
        sys.argv = argv


def test_main_exits_1_over_budget_and_0_under(tmp_path):
    base1 = write_junit(tmp_path / "base-1.xml", {"m::a": 1.0})
    base2 = write_junit(tmp_path / "base-2.xml", {"m::a": 1.0})
    slow1 = write_junit(tmp_path / "head-1.xml", {"m::a": 2.0})
    slow2 = write_junit(tmp_path / "head-2.xml", {"m::a": 2.0})
    fine1 = write_junit(tmp_path / "fine-1.xml", {"m::a": 1.05})
    fine2 = write_junit(tmp_path / "fine-2.xml", {"m::a": 1.05})
    summary = tmp_path / "summary.md"

    argv = sys.argv
    try:
        sys.argv = ["compare_durations.py", "--base", str(base1), str(base2),
                    "--head", str(slow1), str(slow2),
                    "--max-regression", "0.30",
                    "--summary-file", str(summary)]
        assert cd.main() == 1

        sys.argv = ["compare_durations.py", "--base", str(base1), str(base2),
                    "--head", str(fine1), str(fine2),
                    "--max-regression", "0.30"]
        assert cd.main() == 0
    finally:
        sys.argv = argv

    capsys_text = summary.read_text(encoding="utf-8")
    # The report reaches the summary file even on the failing run — a red
    # gate with no detail is one nobody can act on.
    assert "REGRESSION" in capsys_text


def test_main_exits_2_when_it_cannot_compare(tmp_path):
    base = write_junit(tmp_path / "base.xml", {"m::a": 1.0})
    other = write_junit(tmp_path / "other.xml", {"m::z": 1.0})

    argv = sys.argv
    try:
        sys.argv = ["compare_durations.py", "--base", str(base),
                    "--head", str(other)]
        # 2, not 1: "could not compare" is a different thing from "slower",
        # and a workflow that conflates them reports a restructure as a
        # performance regression.
        assert cd.main() == 2
    finally:
        sys.argv = argv
