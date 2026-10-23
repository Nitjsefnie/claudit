#!/usr/bin/env python3
"""Validate a release version and compute its development successor."""
from __future__ import annotations

import argparse
import re
import sys
from typing import NamedTuple


class Decision(NamedTuple):
    ok: bool
    reason: str


def parse_release_version(text: str) -> str:
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", text) is None:
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    decide_p = sub.add_parser("decide", help="refuse an unavailable version")
    decide_p.add_argument("--version", required=True)
    decide_p.add_argument("--tag-exists", required=True, type=str.lower,
                          choices=["true", "false"])
    decide_p.add_argument("--release-exists", required=True, type=str.lower,
                          choices=["true", "false"])
    successor_p = sub.add_parser("successor", help="print the next development version")
    successor_p.add_argument("version")
    args = parser.parse_args(argv)
    if args.command == "decide":
        decision = decide(args.version, args.tag_exists == "true",
                          args.release_exists == "true")
        print(decision.reason)
        return 0 if decision.ok else 1
    try:
        major, minor, patch = map(int, parse_release_version(args.version).split("."))
        print(f"{major}.{minor}.{patch + 1}-dev")
    except ValueError as error:
        print(f"release_dispatch: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
