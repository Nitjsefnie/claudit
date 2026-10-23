#!/usr/bin/env python3
"""Validate a release version and wait for its push-triggered release run."""
from __future__ import annotations

import argparse
from collections.abc import Callable
import json
import os
import re
import subprocess
import sys
import time
from typing import NamedTuple


class Decision(NamedTuple):
    ok: bool
    reason: str


def parse_release_version(text: str) -> str:
    if re.fullmatch(r"\d+\.\d+\.\d+", text) is None:
        raise ValueError(f"not a release version (expected X.Y.Z): {text!r}")
    return text


def decide(version: str, tag_exists: bool, release_exists: bool) -> Decision:
    try:
        parse_release_version(version)
    except ValueError as error:
        return Decision(False, str(error))
    if tag_exists:
        return Decision(False, f"refusing: tag v{version} already exists")
    if release_exists:
        return Decision(False, f"refusing: release v{version} already exists")
    return Decision(True, f"version {version} is free")


def wait_release(
    sha: str,
    *,
    read: Callable[[list[str]], str],
    repo: str,
    timeout: float = 5400,
    grace: float = 300,
    interval: float = 20,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    started = clock()
    while True:
        payload = read([
            "gh", "api", "-H", "Cache-Control: no-cache", "--paginate",
            f"repos/{repo}/actions/runs?head_sha={sha}&per_page=100",
        ]).lstrip()
        runs = []
        while payload:
            page, end = json.JSONDecoder().raw_decode(payload)
            runs.extend(run for run in page["workflow_runs"]
                        if run["path"] == ".github/workflows/release.yml"
                        and run["event"] == "push")
            payload = payload[end:].lstrip()
        elapsed = clock() - started
        for run in runs:
            if (run["status"] == "completed"
                    and run["conclusion"] != "success"):
                raise ValueError(
                    f"release run {run['name']} ({run['id']}) "
                    f"concluded {run['conclusion']}"
                )
        if runs and all(run["status"] == "completed" for run in runs):
            return
        if not runs and elapsed >= grace:
            raise ValueError(
                f"no release run registered for {sha} within {grace:g} s"
            )
        if elapsed >= timeout:
            raise ValueError(f"timeout waiting for release run for {sha} "
                             f"after {timeout:g} s")
        sleep(interval)


def _sha(text: str) -> str:
    if re.fullmatch(r"[0-9a-f]{40}", text) is None:
        raise argparse.ArgumentTypeError("sha must be 40 lowercase hex digits")
    return text


def _read(argv: list[str]) -> str:
    return subprocess.check_output(argv, text=True)


def main(
    argv: list[str] | None = None,
    *,
    read: Callable[[list[str]], str] = _read,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    decide_p = sub.add_parser("decide", help="refuse an unavailable version")
    decide_p.add_argument("--version", required=True)
    decide_p.add_argument("--tag-exists", required=True, type=str.lower,
                          choices=["true", "false"])
    decide_p.add_argument("--release-exists", required=True, type=str.lower,
                          choices=["true", "false"])
    wait_p = sub.add_parser("wait-release", help="wait for release.yml")
    wait_p.add_argument("sha", type=_sha)
    wait_p.add_argument("--timeout", type=float, default=os.environ.get(
        "RELEASE_DISPATCH_WAIT_SECONDS", "5400"))
    wait_p.add_argument("--grace", type=float, default=os.environ.get(
        "RELEASE_DISPATCH_WAIT_GRACE_SECONDS", "300"))
    wait_p.add_argument("--interval", type=float, default=os.environ.get(
        "RELEASE_DISPATCH_WAIT_POLL_SECONDS", "20"))
    args = parser.parse_args(argv)
    if args.command == "decide":
        decision = decide(args.version, args.tag_exists == "true",
                          args.release_exists == "true")
        print(decision.reason)
        return 0 if decision.ok else 1
    try:
        repo = os.environ.get("REPO")
        if not repo:
            raise ValueError("REPO must be set to the owner/repository")
        wait_release(args.sha, read=read, repo=repo, timeout=args.timeout,
                     grace=args.grace, interval=args.interval)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f"release_dispatch: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
