#!/usr/bin/env python3
"""Refuse a tree VERSION that names an already-published release (issue #131).

WHY THIS EXISTS. Between releases, master carries `VERSION` naming the
next release with a `-dev` suffix (`0.4.0-dev`). A tree version that names
an existing `v<VERSION>` tag means different content ships under a version
string someone already installed — and nothing outside the repo can tell
which one a deployment runs. The version-guard workflow checks that on
every master push and PR.

THE BOT CARVE-OUT. The hourly pricing bot commits as
`github-actions[bot]`, touching only `src/pricing.json` and
`backend/constants.py` (the PRICING_VERSION bump). Those commits don't
change released behaviour, so they must pass even when the tree version is
stale — a version move is a human commit's job. The carve-out is keyed on
author AND file shape, never on paths alone: paths-only would need a
maintained shipped/non-shipped path list whose every omission silently
guts the guarantee.

Pure logic plus a thin CLI; the workflow gathers the facts with `gh` and
calls this. Usable by hand:

    python3 scripts/ci/version_guard.py decide --version 0.4.0-dev \
        --tag-exists false --author-email "" --changed-files ""

Exit code: pass → 0, refuse → 1, bad usage → 2 (argparse).
"""
from __future__ import annotations

import argparse
import re
import sys
from typing import NamedTuple

# The identity of the hourly pricing bot's commits (SV-RATE-REFRESH).
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"

# The only files a pricing-bot commit ever touches: the rate data and the
# PRICING_VERSION bump that forces a reprice, not a reparse, of the records.
BOT_FILES = frozenset({"src/pricing.json", "backend/constants.py"})

# `X.Y.Z` with an optional `-<prerelease>` suffix. The core stays free of
# hyphens, so the first `-` in a matched string is the suffix separator.
# Same shape release.yml's "Read and validate VERSION" step accepts.
_VERSION_RE = re.compile(r"^(\d+\.\d+\.\d+)(?:-([0-9A-Za-z.-]+))?$")


class Decision(NamedTuple):
    """The verdict plus the human-readable reason (goes to stdout)."""

    ok: bool
    reason: str


def parse_version(text: str) -> tuple[str, str]:
    """Split a version into `(core, prerelease)`.

    `prerelease` is the suffix without the leading `-`, `''` when absent.
    Anything else — empty, whitespace, a leading `v`, a missing component,
    a lone dash — raises ValueError.
    """
    match = _VERSION_RE.match(text)
    if match is None:
        raise ValueError(
            f"not a version (X.Y.Z with optional -suffix): {text!r}"
        )
    return match.group(1), match.group(2) or ""


def is_bot_exempt(author_email: str, changed_files: list[str]) -> bool:
    """True iff the commit is the pricing bot touching only its own files.

    Both halves are load-bearing: the bot's email alone says nothing (any
    workflow could push under it), and the file set alone would reduce the
    carve-out to a path list that human commits learn to hide behind. An
    empty file set is never exempt — a commit that touches nothing is not
    a pricing run.
    """
    if author_email != BOT_EMAIL:
        return False
    if not changed_files:
        return False
    return set(changed_files) <= BOT_FILES


def decide(
    version: str,
    tag_exists: bool,
    author_email: str,
    changed_files: list[str],
) -> Decision:
    """The whole gate, in this order:

    (a) version unparseable → refuse;
    (b) tag `v<version>` does not exist → pass (nothing shipped under it);
    (c) tag exists, commit is the pricing bot on its own files → pass;
    (d) otherwise refuse — the version is already published and this
        commit is not the carve-out.
    """
    try:
        core, _prerelease = parse_version(version)
    except ValueError:
        return Decision(
            False,
            f"version {version!r} is not semver "
            "(expected X.Y.Z with an optional -suffix)",
        )
    if not tag_exists:
        return Decision(
            True, f"v{core} is not an existing tag — nothing published names it"
        )
    if is_bot_exempt(author_email, changed_files):
        return Decision(
            True,
            f"v{core} is already published, but this commit is the pricing "
            "bot touching only its own files (src/pricing.json, "
            "backend/constants.py) — exempt",
        )
    return Decision(
        False,
        f"v{core} is already published — this commit must not ship under it "
        "again; bump VERSION (e.g. to the next -dev) instead",
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Prints the decision reason to stdout; the exit
    code is the verdict (0 pass, 1 refuse)."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    decide_p = sub.add_parser(
        "decide", help="decide whether a tree version may pass"
    )
    decide_p.add_argument("--version", required=True,
                          help="tree VERSION, e.g. 0.4.0-dev")
    decide_p.add_argument("--tag-exists", required=True,
                          choices=["true", "false"],
                          help="whether tag v<VERSION> exists")
    decide_p.add_argument("--author-email", default="",
                          help="tip commit's author email; empty on PRs")
    decide_p.add_argument("--changed-files", default="",
                          help="newline-separated filenames of the tip commit")
    args = parser.parse_args(argv)

    files = [name for name in args.changed_files.splitlines() if name]
    decision = decide(
        args.version, args.tag_exists == "true", args.author_email, files
    )
    print(decision.reason)
    return 0 if decision.ok else 1


if __name__ == "__main__":
    sys.exit(main())
