#!/usr/bin/env python3
"""The gate trigger set: paths whose change on master strands open heads.

The gate-freshness check (scripts/ci/gate_freshness.py) goes red when
master holds a commit an open pull-request head lacks whose changed
paths intersect this set. The set has four parts:

FAMILIES. Whole directories whose every member defines a gate: the
workflows, the local composite actions CI runs, and the CI scripts.

EXPLICIT. Files named by hand. Two kinds:

- Gate parameters the extraction also finds, held by hand anyway so
  their presence never depends on a spelling surviving in a workflow
  text: the ratchet data — the correction at this set's core, since a
  tighten raises what the gates demand of the same source tree even
  though it touches nothing in any pull request's diff (issue #495) —
  the scanner config, and the three requirements files the audit and
  test legs install from.
- Gate parameters read by tool DISCOVERY, invisible to the extraction:
  bare `pylint` / `pycodestyle` / `pyright` / `eslint` load
  `.pylintrc` / `setup.cfg` / `pyrightconfig.json` /
  `eslint.config.mjs` without any workflow naming them.

EXEMPT. Command references that are NOT gate parameters, so over-broad
extraction cannot turn them into false triggers. Each carries its
reason:

- `src/pricing.json` — the hourly rate-refresh bot's data file. No
  gate leg's verdict depends on its content: the perturbed-data leg
  exists to prove exactly that, and the bot's push is itself
  gate-silent. Making it a trigger would strand every open head red on
  every hourly bot commit for zero safety.
- `backend/constants.py` — the version constants are read at run time
  and never pinned by the suite (tests derive expectations from them
  at run time), so no leg's verdict depends on the file's content; the
  bot's commit touches it alongside the data file.

THE EXTRACTION. Over-broad on purpose: it tokenizes workflow texts for
path-shaped tokens resolving to tracked files, so an over-read turns
the coverage test red and forces an explicit decision, while an
under-read would only ever be silent. Command-bearing lines only —
lines whose first non-blank character is `#` are prose, and a path
named in prose is a mention, not a read. Its REACH LIMIT, stated so
the hand-held half can be checked against it: `./script.sh`,
`python3 -m package.module`, and anything a tool reads by discovery
that the hand-held list misses are invisible to a text matcher.

Tests pin all four parts (tests/test_gate_trigger_set.py).
"""
from __future__ import annotations

import re

WORKFLOW_GLOB = ".github/workflows/*.yml"

# Directory families: any changed path under one is a trigger.
FAMILIES = (
    ".github/workflows/",
    ".github/actions/",
    "scripts/ci/",
)

# Files held by hand: gate parameters whose presence in the set must
# not depend on a spelling surviving in a workflow text, plus the four
# tool-discovery configs the extraction cannot see.
EXPLICIT = (
    ".github/ci-thresholds.json",
    ".gitleaks.toml",
    "backend/requirements.txt",
    "requirements-dev.txt",
    "requirements-test.txt",
    ".pylintrc",
    "setup.cfg",
    "pyrightconfig.json",
    "eslint.config.mjs",
)

# Command references that are not gate parameters. Exempt wins over
# every other part of the set.
EXEMPT = {
    "src/pricing.json": (
        "the rate-refresh bot's data file; no gate leg's verdict depends"
        " on its content (the perturbed-data leg proves it), and the"
        " bot's push is itself gate-silent"),
    "backend/constants.py": (
        "version constants are read at run time and never pinned by the"
        " suite, so no gate leg's verdict depends on the file's"
        " content; the bot's commit touches it beside the data file"),
    "VERSION": (
        "the release input; the version guard is not a required check"
        " (the ruleset requires the aggregate alone) and no aggregate"
        " leg reads it"),
    "backend": (
        "a suite/coverage path argument and working directory"
        " (--cov=backend), not a rule definition; the code it stands"
        " for is production source, which the set deliberately does"
        " not trigger on — a head's verdict is computed from its own"
        " tree, and only files that move the RULES strand it"),
    "tests": (
        "the pytest path argument; test code is production source's"
        " sibling here — a head's verdict is computed from its own"
        " tree, and only files that move the RULES strand it"),
    "fixtures": (
        "the fixture mirror's path argument in the test-data legs;"
        " fixture content is not a rule definition"),
}

# One optional leading dot, not preceded by a word, dot or dash
# character, so `.gitleaks.toml` and `.github/x` extract while the
# dot inside `3.13` does not start a token.
_EXTRACT_RE = re.compile(r"(?<![\w.-])\.?[A-Za-z0-9_][A-Za-z0-9_./-]*")


def _command_lines(text: str):
    for line in text.splitlines():
        if line.strip() and not line.strip().startswith("#"):
            yield line


def references_from_workflows(
    workflow_texts: dict[str, str],
    tracked: set[str],
) -> set[str]:
    """Path-shaped tokens in `workflow_texts` that name tracked files.

    A token counts when it resolves against `tracked`, exactly or as a
    directory prefix (token + `/` below some tracked file — the
    `.github/actions/<name>` shape). Deliberately over-broad: comments
    are the only noise removed, and an over-read is safe because the
    coverage test fails on it instead of silently dropping a file.
    """
    found: set[str] = set()
    for text in workflow_texts.values():
        for line in _command_lines(text):
            for token in _EXTRACT_RE.findall(line):
                if token in tracked or any(
                    name.startswith(token + "/") for name in tracked
                ):
                    found.add(token)
    return found


def is_triggered(path: str, families=FAMILIES, explicit=EXPLICIT,
                 exempt=None) -> bool:
    """Whether a changed path intersects the trigger set."""
    if path in (EXEMPT if exempt is None else exempt):
        return False
    return (any(path.startswith(family) or path + "/" == family
                for family in families)
            or path in explicit)


def uncovered_references(references: set[str]) -> list[str]:
    """References the trigger set does not cover, sorted.

    EXEMPT members are not listed: they are adjudicated command
    references — each carries its reason in EXEMPT, and the suite pins
    that set's membership separately.
    """
    return sorted(
        ref for ref in references
        if ref not in EXEMPT and not is_triggered(ref))
