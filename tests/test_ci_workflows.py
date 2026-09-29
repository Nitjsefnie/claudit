"""Tripwires for the codeql-action pin coupling (issue #335).

github/codeql-action/init and github/codeql-action/analyze must run the same
version inside one workflow run: the action records its version at init and
refuses a later step at a different one — PR #62's analyze log records
exactly that refusal ("Loaded a configuration file for version '4.38.1',
but running version '4.37.7'") — which fails every CodeQL run of the tree
at SARIF processing ("Error when processing the SARIF file"). Dependabot
names the two subpaths as separate dependencies, so ungrouped it files one
half-bump PR per pin, and each half fails CI on its own (#25/#26, #31/#32,
#38/#39, #50/#51, #62/#63 — four of the five pairs were closed unmerged).
The dependabot.yml groups block keeps the pins in one atomic PR; these
tests are the in-tree layer that fails if a single-pin bump ever lands,
and that fails loudly if the group is ever removed.

The workflows and dependabot.yml are asserted on their YAML-DECODED
structure, the way Dependabot itself reads them — a regex over the raw text
would satisfy a decoy spelling and cannot tell a value from a comment. The
one exception is the pin comments themselves, which do not survive YAML
parsing, so that pin is necessarily textual.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

CODEQL_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "codeql.yml"
DEPENDABOT = REPO_ROOT / ".github" / "dependabot.yml"

# `uses: github/codeql-action/<step>@<sha>  # vX.Y.Z` — text-only, by
# necessity: a YAML parse drops comments.
_PIN_COMMENT = re.compile(
    r"uses:\s*github/codeql-action/(?P<step>\S+)@(?P<sha>[0-9a-f]{40})"
    r"\s+#\s*(?P<comment>\S+)"
)


def _codeql_action_uses() -> list[tuple[str, str]]:
    """Yield (step, uses-ref) for every codeql-action step of codeql.yml."""
    doc = yaml.safe_load(CODEQL_WORKFLOW.read_text(encoding="utf-8")) or {}
    uses: list[tuple[str, str]] = []
    for job in (doc.get("jobs") or {}).values():
        for step in (job or {}).get("steps") or []:
            ref = (step or {}).get("uses") or ""
            action, _, sha = ref.partition("@")
            if action.rstrip("/") == "github/codeql-action/init":
                uses.append(("init", sha))
            elif action.rstrip("/") == "github/codeql-action/analyze":
                uses.append(("analyze", sha))
    return uses


def test_codeql_action_pins_share_one_sha() -> None:
    uses = _codeql_action_uses()
    steps = {step for step, _ in uses}
    # the oracle must be live: a refactor that renames the steps or the
    # workflow must not silence this file into a vacuous pass
    assert {"init", "analyze"} <= steps, f"missing pins: {sorted(steps)}"
    shas = {sha for _, sha in uses}
    assert len(shas) == 1, (
        "codeql-action pins disagree — init and analyze must run the same "
        "version or every CodeQL run fails at SARIF processing: "
        f"{sorted(shas)}"
    )
    assert all(sha for _, sha in uses), "a codeql-action pin carries no SHA"


def test_codeql_action_pin_comments_are_immutable_release_tags() -> None:
    text = CODEQL_WORKFLOW.read_text(encoding="utf-8")
    comments = {
        match["step"]: match["comment"]
        for match in _PIN_COMMENT.finditer(text)
    }
    # the oracle must be live: exactly the two coupled steps' comments
    assert set(comments) == {"init", "analyze"}, (
        f"expected the init and analyze pin comments, found {sorted(comments)}"
    )
    # a floating major tag (# v4) decays when upstream re-points it, so each
    # comment must name a release tag of the immutable vX.Y.Z SHAPE. The
    # shape is the convention this gate enforces; a release's actual
    # immutability (its `immutable` flag) is verified reviewer-side, never
    # by a name pattern.
    bad = {
        c for c in comments.values()
        if not re.fullmatch(r"v\d+\.\d+\.\d+", c)
    }
    assert not bad, f"pin comments are not vX.Y.Z release tags: {sorted(bad)}"
    assert len(set(comments.values())) == 1, (
        f"pin comments disagree across steps: {sorted(comments.values())}"
    )


def test_dependabot_groups_action_updates_into_one_pr() -> None:
    doc = yaml.safe_load(DEPENDABOT.read_text(encoding="utf-8")) or {}
    entries = [
        entry
        for entry in doc.get("updates") or []
        if (entry or {}).get("package-ecosystem") == "github-actions"
    ]
    assert len(entries) == 1, (
        "expected exactly one github-actions update entry, "
        f"found {len(entries)}"
    )
    groups = entries[0].get("groups") or {}
    assert groups, (
        "the github-actions entry must group its updates — ungrouped, "
        "Dependabot files one half-bump PR per codeql-action pin"
    )
    versions = [
        group
        for group in groups.values()
        if isinstance(group, dict)
        and group.get("applies-to") == "version-updates"
        and "*" in (group.get("patterns") or [])
    ]
    assert versions, (
        "the entry must carry a group applying to version-updates with a "
        'catch-all "*" pattern — a security-updates-only or narrowed group '
        "never bundles the weekly bump and the half-bumps return"
    )
