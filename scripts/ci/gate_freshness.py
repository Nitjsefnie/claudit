#!/usr/bin/env python3
"""Publish a `gate freshness` check run on every open pull-request head.

THE CHECK. Red iff master holds a commit the head lacks whose changed
paths intersect the gate trigger set (scripts/ci/gate_trigger_set.py:
the workflows, local actions and CI scripts; the ratchet data, lint,
type and scanner configs; the requirements files). Green when nothing
the head lacks is gate-defining. The check is advisory until a ruleset
requires it — that is an operator question (the require-ci-aggregate
ruleset); the deliverable is the publisher.

THE INCIDENT SHAPE. A head tested before a master gate-definer commit
(especially a ratchet tighten) holds a stale green; merging on it
lands master red (issue #495). This check makes the staleness visible
and blockable: each master push re-publishes every open head, so a
tighten flips the heads it strands red within a workflow run.

TRIGGERS. The workflow runs on every master push (all-heads mode) and
on pull_request_target events (single-PR mode), filtered to the base
branch it protects. A `.github/ci-thresholds.json`-only tighten commit
DOES trigger this workflow — reacting to tightens is the workflow's
purpose — although every gate suite's own trigger ignores that file:
this is a one-minute publisher that runs no suite.

TRUST MODEL. pull_request_target runs privileged, so the workflow
checks out the base side only and this script never executes anything
from a pull-request tree: the head SHA arrives via GF_HEAD_SHA, is
fetched as git objects only, and a hostile SHA simply fails the
compare — which publishes red (fail closed).

FAIL SHAPES. An unreadable per-head compare publishes RED. A global
read failure (the master ref, the open-PR list, the trigger set)
publishes nothing and exits nonzero. A failed check-run publish is
retried once and then exits nonzero, so no head keeps a silently
stale green behind an apparently green run. `--dry-run` prints the
verdicts and writes nothing.

THE BOUND. One master fetch and one open-PR listing per run, one
grouped head-object fetch per remote, one git log per head, one
check-run POST per head, retried once. Reads fresh with no-cache
headers and pagination.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

BASE_BRANCH = "master"
CHECK_NAME = "gate freshness"
WORKFLOW_ID = "gate-freshness.yml"
ENV_PR = "GF_PR"
ENV_HEAD_SHA = "GF_HEAD_SHA"
_HEX40 = frozenset("0123456789abcdef")


class QueryError(RuntimeError):
    """A git or `gh api` call that could not be read."""


def _hex40(value):
    return (isinstance(value, str) and len(value) == 40
            and all(char in _HEX40 for char in value))


# --- the compare ------------------------------------------------------------

def missing_commit_files(run_git, master_tip, head_sha):
    """(sha, changed paths) for every master commit the head lacks.

    One `git log` over `master_tip ^head_sha`; `-m` shows merge
    commits against their first parent, so a merge on master cannot
    hide its files behind an empty combined diff. Raises QueryError
    when the head's objects are absent or git fails: the caller
    publishes red on that head (fail closed).
    """
    try:
        out = run_git(["git", "log", "-m", "--name-only", "--format=%H",
                       master_tip, f"^{head_sha}"])
    except Exception as exc:
        raise QueryError(f"the compare for {head_sha[:12]} failed: {exc}"
                         ) from exc
    commits: list[tuple[str, list[str]]] = []
    block: list[str] = []
    for line in out.splitlines():
        if line.strip():
            block.append(line)
        elif block:
            commits.append((block[0], block[1:]))
            block = []
    if block:
        commits.append((block[0], block[1:]))
    return commits


def first_trigger_hit(commits, is_triggered):
    """(sha, path) of the first commit whose files hit the set, or None."""
    for sha, files in commits:
        for path in files:
            if is_triggered(path):
                return sha, path
    return None


def head_verdict(run_git, master_tip, head_sha, is_triggered):
    """(conclusion, title, summary) for one head; fail-closed on error."""
    try:
        commits = missing_commit_files(run_git, master_tip, head_sha)
    except QueryError as error:
        return ("failure",
                f"Unreadable compare against master {master_tip[:12]}",
                f"The compare could not be read ({error}); the check is "
                "red until it can. Re-run or rebase to retry it.")
    hit = first_trigger_hit(commits, is_triggered)
    if hit:
        sha, path = hit
        return ("failure",
                f"Stale against master {master_tip[:12]}: {sha[:12]} "
                f"touches {path}",
                f"Master holds {sha[:12]}, which the head lacks, and it "
                f"changes {path} — a gate definer. Rebase onto master "
                "and rerun; the previous verdict was computed before "
                "the rules moved (issue #495).")
    return ("success",
            f"Fresh against master {master_tip[:12]}",
            "Nothing this head lacks from master is gate-defining.")


def resolve_master_tip(run_git):
    """The fetched master tip, never the event's base.sha (it trails)."""
    run_git(["git", "fetch", "origin", BASE_BRANCH])
    out = run_git(["git", "rev-parse", "FETCH_HEAD"])
    tip = out.strip()
    if not _hex40(tip):
        raise QueryError(f"unreadable master tip: {tip!r}")
    return tip


def ensure_head_objects(run_git, heads, repository):
    """Fetch each head's objects, grouped by remote; leave failures.

    A same-repo head comes from `origin`; a fork head from its own
    URL (public, anonymous). A fetch that fails is left alone: the
    head's compare then fails closed and publishes red naming the
    cause.
    """
    missing = []
    for head in heads:
        sha = head["sha"]
        try:
            run_git(["git", "cat-file", "-e", f"{sha}^{{commit}}"])
        except Exception:
            missing.append(head)
    by_remote: dict[str, list[str]] = {}
    for head in missing:
        fork = head.get("repo") or ""
        remote = ("origin" if not fork or fork == repository
                  else f"https://github.com/{fork}")
        by_remote.setdefault(remote, []).append(head["sha"])
    for remote, shas in by_remote.items():
        try:
            run_git(["git", "fetch", remote, *shas])
        except Exception as exc:
            print(f"gate freshness: fetching {remote} heads failed; "
                  f"they will compare unreadable: {exc}", file=sys.stderr)


# --- gh reads ---------------------------------------------------------------

def _read(argv):
    """One `gh api` call: stdout, or a QueryError naming the failure."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=60, check=False)
    except (subprocess.SubprocessError, OSError) as exc:
        raise QueryError(f"gh failed: {exc}") from exc
    if proc.returncode != 0:
        raise QueryError(
            proc.stderr.strip()[:400] or f"gh exit {proc.returncode}")
    return proc.stdout


def _decode(stdout, what):
    try:
        return json.loads(stdout)
    except ValueError as exc:
        raise QueryError(f"unparseable {what}: {exc}") from exc


def _api(url, *extra):
    """The one call shape, so the no-cache convention has one site."""
    return ["gh", "api", "-H", "Cache-Control: no-cache", url, *extra]


def _open_pulls(repository):
    """Every open pull request against BASE_BRANCH, complete."""
    stdout = _read(_api(
        f"repos/{repository}/pulls?state=open&base={quote(BASE_BRANCH)}"
        f"&per_page=100", "--paginate"))
    payload = _decode(stdout, "the open pull-request list")
    if not isinstance(payload, list):
        raise QueryError("the open pull-request list did not decode as a "
                         "list")
    return payload


# --- publishing -------------------------------------------------------------

def publish_check(run_gh, repository, sha, conclusion, title, summary):
    """One check run, completed, on the head SHA."""
    run_gh(["gh", "api", "--method", "POST",
            f"repos/{repository}/check-runs",
            "-f", f"name={CHECK_NAME}",
            "-f", f"head_sha={sha}",
            "-F", "status=completed",
            "-F", f"conclusion={conclusion}",
            "-f", f"output[title]={title}",
            "-f", f"output[summary]={summary}"])


def publish_with_retry(run_gh, repository, sha, conclusion, title, summary):
    """publish_check, retried once; the second failure propagates."""
    last: QueryError | None = None
    for _attempt in (1, 2):
        try:
            publish_check(run_gh, repository, sha, conclusion, title,
                          summary)
            return
        except QueryError as error:
            last = error
    raise last  # type: ignore[misc]


# --- selection --------------------------------------------------------------

def scannable(head):
    """Whether this run judges the head: on the base branch, usable SHA."""
    return (head.get("base") == BASE_BRANCH and _hex40(head.get("sha")))


def heads_for_run(env, pulls):
    """The heads this run judges.

    Single-PR mode when both GF_PR and GF_HEAD_SHA are set (the
    pull_request_target event's own pull request); otherwise all
    scannable heads of the open-PR list.
    """
    number, sha = env.get(ENV_PR), env.get(ENV_HEAD_SHA)
    if number and sha:
        return [{"number": int(number), "sha": sha, "base": BASE_BRANCH,
                 "repo": ""}]
    heads = []
    for pr in pulls or []:
        if not isinstance(pr, dict):
            continue
        head, base = pr.get("head"), pr.get("base")
        heads.append({
            "number": pr.get("number"),
            "sha": head.get("sha") if isinstance(head, dict) else None,
            "base": base.get("ref") if isinstance(base, dict) else None,
            "repo": (head.get("repo", {}).get("full_name") or ""
                     if isinstance(head, dict) else ""),
        })
    return [head for head in heads if scannable(head)]


# --- the summary ------------------------------------------------------------

def write_summary(path, stale, scanned):
    """The step summary naming the heads whose check went red and why."""
    if not path:
        return
    lines = [
        "### Gate freshness",
        "",
        f"{len(stale)} of {scanned} open pull-request head(s) on "
        f"{BASE_BRANCH} hold a gate-defining commit they lack:",
        "",
    ]
    if stale:
        for entry in stale:
            lines.append(f"- PR #{entry['number']} "
                         f"(`{str(entry['sha'])[:12]}`): {entry['reason']}")
    else:
        lines.append("None — every open head is fresh against the "
                     "current master.")
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


# --- the run ----------------------------------------------------------------

def _load_trigger_set():
    """The sibling trigger-set module, loaded the scripts/ci way."""
    path = Path(__file__).with_name("gate_trigger_set.py")
    spec = importlib.util.spec_from_file_location("gate_trigger_set", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["gate_trigger_set"] = module
    spec.loader.exec_module(module)
    return module


def _publish_heads(run_git, run_gh, repository, heads, master_tip,
                   is_triggered, dry_run):
    """Publish each head's verdict; returns (stale entries, failed?).

    A per-head failure is contained: an unreadable compare publishes
    red on that head, and a failed publish is retried once and then
    flags the run so no head keeps a stale green silently.
    """
    stale = []
    publish_failed = False
    for head in heads:
        conclusion, title, summary = head_verdict(
            run_git, master_tip, head["sha"], is_triggered)
        print(f"PR #{head['number']} ({head['sha'][:12]}): {conclusion} "
              f"— {title}")
        if not dry_run:
            try:
                publish_with_retry(run_gh, repository, head["sha"],
                                   conclusion, title, summary)
            except QueryError as error:
                print(f"gate freshness: publishing the check for PR "
                      f"#{head['number']} failed twice: {error}",
                      file=sys.stderr)
                publish_failed = True
        if conclusion == "failure":
            stale.append({"number": head["number"], "sha": head["sha"],
                          "reason": title})
    return stale, publish_failed


def main_impl(run_git, run_gh, repository, env, summary_path,
              dry_run=False):
    """The whole run with injected dependencies; returns the exit code.

    Global read failures (the master ref, the open-PR list) publish
    nothing and exit nonzero.
    """
    try:
        trigger = _load_trigger_set()
        is_triggered = trigger.is_triggered
        master_tip = resolve_master_tip(run_git)
    except QueryError as error:
        print(f"gate freshness: global read failure, publishing "
              f"nothing: {error}", file=sys.stderr)
        return 1
    single = bool(env.get(ENV_PR)) and bool(env.get(ENV_HEAD_SHA))
    heads = heads_for_run(env, None)
    if not single:
        try:
            heads = heads_for_run(env, _open_pulls(repository))
        except QueryError as error:
            print(f"gate freshness: could not read the open pull-request "
                  f"list; publishing nothing: {error}", file=sys.stderr)
            return 1
    ensure_head_objects(run_git, heads, repository)
    stale, publish_failed = _publish_heads(
        run_git, run_gh, repository, heads, master_tip, is_triggered,
        dry_run)
    if not dry_run:
        write_summary(summary_path, stale, scanned=len(heads))
    return 1 if publish_failed else 0


def main():
    parser = argparse.ArgumentParser(
        description="Publish gate-freshness checks on open pull-request "
                    "heads.")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the verdicts and write nothing")
    opts = parser.parse_args()
    repository = os.environ.get("GITHUB_REPOSITORY", "")

    def run_git(argv):
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=120, check=False)
        if proc.returncode != 0:
            raise QueryError(
                proc.stderr.strip()[:400] or f"git exit {proc.returncode}")
        return proc.stdout

    def run_gh(argv):
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=60, check=False)
        if proc.returncode != 0:
            raise QueryError(
                proc.stderr.strip()[:400] or f"gh exit {proc.returncode}")
        return proc.stdout

    code = main_impl(run_git=run_git, run_gh=run_gh, repository=repository,
                     env=os.environ,
                     summary_path=os.environ.get("GITHUB_STEP_SUMMARY"),
                     dry_run=opts.dry_run)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
