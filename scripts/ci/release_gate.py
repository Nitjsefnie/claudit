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

  - its own workflow, by that workflow's own path: every row produced by
    THIS workflow file, at any attempt. An earlier `release` run on the
    same commit is this job's previous answer — it vouches for nothing
    the waiter is here to establish, and judging it makes a manual re-cut
    of a commit whose first release run failed refuse forever. The path
    comes from the RUN (`--self-path`), not from the runs listing, because
    a `workflow_dispatch` carrying `sha=` records its own run against the
    branch tip and is therefore absent from a listing filtered by head
    SHA — the common re-cut, since the 2700 s deadline fires after the tip
    has moved on. The listing remains the fallback when no path is passed,
    and an unknown path excludes nothing;
  - a run whose head SHA is a different commit;
  - a run whose event is outside the gate set, with the dispatch case
    closed by the workflow's own path.

A row whose producing run cannot be resolved at all is JUDGED, not
skipped: an unresolvable run is not evidence that the row is noise. The
one exception is this job's own, excluded by exact job name when its run
id is unreadable — a row the waiter cannot place must not make it wait on
itself, and an exact name is not a prefix. And absence of a gate is
caught on the other side of the filter, not here — the wait step still
requires ci-gate's own `aggregate` verdict (issue #247), which no amount
of dropping rows can manufacture.

  python3 scripts/ci/release_gate.py --sha <sha> --self-run-id <id> \\
      --self-path .github/workflows/release.yml \\
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
            gate_paths: set[str], self_path: str | None, sha: str,
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
    # Every row THIS WORKFLOW produced, at any attempt. The push-triggered
    # release run on this commit is a push run, so it lands in gate_paths
    # and would be judged: after it fails — the deadline, a transient
    # `gh release create`, a cancel — every manual re-cut of the same commit
    # would refuse on its own predecessor's answer. The self run is not
    # listed when its own path is unknown, and then nothing is dropped.
    if self_path is not None and run.get("path") == self_path:
        return False, "this workflow's own run"
    if run.get("head_sha") != sha:
        return False, f"run {run_id} answers for another commit"
    # A gate event, or a dispatch of a workflow that also gates this
    # commit — the latter closed by the runs on this SHA, never by a list
    # of workflow names.
    event = run.get("event")
    if event in GATE_EVENTS or (
            event == DISPATCH_EVENT
            and (not gate_paths or run.get("path") in gate_paths)):
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
           self_run_id: str | None, self_job: str,
           self_path: str | None = None) -> tuple[list[str], list[str]]:
    """Return (rows to judge, notes on every row dropped, both in order)."""
    runs_by_id = {str(run["id"]): run
                  for run in workflow_runs if run.get("id") is not None}
    gate_paths = gate_workflow_paths(workflow_runs, sha)
    # This run's own workflow file. The caller reads it off the run itself
    # (--self-path), which is the only source that survives a dispatch: a
    # `workflow_dispatch` carrying `sha=` records its run against the
    # BRANCH TIP, so a re-cut of a non-tip commit is not in a listing
    # filtered by head SHA. The listing is the fallback for a caller that
    # passes no path — absent there means unknown, and an unknown path
    # excludes nothing.
    if self_path is None and self_run_id is not None:
        self_run = runs_by_id.get(str(self_run_id))
        self_path = self_run.get("path") if self_run else None
    rows: list[str] = []
    notes: list[str] = []
    for check_run in check_runs:
        judged, why = _judged(check_run, runs_by_id, gate_paths, self_path,
                              sha, self_run_id, self_job)
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
    parser.add_argument("--self-path", default="",
                        help="this workflow file's repo-relative path, when "
                             "known; falls back to the runs listing")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    checks = parse_pages(_read(args.checks), "check_runs")
    runs = parse_pages(_read(args.runs), "workflow_runs")
    rows, notes = select(checks, runs, args.sha,
                         args.self_run_id or None, args.self_job,
                         args.self_path or None)
    for note in notes:
        print(note, file=sys.stderr)
    for line in rows:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
