#!/usr/bin/env python3
"""Compare per-test durations between two builds of the suite, in pairs.

Answers one question: did the tests that exist in both builds get slower?

WHY NOT TOTAL WALL TIME. The obvious metric — how long did the suite take
— punishes the wrong thing. Add twenty tests and the total climbs, the
gate goes red, and nothing regressed. Delete a slow test and the total
drops, hiding a genuine regression somewhere else. So a pair intersects
on test node id and compares only the tests passing, and present, in both
reports of that pair. Adding or removing tests cannot move the number.

WHY PAIRED ROUNDS. A runner's first round pays a cold page cache and a
cold interpreter, so first rounds are systematically the slowest. Taking
the minimum across rounds discards exactly the round that shows that
bias. Interleaving the two sides does not remove the bias: the warm-up
cost lands in whichever pair runs first no matter how the sides
alternate. So the first round is not folded in at all — it runs and is
discarded, and the remaining rounds are read in pairs, round i of the
baseline against round i of this commit. A pair's ratio is its head total
over its base total, each summed over the tests passing in both reports
of that pair, and the verdict is the median of the paired ratios —
robust to one noisy pair, and every total reported is one a complete run
achieved.

WHY THIS IS COMPARABLE AT ALL. Both builds run back to back on the SAME
runner, in the same job. That is what makes a percentage meaningful here;
comparing a duration from one runner against a duration recorded on
another would measure the runners.

Input is pytest's own --junitxml, which carries an exact per-testcase
time and needs no plugin.

    compare_durations.py --base base1.xml base2.xml \\
                         --head head1.xml head2.xml \\
                         --max-regression 0.30
"""
from __future__ import annotations

import argparse
import statistics
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# A testcase carrying any of these children did not pass, and its duration
# is not comparable: a failure can be fast (an early assert) or slow (a
# timeout), and either way it is measuring the wrong thing.
NOT_PASSED = ("failure", "error", "skipped")


class ComparisonError(RuntimeError):
    """The comparison could not be made at all."""


def node_id(case: ET.Element) -> str:
    """`tests.test_a::test_one`, stable across runs and machines."""
    classname = case.get("classname", "")
    name = case.get("name", "")
    return f"{classname}::{name}" if classname else name


def parse_junit(path: Path) -> dict:
    """{node_id: seconds} for the passing testcases in one report."""
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise ComparisonError(f"{path} is not parseable JUnit XML: {exc}") from exc

    times = {}
    for case in root.iter("testcase"):
        if any(case.find(tag) is not None for tag in NOT_PASSED):
            continue
        raw = case.get("time")
        if raw is None:
            continue
        try:
            times[node_id(case)] = float(raw)
        except ValueError:
            continue
    if not times:
        raise ComparisonError(f"{path} contains no passing testcases")
    return times


def pair_totals(base: dict, head: dict) -> dict:
    """One pair's totals and ratio over the tests passing in both reports.

    Intersecting on node id is what stops an added or removed test from
    moving the number: a duration counted on one side of a ratio and not
    the other is not a slowdown, it is a population change.
    """
    shared = set(base) & set(head)
    if not shared:
        raise ComparisonError(
            "the two reports of this pair share no passing test — the "
            "suite was renamed or restructured wholesale, so there is "
            "nothing to compare"
        )
    base_total = sum(base[n] for n in shared)
    head_total = sum(head[n] for n in shared)
    if base_total <= 0:
        raise ComparisonError(
            "baseline total is zero — the reports carry no usable timings"
        )
    return {
        "shared": len(shared),
        "base_total": base_total,
        "head_total": head_total,
        "ratio": head_total / base_total,
    }


def compare(base_paths: list, head_paths: list) -> dict:
    """Pair the rounds positionally and fold each side to per-test medians.

    Round i of --base is compared against round i of --head, which is the
    order the workflow's run step emits them in. The counts and the
    per-test table describe the population that passed in every round on
    both sides; the verdict uses each pair's own intersection.
    """
    if not base_paths or not head_paths:
        raise ComparisonError("no JUnit reports given")
    if len(base_paths) != len(head_paths):
        raise ComparisonError(
            "--base and --head must carry the same number of reports — "
            f"one per pair — got {len(base_paths)} and {len(head_paths)}"
        )

    base_rounds = [parse_junit(Path(p)) for p in base_paths]
    head_rounds = [parse_junit(Path(p)) for p in head_paths]

    pairs = [pair_totals(b, h) for b, h in zip(base_rounds, head_rounds)]

    # "Removed" and "new" mean present-in-every-round of one side and not
    # the other — a test that flaked out of one round on BOTH sides is
    # neither, and appears in neither count.
    base_stable = set(base_rounds[0])
    for other in base_rounds[1:]:
        base_stable &= set(other)
    head_stable = set(head_rounds[0])
    for other in head_rounds[1:]:
        head_stable &= set(other)
    common = base_stable & head_stable

    per_test = []
    for nid in common:
        was = statistics.median([r[nid] for r in base_rounds])
        now = statistics.median([r[nid] for r in head_rounds])
        # Tests in the millisecond range are dominated by fixture and
        # collection overhead; a 300% "regression" on a 2 ms test is
        # noise and would bury the real entries in the table. They still
        # count in the pair totals, which is where they belong.
        if was >= 0.05:
            per_test.append((now - was, (now / was) - 1.0, nid, was, now))
    per_test.sort(reverse=True)

    return {
        "shared": len(common),
        "base_only": sorted(base_stable - head_stable),
        "head_only": sorted(head_stable - base_stable),
        "pairs": pairs,
        "median_ratio": statistics.median([p["ratio"] for p in pairs]),
        "per_test": per_test,
    }


def render(result: dict, threshold: float, base_label: str) -> str:
    delta = result["median_ratio"] - 1.0
    verdict = "🔴 REGRESSION" if delta > threshold else "🟢 within budget"
    lines = [
        "### Test speed vs "
        f"`{base_label}`",
        "",
        f"**{verdict}** — median paired ratio {result['median_ratio']:.3f} "
        f"({delta:+.1%}, budget {threshold:+.0%})",
        "",
        f"- Compared on **{result['shared']}** tests passing in every round "
        "on both builds",
        "- Each row is one interleaved pair of rounds, so every total below "
        "is a run that happened:",
        "",
        "| round | baseline | this commit | ratio |",
        "| --- | ---: | ---: | ---: |",
    ]
    for number, pair in enumerate(result["pairs"], 1):
        lines.append(
            f"| {number} | {pair['base_total']:.2f}s | "
            f"{pair['head_total']:.2f}s | {pair['ratio']:.3f} |"
        )
    spread = [p["ratio"] for p in result["pairs"]]
    lines += [
        "",
        f"- median paired ratio: {result['median_ratio']:.3f}",
    ]
    if len(spread) > 1:
        lines.append(
            f"- spread across pairs: {min(spread):.3f} to {max(spread):.3f}"
        )
    if result["head_only"]:
        lines.append(
            f"- {len(result['head_only'])} test(s) new since `{base_label}`, "
            "excluded from the comparison"
        )
    if result["base_only"]:
        lines.append(
            f"- {len(result['base_only'])} test(s) removed since "
            f"`{base_label}`, excluded from the comparison"
        )

    movers = [row for row in result["per_test"] if abs(row[1]) >= 0.10][:15]
    if movers:
        lines += [
            "",
            "<details><summary>Largest per-test movements "
            "(median across rounds, individually noisy — read the median "
            "paired ratio, not these)</summary>",
            "",
            "| Test | Was | Now | Change |",
            "| --- | ---: | ---: | ---: |",
        ]
        for _abs_delta, rel, nid, was, now in movers:
            lines.append(
                f"| `{nid}` | {was:.3f}s | {now:.3f}s | {rel:+.0%} |"
            )
        lines += [
            "",
            "_**A row here is not a regression.** Two runs of identical "
            "code on one machine routinely differ by 100% or more on a "
            "single test, while the total moves by under 2% — that is why "
            "the gate is the median paired ratio and not any individual "
            "row. These are ordered by absolute seconds gained and are "
            "useful only as a starting point once the MEDIAN has already "
            "gone red._",
            "",
            "_Tests under 50 ms are omitted — at that scale the number is "
            "fixture overhead, not the test. They still count in the pair "
            "totals above._",
            "",
            "</details>",
        ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--base", nargs="+", required=True,
                        metavar="XML",
                        help="baseline JUnit report(s), in pair order")
    parser.add_argument("--head", nargs="+", required=True,
                        metavar="XML",
                        help="this commit's JUnit report(s), in pair order")
    parser.add_argument("--max-regression", type=float, default=0.30,
                        help="fail above this fractional slowdown "
                             "(default: 0.30 = 30%%)")
    parser.add_argument("--base-label", default="baseline",
                        help="name of the baseline, for the report")
    parser.add_argument("--summary-file", default=None,
                        help="append the markdown report here as well as "
                             "printing it (e.g. $GITHUB_STEP_SUMMARY)")
    args = parser.parse_args()

    try:
        result = compare(args.base, args.head)
    except ComparisonError as exc:
        print(f"cannot compare: {exc}", file=sys.stderr)
        return 2

    report = render(result, args.max_regression, args.base_label)
    print(report)
    if args.summary_file:
        with open(args.summary_file, "a", encoding="utf-8") as handle:
            handle.write(report)

    delta = result["median_ratio"] - 1.0
    if delta > args.max_regression:
        print(
            f"FAIL: the median paired ratio is {delta:+.1%} vs "
            f"{args.base_label}, over the {args.max_regression:+.0%} budget.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
