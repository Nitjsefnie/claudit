#!/usr/bin/env python3
"""Select the check runs a release waits on, and say why each was kept.

The release job blocks on the gates that vouch for the commit it is about
to tag. "The gates that vouch for the commit" is the whole content of this
module: a check run sitting on a SHA is NOT by itself evidence that CI
ran for that SHA, and two shapes of noise make it a bad one.

  - A NAME is not identity. The waiter used to drop every check whose name
    started with `release`, meaning to drop its own job and in fact
    dropping any gate that ever grows that prefix (issue #557).
  - A SHA is not a claim either. An `issue_comment` workflow runs against
    the DEFAULT BRANCH TIP — GitHub sets that run's head SHA to master's
    last commit — so a `/claim` anywhere in the repository publishes a
    `claim` check run on master's head, and a red one refused a release
    cut from that very commit (issue #579).

So identity comes from the workflow run that PRODUCED each check run: its
`head_sha` and its triggering `event`. A row is judged when the run that
produced it was triggered for this commit by an event that creates CI for
a commit's sake — a push or a pull request — plus a manual dispatch of a
workflow that also gates this commit. Every other trigger (`issue_comment`,
`schedule`, `workflow_run`, and a dispatch of a workflow that never gates)
answers a question no commit asked, and is skipped.

THE FAIL-CLOSED SIDE. This filter may only ever REMOVE a row the module
can positively identify as not-a-gate:

  - its own job, by exact job name AND run identity — never a name
    prefix;
  - a run whose head SHA is a different commit;
  - a run whose event is outside the gate set, with the dispatch case
    closed by the workflow's own path.

A row whose producing run cannot be resolved at all is JUDGED, not
skipped: an unresolvable run is not evidence that the row is noise. And
absence of a gate is caught on the other side of the filter, not here —
the wait step still requires ci-gate's own `aggregate` verdict (issue
#247), which no amount of dropping rows can manufacture.

  python3 scripts/ci/release_gate.py --sha <sha> --self-run-id <id> \\
      --checks checks.json --runs runs.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# The events that create CI FOR A COMMIT, and so vouch for one.
GATE_EVENTS = frozenset({"push", "pull_request"})

# A dispatch is a gate of the commit when the same workflow also gates it —
# derived from the runs on this SHA, never from a list of workflow names,
# so a workflow added later is classified without editing this file.
DISPATCH_EVENT = "workflow_dispatch"

# A check run's html_url is .../actions/runs/<run id>/job/<job id>, which
# is the only join from a check run to the run that produced it.
RUN_ID_RE = re.compile(r"/actions/runs/(\d+)(?:/|$)")


def parse_pages(raw: str, key: str) -> list[dict]:
    """Merge the top-level objects `gh api --paginate` concatenated.

    Pagination makes `gh` print one JSON document per page, back to back,
    so a single response is not a single `json.loads`. Anything that is
    not a JSON object, and any object without `key`, contributes nothing.
    """
    decoder = json.JSONDecoder()
    merged: list[dict] = []
    index, length = 0, len(raw)
    while index < length:
        while index < length and raw[index] in " \t\r\n":
            index += 1
        if index >= length:
            break
        document, index = decoder.raw_decode(raw, index)
        if isinstance(document, dict):
            merged.extend(document.get(key) or [])
    return merged


def run_id_of(check_run: dict) -> str | None:
    """The producing workflow run's id, or None when it cannot be read."""
    match = RUN_ID_RE.search(str(check_run.get("html_url") or ""))
    return match.group(1) if match else None


def gate_workflow_paths(workflow_runs: list[dict], sha: str) -> set[str]:
    """Workflows that gate this commit, read off the runs on this commit.

    Empty means nothing has been scheduled yet — the caller treats that as
    "cannot tell a gate dispatch from a stray one" and judges both.
    """
    return {run["path"] for run in workflow_runs
            if run.get("head_sha") == sha
            and run.get("event") in GATE_EVENTS
            and run.get("path")}


def _judged(check_run: dict, runs_by_id: dict[str, dict],
            gate_paths: set[str], sha: str,
            self_run_id: str | None, self_job: str) -> tuple[bool, str]:
    """Whether this check run is a gate of the commit, and why not if not."""
    run_id = run_id_of(check_run)
    # Its own job, by exact name AND run identity. An unreadable run id
    # also qualifies: the name is exact, never a prefix, so no other job in
    # this repository answers to it.
    if check_run.get("name") == self_job and run_id in (None, self_run_id):
        return False, "this job"
    run = runs_by_id.get(run_id) if run_id is not None else None
    if run is None:
        return True, ""
    if run.get("head_sha") != sha:
        return False, f"run {run_id} answers for another commit"
    event = run.get("event")
    if event in GATE_EVENTS:
        return True, ""
    if event == DISPATCH_EVENT and (
            not gate_paths or run.get("path") in gate_paths):
        return True, ""
    return False, f"run {run_id} event {event} ({run.get('path')})"


def row(check_run: dict) -> str:
    """One projected check-run row: status, conclusion, name, app slug."""
    app = check_run.get("app") or {}
    return "\t".join([
        str(check_run.get("status") or ""),
        str(check_run.get("conclusion") or "-"),
        str(check_run.get("name") or ""),
        str(app.get("slug") or "-"),
    ])


def select(check_runs: list[dict], workflow_runs: list[dict], sha: str,
           self_run_id: str | None, self_job: str) -> tuple[list[str], list[str]]:
    """Return (rows to judge, notes on every row dropped, both in order)."""
    runs_by_id = {str(run["id"]): run
                  for run in workflow_runs if run.get("id") is not None}
    gate_paths = gate_workflow_paths(workflow_runs, sha)
    rows: list[str] = []
    notes: list[str] = []
    for check_run in check_runs:
        judged, why = _judged(check_run, runs_by_id, gate_paths, sha,
                              self_run_id, self_job)
        if judged:
            rows.append(row(check_run))
        else:
            notes.append(f"skipping check {check_run.get('name')!r}: {why}")
    return rows, notes


def _read(source: str) -> str:
    if source == "-":
        return sys.stdin.read()
    return Path(source).read_text(encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha", required=True,
                        help="the commit being released")
    parser.add_argument("--checks", default="-",
                        help="check-runs JSON, or - for stdin")
    parser.add_argument("--runs", default="-",
                        help="workflow-runs JSON, or - for stdin")
    parser.add_argument("--self-job", default="release",
                        help="this workflow's job name")
    parser.add_argument("--self-run-id", default="",
                        help="this workflow run's id, when known")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    checks = parse_pages(_read(args.checks), "check_runs")
    runs = parse_pages(_read(args.runs), "workflow_runs")
    rows, notes = select(checks, runs, args.sha,
                         args.self_run_id or None, args.self_job)
    for note in notes:
        print(note, file=sys.stderr)
    for line in rows:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
