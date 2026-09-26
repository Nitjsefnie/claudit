"""Push-event classification: the verified base (issue #208).

ci-gate's classifier narrows a documentation-only push by reading the
push's changed paths, and on master it reads them since the newest
commit whose gate legs actually executed — not since the push's own
`before`, which a cancelled predecessor run left unverified. The
scenarios here arrange the runs list, the candidate runs' jobs and the
compare read through a stubbed `run` callable keyed on argv, and pin
the walk's contract: a completed, non-cancelled run at least one leg
of which executed is the base; anything unreadable over-runs to the
full gate set, never fewer legs.

The classifier's pattern set and its pull-request reads live in
test_ci_gate_modules.py, beside the aggregate fold.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    """Import scripts/ci/<name>.py by path.

    scripts/ci is not a package and deliberately has no __init__.py — it
    holds standalone CI entry points, not an importable library.
    """
    path = REPO_ROOT / "scripts" / "ci" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


classify = _load("classify_changes")

# The runs-list URL segment the classifier reads on a push, and the SHAs
# the scenarios below arrange newest-first in it: BASE_SHA is the last
# commit whose gate legs executed, INTERVENING_SHA is a cancelled run's
# commit (the push's `before`), and PUSH_SHA is the push itself.
WORKFLOW_RUNS_URL = "actions/workflows/ci-gate.yml/runs"
BASE_SHA = "a" * 40
INTERVENING_SHA = "e" * 40
PUSH_SHA = "b" * 40

GREEN_JOBS = "classify success\naggregate success\ntests success\n"
DOCS_ONLY_JOBS = ("classify success\naggregate success\n"
                  "tests skipped\nlint skipped\n")


def _url(argv):
    """The single repos/… URL of a stubbed gh api call."""
    return next(arg for arg in argv if arg.startswith("repos/"))


def _runs_page(*lines):
    """A runs-list jq payload: `{sha} {status} {conclusion} {id}` lines."""
    return "\n".join(lines) + "\n"


def test_jobs_read_sees_every_job_of_a_run():
    # Issue #210. The legs-executed decision read a run's jobs with no
    # `per_page` and no pagination, so it saw only the API's default
    # first page (30 jobs); on a longer run an all-skipped later page
    # would hide a leg that executed and misread that run as a verified
    # docs-only base. The read must be complete (`--paginate`, the same
    # shape the pull-request files read pins) and batched at the API's
    # per-page maximum (`per_page=100`) so completeness costs fewer
    # requests.
    calls = []

    def run(argv):
        calls.append(argv)
        url = _url(argv)
        if WORKFLOW_RUNS_URL in url:
            return _runs_page(f"{INTERVENING_SHA} completed success 90")
        if "/jobs" in url:
            return GREEN_JOBS
        return ""  # the compare comes back empty and over-runs

    assert classify.changed_paths(
        {"name": "push", "repository": "o/r",
         "before": INTERVENING_SHA, "sha": PUSH_SHA},
        run,
    ) is None
    argv = next(call for call in calls if "/jobs" in _url(call))
    assert "--paginate" in argv  # every job of the run, not a first page
    assert _url(argv) == "repos/o/r/actions/runs/90/jobs?per_page=100"
    assert argv[argv.index("-H") + 1] == "Cache-Control: no-cache"


def test_push_classifies_since_the_last_run_whose_legs_executed():
    # Issue #208. A code push had its ci-gate run cancelled by the next
    # push; the following docs-only push then classified only its own
    # before...sha, so every leg skipped and the aggregate reported
    # green over code no leg had run on. The classifier must instead
    # classify since the newest master run whose legs actually
    # executed, so the intervening code is inside the classified range.
    calls = []

    def run(argv):
        calls.append(argv)
        url = _url(argv)
        if WORKFLOW_RUNS_URL in url:
            return _runs_page(
                f"{PUSH_SHA} in_progress - 31",
                f"{INTERVENING_SHA} completed cancelled 30",
                f"{BASE_SHA} completed success 29",
            )
        if "/jobs" in url:
            return GREEN_JOBS if url.split("/")[-2] == "29" else ""
        return "AGENTS.md\n.github/workflows/tests.yml\n"

    docs_only, reason = classify.classify(
        {"name": "push", "repository": "o/r",
         "before": INTERVENING_SHA, "sha": PUSH_SHA},
        run,
    )
    assert docs_only is False
    assert "outside documentation" in reason
    assert f"repos/o/r/compare/{BASE_SHA}...{PUSH_SHA}" in calls[-1]


def test_push_walks_past_a_docs_only_run_whose_legs_all_skipped():
    # A completed, non-cancelled run whose every leg was skipped by the
    # docs-only narrowing verified nothing: it is not a base, and the
    # walk continues to the older run whose legs executed.
    calls = []

    def run(argv):
        calls.append(argv)
        url = _url(argv)
        if WORKFLOW_RUNS_URL in url:
            return _runs_page(
                f"{PUSH_SHA} in_progress - 33",
                f"{INTERVENING_SHA} completed success 32",
                f"{BASE_SHA} completed success 29",
            )
        if "/jobs" in url:
            return (DOCS_ONLY_JOBS if url.split("/")[-2] == "32"
                    else GREEN_JOBS)
        return "docs/guide.md\n"

    docs_only, _ = classify.classify(
        {"name": "push", "repository": "o/r",
         "before": INTERVENING_SHA, "sha": PUSH_SHA},
        run,
    )
    assert docs_only is True
    compares = [argv for argv in calls if "/compare/" in _url(argv)]
    assert compares == [
        ["gh", "api", "-H", "Cache-Control: no-cache",
         f"repos/o/r/compare/{BASE_SHA}...{PUSH_SHA}", "--jq",
         ".files[].filename"],
    ]


def test_push_skips_runs_that_are_not_completed_without_a_jobs_read():
    # This push's own run is still queued and an older one is in
    # progress: neither is completed, so neither is a base candidate
    # and neither costs a jobs read.
    jobs_calls = []

    def run(argv):
        url = _url(argv)
        if WORKFLOW_RUNS_URL in url:
            return _runs_page(
                f"{PUSH_SHA} queued - 35",
                f"{INTERVENING_SHA} in_progress - 34",
                f"{BASE_SHA} completed success 33",
            )
        if "/jobs" in url:
            jobs_calls.append(url)
            assert url.split("/")[-2] == "33"
            return GREEN_JOBS
        return "NOTICE\n"

    docs_only, _ = classify.classify(
        {"name": "push", "repository": "o/r",
         "before": INTERVENING_SHA, "sha": PUSH_SHA},
        run,
    )
    assert docs_only is True
    assert len(jobs_calls) == 1


def test_push_walks_past_a_run_whose_jobs_name_no_leg():
    # A completed, non-cancelled run whose jobs list holds only the
    # classifier and the aggregate verified nothing either — the walk
    # continues, and with nothing older it over-runs.
    def run(argv):
        url = _url(argv)
        if WORKFLOW_RUNS_URL in url:
            return _runs_page(
                f"{INTERVENING_SHA} completed success 50",
                f"{BASE_SHA} completed success 49",
            )
        if "/jobs" in url:
            return "classify success\naggregate success\n"
        raise AssertionError("no compare should be reached")

    assert classify.changed_paths(
        {"name": "push", "repository": "o/r",
         "before": INTERVENING_SHA, "sha": PUSH_SHA},
        run,
    ) is None


def test_push_with_no_verified_base_over_runs():
    # Every older run was cancelled: nothing on master has been
    # verified, so the full gate set runs. The same holds for a push
    # with no runs at all.
    events = {"name": "push", "repository": "o/r",
              "before": INTERVENING_SHA, "sha": PUSH_SHA}

    def cancelled_only(argv):
        assert WORKFLOW_RUNS_URL in _url(argv)
        return _runs_page(
            f"{PUSH_SHA} in_progress - 41",
            f"{INTERVENING_SHA} completed cancelled 40",
            f"{BASE_SHA} completed cancelled 39",
        )

    assert classify.changed_paths(events, cancelled_only) is None
    docs_only, reason = classify.classify(events, cancelled_only)
    assert docs_only is False
    assert "full gate set" in reason

    def no_runs(argv):
        assert WORKFLOW_RUNS_URL in _url(argv)
        return ""

    assert classify.changed_paths(events, no_runs) is None


def test_push_api_failure_anywhere_over_runs():
    # The runs list, a candidate's jobs read and the compare read each
    # over-run on failure: the full gate set, never fewer legs.
    events = {"name": "push", "repository": "o/r",
              "before": INTERVENING_SHA, "sha": PUSH_SHA}

    def failing_runs(argv):
        raise OSError("gh failed")

    assert classify.changed_paths(events, failing_runs) is None

    def failing_jobs(argv):
        if WORKFLOW_RUNS_URL in _url(argv):
            return _runs_page(f"{INTERVENING_SHA} completed success 60")
        raise OSError("gh failed")

    assert classify.changed_paths(events, failing_jobs) is None

    def failing_compare(argv):
        url = _url(argv)
        if WORKFLOW_RUNS_URL in url:
            return _runs_page(f"{INTERVENING_SHA} completed success 61")
        if "/jobs" in url:
            return GREEN_JOBS
        raise OSError("gh failed")

    assert classify.changed_paths(events, failing_compare) is None


def test_push_whose_base_equals_its_own_sha_over_runs():
    # The newest legs-bearing run sits at this push's own SHA (a
    # dispatch re-run, say): the compare comes back empty and over-runs
    # — the same reading as every other unreadable changed set.
    def run(argv):
        url = _url(argv)
        if WORKFLOW_RUNS_URL in url:
            return _runs_page(f"{PUSH_SHA} completed success 70")
        if "/jobs" in url:
            return GREEN_JOBS
        return ""

    assert classify.changed_paths(
        {"name": "push", "repository": "o/r",
         "before": INTERVENING_SHA, "sha": PUSH_SHA},
        run,
    ) is None


def test_push_with_an_unreadable_runs_shape_over_runs():
    # A line the jq shape cannot produce means the API's shape moved:
    # nothing is trusted, and the full gate set runs.
    def run(argv):
        if WORKFLOW_RUNS_URL in _url(argv):
            return "not-a-sha completed success 80\n"
        raise AssertionError("no further read should happen")

    assert classify.changed_paths(
        {"name": "push", "repository": "o/r",
         "before": INTERVENING_SHA, "sha": PUSH_SHA},
        run,
    ) is None
