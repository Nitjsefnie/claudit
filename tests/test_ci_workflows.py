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

Also the CI-tool manifest tripwires (issue #551): Dependabot's pip ecosystem
reads manifest files, never a workflow `run:` block, so an inline
`pip install name==ver` pin can only go stale unnoticed — the pins live in
requirements manifests at the repo root, the zizmor one hash-pinned, and
audit.yml's audit step covers every manifest in the tree.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

CODEQL_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "codeql.yml"
DEPENDABOT = REPO_ROOT / ".github" / "dependabot.yml"
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
ZIZMOR_MANIFEST = REPO_ROOT / "requirements-zizmor.txt"

# a pip requirement carrying a version spec — the inline-pin shape
# Dependabot can never see. Single-char comparison operators need a
# digit/letter after them so a `> audit.out` redirect never matches.
_REQ_OPERATOR = re.compile(
    r"[A-Za-z0-9_.\[\]-]+\s*(==|~=|!=|<=|>=|<(?=[0-9A-Za-z])|>(?=[0-9A-Za-z]))"
)


def _run_blocks(path: Path) -> list[str]:
    """Every step's `run:` string in one workflow, YAML-decoded."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    blocks: list[str] = []
    for job in (doc.get("jobs") or {}).values():
        for step in (job or {}).get("steps") or []:
            run = (step or {}).get("run")
            if isinstance(run, str):
                blocks.append(run)
    return blocks


def _pip_install_segments(run: str) -> list[str]:
    """Each `pip install` command's argument segment, continuations joined."""
    segments: list[str] = []
    lines = run.splitlines()
    for i, line in enumerate(lines):
        if not re.search(r"\bpip3?\s+install\b", line):
            continue
        segment = line
        j = i
        while segment.rstrip().endswith("\\") and j + 1 < len(lines):
            j += 1
            segment = segment.rstrip().removesuffix("\\") + " " + lines[j]
        segments.append(segment)
    return segments


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


def test_every_group_has_a_security_updates_twin() -> None:
    """A group covers the kind it names, and the kind DEFAULTS to version.

    Security updates are enabled on this repository (verified 2026-10-04
    via `gh api repos/Nitjsefnie/claudit/automated-security-fixes`), and
    they skip a version-updates group entirely — one written that way
    explicitly, and one that simply never names `applies-to`. A codeql-action
    CVE bump then arrives as one pull request per `uses:` line, leaving init
    and analyze on different versions — the same red PR the group above
    exists to prevent (issue #555; Nitjsefnie-Actions/claim PR #38, pr-gate
    PR #29).

    A twin must match its sibling in EVERY key but `applies-to`, not merely
    be catch-all: one that narrowed `patterns`, or added
    `exclude-patterns`, `update-types` or `dependency-type`, still reads as a
    twin to a membership-only check while bundling a different set — and for
    `github/codeql-action` that is the very split #555 is about.
    """
    doc = yaml.safe_load(DEPENDABOT.read_text(encoding="utf-8")) or {}
    unpaired = []
    for entry in doc.get("updates") or []:
        groups = {
            name: group
            for name, group in ((entry or {}).get("groups") or {}).items()
            if isinstance(group, dict)
        }
        shape = {
            name: {k: v for k, v in group.items() if k != "applies-to"}
            for name, group in groups.items()
        }
        kind = {
            name: group.get("applies-to", "version-updates")
            for name, group in groups.items()
        }
        opposite = {"version-updates": "security-updates",
                    "security-updates": "version-updates"}
        for name in groups:
            if not any(n != name and kind[n] == opposite[kind[name]]
                       and shape[n] == shape[name] for n in groups):
                unpaired.append((entry.get("package-ecosystem"), name,
                                 kind[name], sorted(shape[name])))
    assert not unpaired, (
        "a group has no twin of the opposite applies-to kind carrying an "
        "identical rule, so that kind's bumps arrive unbundled or bundled "
        "differently: " + repr(unpaired)
    )


# A container image reference carrying an explicit digest — `name:tag@sha256:…`
# or `name@sha256:…`. Bare `name` and `name:tag` are floating.
_DIGESTED_IMAGE = re.compile(r"^\S+@sha256:[0-9a-f]{64}$")
# A step that runs an image instead of an action: `uses: docker://image`.
_DOCKER_REF = "docker://"


def _workflow_files() -> list[Path]:
    """Every workflow file. GitHub reads `.yml` AND `.yaml`."""
    found = set(WORKFLOWS_DIR.glob("*.yml")) | set(WORKFLOWS_DIR.glob("*.yaml"))
    return sorted(found)


def _job_container_images(job: dict) -> list[tuple[str, str]]:
    """(label, image) for the containers this job itself runs.

    A job's `container` has two legal shapes: the mapping with an `image`
    key, and the bare string shorthand (`container: node:20`, documented
    as "when you only specify a container image, you can omit the image
    keyword"). Only the mapping shape is handled otherwise, so the
    shorthand would sail past the gate.
    """
    container = job.get("container")
    if isinstance(container, str) and container:
        return [("container", container)]
    if isinstance(container, dict) and container.get("image"):
        return [("container", container["image"])]
    return []


def _service_images() -> list[tuple[str, str]]:
    """(where, image) for every container image a workflow runs.

    Enumerated from the workflow grammar, not from the spellings this
    repository happens to use: a service container
    (`jobs.<job>.services.<id>.image`), a job's own container (both
    shapes), and a step that pulls an image directly
    (`uses: docker://…`). Read from DECODED YAML, so a value is never
    confused with a comment naming it.
    """
    found: list[tuple[str, str]] = []
    for path in _workflow_files():
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for job_name, job in (doc.get("jobs") or {}).items():
            job = job or {}
            where = f"{path.name}:jobs.{job_name}"
            for label, image in _job_container_images(job):
                found.append((f"{where}.{label}", image))
            for svc_name, svc in (job.get("services") or {}).items():
                image = (svc or {}).get("image")
                if image:
                    found.append(
                        (f"{where}.services.{svc_name}", image))
            for index, step in enumerate(job.get("steps") or []):
                uses = (step or {}).get("uses") or ""
                if isinstance(uses, str) and uses.startswith(_DOCKER_REF):
                    found.append((f"{where}.steps[{index}]",
                                  uses[len(_DOCKER_REF):]))
    return found


def test_every_workflow_container_image_is_digest_pinned() -> None:
    """No workflow names a container image by a moving tag (issue #559).

    Three of the five workflows that start the suite's Postgres named it
    `image: postgres:16`, a tag upstream re-points, so the database a CI
    leg measured against could change under a fixed commit. Every image is
    pinned to a digest instead.
    """
    images = _service_images()
    # the oracle must be live: a refactor that stopped finding the
    # containers must not silence this into a vacuous pass
    assert images, "found no container image in .github/workflows/"
    unpinned = [(where, image) for where, image in images
                if not _DIGESTED_IMAGE.match(image)]
    assert not unpinned, (
        "container images named by a moving tag or bare name — pin each to "
        "the digest the other jobs use: " + repr(unpinned)
    )


def test_postgres_service_images_share_one_digest() -> None:
    """One Postgres image across every workflow that starts one.

    Two digests of the same tag are two databases: a leg's results stop
    being comparable with a gate leg's, which is the whole reason the
    publish path pins what it measured on.
    """
    postgres = {where: image for where, image in _service_images()
                if image.split("@", 1)[0].split(":", 1)[0].rsplit(
                    "/", 1)[-1] == "postgres"}
    assert postgres, (
        "no workflow starts a Postgres service — extend this test if the "
        "image's name changed"
    )
    digests = {image.rpartition("@")[2] for image in postgres.values()}
    assert len(digests) == 1, (
        "the Postgres service images disagree on their digest: "
        f"{dict(sorted(postgres.items()))}"
    )


def test_codeql_matrix_analyses_the_workflows() -> None:
    """The `actions` language stays in the CodeQL matrix (issue #552).

    The workflow files are among the most privileged code here: a bot
    pushes ratchet commits to master with a deploy key, and the
    `pull_request_target` gates decide with the base repository's
    secrets. actionlint and zizmor read them for known-wrong shapes;
    CodeQL asks a dataflow question of the same files, which the other
    two do not. Nothing else in the tree fails if the entry is dropped —
    a shorter matrix is a perfectly valid workflow, and only the missing
    code-scanning alerts say otherwise — so the tripwire is here.

    `actions` takes no build; every entry carries `build-mode: none`,
    because this tree has no compiled language for CodeQL to build —
    CodeQL's own autobuild is not what the entry is declining.

    The matrix is DECLARED, not consumed: an `include` list nothing
    reads passes the checks above and analyses one language three
    times. So the plumbing is pinned too — the init step's `languages`
    and `build-mode`, and the analyze step's `category`, each
    interpolate the matrix rather than naming a language themselves.
    A hard-coded `languages: python` under a three-entry matrix is the
    mutant these last assertions exist for.
    """
    doc = yaml.safe_load(CODEQL_WORKFLOW.read_text(encoding="utf-8")) or {}
    job = (doc.get("jobs") or {}).get("analyze") or {}
    matrix = job.get("strategy")
    entries = ((matrix or {}).get("matrix") or {}).get("include") or []
    languages = [entry.get("language") for entry in entries]
    assert "actions" in languages, (
        "the CodeQL matrix no longer analyses the workflow files: "
        f"{languages}"
    )
    assert {"python", "javascript-typescript"} <= set(languages), (
        f"the CodeQL matrix lost a shipped language: {languages}"
    )
    assert all(entry.get("build-mode") == "none" for entry in entries), (
        "a matrix entry carries a build mode CodeQL would try to run; "
        f"{entries}"
    )

    # the oracle must be live: a rename or a dropped step must not
    # silence the plumbing assertions into a vacuous pass
    steps = [step for step in (job.get("steps") or []) if isinstance(step, dict)]
    init = [s for s in steps
            if (s.get("uses") or "").partition("@")[0].rstrip("/")
            == "github/codeql-action/init"]
    analyze = [s for s in steps
               if (s.get("uses") or "").partition("@")[0].rstrip("/")
               == "github/codeql-action/analyze"]
    assert len(init) == 1 and len(analyze) == 1, (
        f"expected one init and one analyze step, found "
        f"{len(init)} and {len(analyze)}"
    )
    with_ = init[0].get("with") or {}
    assert with_.get("languages") == "${{ matrix.language }}", (
        "the init step does not read the matrix's language, so the "
        f"matrix does not choose what is analysed: {with_.get('languages')!r}"
    )
    assert with_.get("build-mode") == "${{ matrix.build-mode }}", (
        "the init step does not read the matrix's build mode: "
        f"{with_.get('build-mode')!r}"
    )
    category = (analyze[0].get("with") or {}).get("category")
    assert category == "/language:${{ matrix.language }}", (
        "the analyze step does not file its SARIF under the matrix's "
        f"language category: {category!r}"
    )


def test_workflow_pip_installs_never_pin_inline() -> None:
    """A pip install in a workflow installs from a manifest, never a pin.

    Dependabot's pip ecosystem reads requirements manifests, never a
    workflow `run:` block, so `pip install name==ver` in a step is a pin
    that can only go stale unnoticed — zizmor sat at 1.29.0 through two
    releases this way (issue #551). `--upgrade pip` is exempt here: it
    carries no version operator, and upgrading the installer itself is
    refresh-pricing/tests/lint's existing shape, not this issue's scope.
    """
    offenders = []
    for path in _workflow_files():
        for run in _run_blocks(path):
            for segment in _pip_install_segments(run):
                if _REQ_OPERATOR.search(segment):
                    offenders.append(f"{path.name}: {segment.strip()}")
    assert not offenders, (
        "a workflow pins a package inline in a run: block, where "
        "Dependabot's pip ecosystem can never see it — move it into a "
        "requirements manifest at the repo root: " + repr(offenders)
    )


def test_zizmor_manifest_is_hash_pinned_and_require_hashes_installed() -> None:
    """The zizmor manifest carries hashes, and its install verifies them.

    actionlint runs before every other gate, so the linter's install is a
    supply-chain position worth defending: a version pin alone still
    installs whatever artifact the index serves for that version. The
    manifest is hash-pinned, and the workflow installs it with
    --require-hashes, which pip refuses to run against a manifest missing
    a hash — so the two assertions below fail together by construction.
    """
    logical: list[str] = []
    pending = ""
    for raw in ZIZMOR_MANIFEST.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.endswith("\\"):
            pending += line[:-1].strip() + " "
            continue
        entry = (pending + line).strip()
        pending = ""
        if entry and not entry.startswith("#"):
            logical.append(entry)
    requirements = logical
    assert requirements, f"{ZIZMOR_MANIFEST.name} names no requirement"
    unpinned = [
        entry for entry in requirements
        if not re.match(r"^[A-Za-z0-9_.\[\]-]+==\S+", entry)
        or "--hash=sha256:" not in entry
    ]
    assert not unpinned, (
        "a requirement in the zizmor manifest lacks a == pin with a hash, "
        "so --require-hashes would reject the install — every line needs "
        "the name==version and at least one --hash=sha256: " + repr(unpinned)
    )
    installs = [
        segment for run in _run_blocks(WORKFLOWS_DIR / "actionlint.yml")
        for segment in _pip_install_segments(run)
        if "requirements-zizmor.txt" in segment
    ]
    assert installs and all(
        "--require-hashes" in segment and "-r" in segment
        for segment in installs
    ), (
        "the zizmor manifest install does not use --require-hashes -r: "
        + repr(installs)
    )


def test_audit_gate_covers_every_requirements_manifest() -> None:
    """Every requirements manifest in the tree is audited by audit.yml.

    The gate's job is "is a version we froze still safe", and a manifest
    it does not name is frozen outside its view — the manifest Dependabot
    keeps fresh (issue #551) would be exactly one pip-audit never saw.
    """
    audited: set[str] = set()
    for run in _run_blocks(WORKFLOWS_DIR / "audit.yml"):
        audited |= set(re.findall(r"--requirement\s+(\S+)", run))
    manifests = {"backend/requirements.txt"} | {
        path.name for path in REPO_ROOT.glob("requirements*.txt")
    }
    assert audited == manifests, (
        "audit.yml's --requirement set and the tree's manifests disagree — "
        f"unaudited: {sorted(manifests - audited)}, "
        f"phantom: {sorted(audited - manifests)}"
    )
