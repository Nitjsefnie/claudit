"""CodeQL init and analyze must use the same action version in one workflow.
Init records its action version; analyze refuses configuration from another
version. Dependabot treats the subpaths separately, so group updates atomically.
Inspect decoded workflow and Dependabot structures rather than regexes that
can accept decoy comments; action pin comments require a textual check.
Keep CI-tool pins in requirement manifests so Dependabot sees them and audit
covers them; an inline pip-install version can otherwise go stale unnoticed."""
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
    """Dependabot groups default to version updates, excluding security updates.
    Security and version groups must match in every key except applies-to,
    including patterns, exclude-patterns, update-types and dependency-type.
    Otherwise a CodeQL update can split init and analyze across versions."""
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


POSTGRES_MIRROR = "public.ecr.aws/docker/library/postgres:"


def test_every_postgres_job_uses_the_cached_action() -> None:
    """A service/container bypass would pull before the cache can restore."""
    postgres = [(where, image) for where, image in _service_images()
                if "postgres" in image]
    assert not postgres, postgres
    callers = []
    for path in _workflow_files():
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for job in (doc.get("jobs") or {}).values():
            callers.extend(path.name for step in job.get("steps", [])
                           if step.get("uses") == "$/.github/actions/postgres")
    assert sorted(callers) == sorted([
        "tests.yml", "tests.yml", "tests.yml", "test-data.yml", "test-data-explore.yml",
        "smoke.yml", "refresh-pricing.yml"]), callers


def test_postgres_action_pulls_a_digest_from_the_public_ecr_mirror() -> None:
    """The shared miss path retains the mirror and reviewed digest pin."""
    path = REPO_ROOT / ".github/actions/postgres-image/action.yml"
    steps = yaml.safe_load(path.read_text(encoding="utf-8"))["runs"]["steps"]
    image = next(step for step in steps if step.get("id") == "image")
    assert image["env"]["POSTGRES_REPOSITORY"].startswith(POSTGRES_MIRROR)
    assert re.fullmatch(r"sha256:[0-9a-f]{64}",
                        image["env"]["POSTGRES_DIGEST"])


def test_codeql_matrix_analyses_the_workflows() -> None:
    """The `actions` language stays in the CodeQL matrix.

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
    """Install CI tools from manifests so Dependabot can discover their pins.
    An unversioned --upgrade pip is exempt because it carries no version operator."""
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
        "--require-hashes" in segment and re.search(r"(^|\s)-r\s", segment)
        for segment in installs
    ), (
        "the zizmor manifest install does not use --require-hashes -r: "
        + repr(installs)
    )


def test_actionlint_pin_parses_self_repository_uses() -> None:
    """The pinned actionlint build parses the `$/` self-repository refs.

    Stock actionlint's workflow-call and action format checks reject the
    `$/` syntax GitHub's self-repository form uses, and the
    inline `# actionlint: ignore` comment cannot silence them — the
    migrated `uses: $/.github/...` refs are unlintable below
    fork build 1.7.12-queue.2, which carries parser support for that form.
    This is a threshold in the fork's queue series, not an
    exact pin: the next fork build (queue.3+) must keep passing. Any
    other shape — stock upstream, an older queue build, a mangled
    version — fails closed: the gate cannot be shown to parse the refs,
    and re-pinning is a deliberate act that re-examines this test.
    """
    match = re.search(
        r"^\s*ACTIONLINT_VERSION: (\S+)\s*$",
        (WORKFLOWS_DIR / "actionlint.yml").read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert match, f"{WORKFLOWS_DIR / 'actionlint.yml'} carries no ACTIONLINT_VERSION pin"
    version = match.group(1)
    queue = re.fullmatch(r"1\.7\.12-queue\.(\d+)", version)
    assert queue and int(queue.group(1)) >= 2, (
        "the actionlint pin does not parse the `$/` self-repository uses: "
        "refs (fork build 1.7.12-queue.2 was the first that did, upstream "
        "rhysd/actionlint#732) — the migrated refs break the gate on a "
        f"stock or older build: {version}"
    )


def test_self_repository_uses_use_the_dollar_slash_form() -> None:
    """Self-repository uses references take $/ with no suppression.
    The pinned actionlint supports that form and zizmor accepts it directly.
    Scope the check to .github/workflows, the surface the audit scans."""
    offenders: list[str] = []
    for path in _workflow_files():
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1
        ):
            if re.match(r"^\s*uses:\s+\./", line):
                offenders.append(f"{path.name}:{lineno}: workspace-relative")
    assert not offenders, (
        "a workspace-relative local uses: reference remains — migrate it "
        "to GitHub's $/ self-repository form (the pinned actionlint parses "
        "it since 1.7.12-queue.2, see #605): " + repr(offenders)
    )


def test_audit_gate_covers_every_requirements_manifest() -> None:
    """Every requirements manifest in the tree is audited by audit.yml.

    The gate's job is "is a version we froze still safe", and a manifest
    it does not name is frozen outside its view — the manifest Dependabot
    keeps fresh would be exactly one pip-audit never saw.
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


class TestRefreshPricingShards:
    def setup_method(self) -> None:
        self.jobs = yaml.safe_load(
            (WORKFLOWS_DIR / "refresh-pricing.yml").read_text(encoding="utf-8")
        )["jobs"]

    def test_fetch_suite_stage_push_jobs(self) -> None:
        assert set(self.jobs) == {"refresh", "suite", "stage", "push", "verdict"}
        assert set(self.jobs["push"]["needs"]) == {"refresh", "stage"}
        assert self.jobs["verdict"]["needs"] == ["refresh", "stage", "push"]
        assert self.jobs["verdict"]["if"] == "${{ always() }}"

    def test_stage_waits_for_the_suite(self) -> None:
        stage = self.jobs["stage"]
        assert set(stage["needs"]) == {"refresh", "suite"}
        assert stage["if"] == "needs.refresh.outputs.moved == 'true'"
        staging = next(step["run"] for step in stage["steps"]
                       if step.get("name") == "Stage the tested data")
        download = next(step for step in stage["steps"]
                        if step.get("name") == "Download the fetched data")
        upload = next(step for step in stage["steps"]
                      if step.get("name") == "Upload the tested data")
        assert {line for line in staging.splitlines() if line.startswith("cp ")} == {
            f'cp "{download["with"]["path"]}/{filename}" '
            f'"{upload["with"]["path"]}/{filename}"'
            for filename in ["pricing.json", "constants.py", "pricing_notices.json",
                             "commit-msg.txt", "base.txt"]
        }

    def test_suite_runs_four_shards_only_when_data_moved(self) -> None:
        suite = self.jobs["suite"]
        assert suite["needs"] == "refresh"
        assert suite["if"] == "needs.refresh.outputs.moved == 'true'"
        assert suite["strategy"] == {
            "fail-fast": False, "matrix": {"shard": [1, 2, 3, 4]}
        }
        steps = suite["steps"]
        run = next(step["run"] for step in steps if step.get("name") == "Run tests")
        assert (
            "python -m pytest $(python3 scripts/ci/pytest_shards.py "
            "--shard ${{ matrix.shard }} --of 4) -q --tb=short -ra"
        ) in run
        assert "--cov" not in run

    def test_fetch_and_suite_interpreters_match_the_explorer(self) -> None:
        explorer = yaml.safe_load(
            (WORKFLOWS_DIR / "test-data-explore.yml").read_text(encoding="utf-8")
        )["jobs"]
        versions = []
        for jobs, required in [(self.jobs, ["refresh", "suite"]),
                               (explorer, ["perturbed-suite-explore"])]:
            setups = {name: [step for step in job["steps"]
                            if "python-version" in step.get("with", {})]
                      for name, job in jobs.items()}
            assert all(setups[name] for name in required)
            versions.extend(step["with"]["python-version"]
                            for steps in setups.values() for step in steps)
        assert versions[0] and len(set(versions)) == 1
        steps = self.jobs["refresh"]["steps"]
        fetch = next(i for i, step in enumerate(steps) if step.get("id") == "fetch")
        assert all(i < fetch for i, step in enumerate(steps)
                   if "python-version" in step.get("with", {}))

    def test_suite_overlays_all_fetched_data(self) -> None:
        steps = self.jobs["suite"]["steps"]
        download = next(step for step in steps if step.get("name") == "Download the fetched data")
        overlay = next(step["run"] for step in steps
                       if step.get("name") == "Overlay the fetched data")
        assert {line for line in overlay.splitlines() if line.startswith("cp ")} == {
            f'cp "{download["with"]["path"]}/{filename}" {target}'
            for filename, target in [
                ("pricing.json", "src/pricing.json"),
                ("constants.py", "backend/constants.py"),
                ("pricing_notices.json", "src/pricing_notices.json"),
            ]
        }

    def test_postgres_lives_in_suite_only(self) -> None:
        for workflow in ["refresh-pricing.yml", "test-data-explore.yml"]:
            jobs = yaml.safe_load((WORKFLOWS_DIR / workflow).read_text(encoding="utf-8"))["jobs"]
            consumers = 0
            for job in jobs.values():
                steps = job["steps"]
                tests = [i for i, step in enumerate(steps) if
                         "python -m pytest $(python3 scripts/ci/pytest_shards.py" in step.get("run", "")]
                if tests:
                    consumers += 1
                    starts = [i for i, step in enumerate(steps) if step.get("name") == "Start cached Postgres"]
                    assert len(starts) == 1 and starts[0] < min(tests)
                    assert "uses" in steps[starts[0]] and "if" not in steps[starts[0]]
            assert consumers
        assert not any(step.get("name") == "Start cached Postgres"
                       for step in self.jobs["refresh"]["steps"])

    def test_artifact_names_connect_producers_to_consumers(self) -> None:
        def named(job: str, name: str) -> dict:
            return next(step for step in self.jobs[job]["steps"] if step.get("name") == name)

        raw = named("refresh", "Upload the fetched data")
        staging = named("refresh", "Stage the fetched data")["run"]
        outputs = {line.rsplit('"', 2)[1].replace("$RUNNER_TEMP", "${{ runner.temp }}")
                   for line in staging.splitlines() if line.startswith(("cp ", "git rev-parse HEAD > "))}
        assert outputs == {f'{raw["with"]["path"]}/{filename}' for filename in
                           ["pricing.json", "constants.py", "pricing_notices.json", "commit-msg.txt", "base.txt"]}
        for job in ["suite", "stage"]:
            download = named(job, "Download the fetched data")
            assert "uses" in download and "run" not in download
            assert raw["with"]["name"] == download["with"]["name"]
        tested = named("stage", "Upload the tested data")
        assert tested["with"]["name"] == named("push", "Download the tested data")["with"]["name"]

    def test_fetch_and_moved_commands_are_preserved(self) -> None:
        steps = {step.get("id"): step for step in self.jobs["refresh"]["steps"]}
        fetch = steps["fetch"]["run"]
        assert self.jobs["refresh"]["outputs"] == {
            "moved": "${{ steps.moved.outputs.moved }}",
            "fetch_rc": "${{ steps.fetch.outputs.rc }}",
        }
        for job in ["suite", "stage", "push"]:
            assert self.jobs[job]["if"] == "needs.refresh.outputs.moved == 'true'"
        verdict, = self.jobs["verdict"]["steps"]
        assert verdict["env"]["FETCH_RC"] == "${{ needs.refresh.outputs.fetch_rc }}"
        assert "python3 scripts/ci/refresh_provider_rates.py" in fetch
        assert '|| rc=$?' in fetch
        assert 'echo "rc=$rc" >> "$GITHUB_OUTPUT"' in fetch
        moved = steps["moved"]["run"]
        assert "git diff --quiet -- src/pricing.json backend/constants.py" in moved
        for value in ["true", "false"]:
            assert f'echo "moved={value}" >> "$GITHUB_OUTPUT"' in moved

    def test_push_steps_and_script_are_preserved(self) -> None:
        steps = self.jobs["push"]["steps"]
        assert [step["name"] for step in steps] == [
            "Download the tested data", "Push the tested tree to master"
        ]
        run = steps[1]["run"]
        assert 'if [ -z "${MASTER_PUSH_DEPLOY_KEY:-}" ]; then' in run
        assert 'fetched="$(git ls-remote "$remote" refs/heads/master | cut -f1)"' in run
        assert 'if [ "$fetched" != "$base" ]; then' in run
        assert 'push origin HEAD:master' in run


class TestPerturbedSuiteExploreShards:
    def setup_method(self) -> None:
        self.jobs = yaml.safe_load(
            (WORKFLOWS_DIR / "test-data-explore.yml").read_text(encoding="utf-8")
        )["jobs"]
        self.job = self.jobs["perturbed-suite-explore"]
        self.step = next(step for step in self.job["steps"]
                         if step.get("name") == "Perturb and test, per wall-clock seed")

    def test_seeds_job_exposes_one_wall_clock_draw_to_all_cells(self) -> None:
        seeds = self.jobs["seeds"]
        assert self.job["needs"] == "seeds"
        assert seeds["runs-on"] == "ubuntu-latest"
        assert seeds["timeout-minutes"] == 5
        assert seeds["outputs"] == {"seed-base": "${{ steps.draw.outputs.seed_base }}"}
        assert seeds["steps"] == [{
            "id": "draw",
            "run": 'echo "seed_base=$(date +%s)" >> "$GITHUB_OUTPUT"',
        }]

    def test_three_seeds_run_four_shards_without_cancellation(self) -> None:
        assert self.job["strategy"] == {
            "fail-fast": False,
            "matrix": {"seed-index": [1, 2, 3], "shard": [1, 2, 3, 4]},
        }
        assert self.job["name"] == (
            "perturbed suite (explorer) seed ${{ matrix.seed-index }} "
            "shard ${{ matrix.shard }}"
        )

    def test_cell_timeout_is_thirty_minutes(self) -> None:
        assert self.job["timeout-minutes"] == 30

    def test_cell_perturbs_one_wall_clock_seed_and_runs_its_shard(self) -> None:
        run = self.step["run"]
        assert [line for line in run.splitlines() if line.startswith("SEED=")] == [
            'SEED="$(( ${{ needs.seeds.outputs.seed-base }} + ${{ matrix.seed-index }} ))"'
        ]
        assert "date +%s" not in run
        assert 'for ' not in run
        perturb = 'python scripts/ci/perturb_test_data.py --seed "$SEED"'
        restore = ("git checkout -- src/pricing.json backend/constants.py "
                   "backend/parser_version.py")
        normalized = " ".join(run.replace("\\\n", " ").split())
        assert normalized.index(restore) < normalized.index(perturb)
        assert (
            "# shellcheck disable=SC2046\n"
            "  python -m pytest $(python3 scripts/ci/pytest_shards.py "
            "--shard ${{ matrix.shard }} --of 4) -q --tb=short -ra\n}; then"
        ) in run
        assert self.step["env"]["CLAUDIT_PERTURBED_DATA"] == "1"

    def test_cell_reports_seed_and_shard_and_fails_nonzero(self) -> None:
        run = self.step["run"]
        assert 'echo "::notice::perturbed-suite seed=$SEED shard=${{ matrix.shard }}"' in run
        assert (
            'echo "seed=$SEED shard=${{ matrix.shard }} ok" >> "$GITHUB_STEP_SUMMARY"'
        ) in run
        assert (
            'else\n  echo "seed=$SEED shard=${{ matrix.shard }} failed" '
            '>> "$GITHUB_STEP_SUMMARY"\n  exit 1\nfi'
        ) in run

    def test_pip_cache_save_requires_first_shard(self) -> None:
        save = next(step for step in self.job["steps"]
                    if step.get("name") == "Save the pip cache")
        assert "&& matrix.shard == 1" in save["if"]
