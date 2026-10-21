"""CI starts one digest-pinned, cached Postgres before database consumers."""
from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTIONS = ROOT / ".github/actions"
ACTION = ACTIONS / "postgres/action.yml"
IMAGE_ACTION = ACTIONS / "postgres-image/action.yml"
CACHED_ACTION = ACTIONS / "cached-image/action.yml"
LOCAL_ACTION = "$/.github/actions/postgres"
# The first database consumer in each job, including wrapper scripts.
SITES = (
    ("tests.yml", "pytest-shards", "python -m pytest $(python3 scripts/ci/pytest_shards.py"),
    ("tests.yml", "pytest-thresholds", "python -m pytest $(sed"),
    ("test-data.yml", "perturbed-suite", "scripts/ci/perturbed_leg_bench.py"),
    ("test-data-explore.yml", "perturbed-suite-explore", "python -m pytest $(python3 scripts/ci/pytest_shards.py"),
    ("smoke.yml", "smoke", "scripts/ci/smoke.py"),
    ("refresh-pricing.yml", "suite", "python -m pytest $(python3 scripts/ci/pytest_shards.py"),
)


def _load(path: Path) -> dict:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def _step(path: Path, **match: str) -> dict:
    steps = _load(path)["runs"]["steps"]
    found = [step for step in steps
             if all(step.get(k) == v for k, v in match.items())]
    assert len(found) == 1, (path.name, match)
    return found[0]


def _cached_step() -> dict:
    """The Postgres image's call into the shared cache mechanism."""
    step = _step(IMAGE_ACTION, uses="$/.github/actions/cached-image")
    assert "if" not in step and "continue-on-error" not in step
    return step


@pytest.mark.parametrize("workflow,job_name,consumer", SITES)
def test_workflows_start_postgres_before_database_use(workflow, job_name, consumer):
    jobs = _load(ROOT / ".github/workflows" / workflow)["jobs"]
    assert all("services" not in job for job in jobs.values()), workflow
    steps = jobs[job_name]["steps"]
    starts = [i for i, step in enumerate(steps)
              if step.get("uses") == LOCAL_ACTION]
    assert len(starts) == 1, workflow
    assert "if" not in steps[starts[0]], workflow
    consumers = [i for i, step in enumerate(steps)
                 if consumer in step.get("run", "")]
    assert consumers and starts[0] < min(consumers), workflow


def test_js_coverage_keeps_postgres_for_transitive_db_fixtures():
    job = _load(ROOT / ".github/workflows/tests.yml")["jobs"]["js-coverage"]
    steps = job["steps"]
    start, = [i for i, step in enumerate(steps) if step.get("uses") == LOCAL_ACTION]
    gate, = [i for i, step in enumerate(steps) if step.get("id") == "jscov"]
    assert start < gate
    assert job["env"] == {
        "PGHOST": "localhost", "PGPORT": "5432",
        "PGUSER": "postgres", "PGPASSWORD": "postgres"}


def test_image_digest_matches_compose_and_keys_both_cache_operations():
    image = _step(IMAGE_ACTION, id="image")
    digest = image["env"]["POSTGRES_DIGEST"]
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
    compose_image = _load(ROOT / "compose.yaml")["services"]["db"]["image"]
    assert compose_image.rpartition("@")[2] == digest
    # The key is emitted from the same digest used by the pull, not a copy.
    cached = _cached_step()["with"]
    assert cached["ref"] == "${{ steps.image.outputs.ref }}"
    assert cached["key"] == "${{ steps.image.outputs.key }}"
    acquire = _step(CACHED_ACTION, id="acquire")
    assert 'docker pull "$ref"' in acquire["run"]
    assert acquire["env"]["IMAGE_REF"] == "${{ inputs.ref }}"
    assert '@${POSTGRES_DIGEST}' in image["run"]
    assert ('key=postgres-${RUNNER_OS}-${RUNNER_ARCH}-${POSTGRES_DIGEST}'
            in image["run"])
    caches = [step for step in _load(CACHED_ACTION)["runs"]["steps"]
              if step.get("uses", "").startswith("actions/cache/")]
    for step in caches:
        assert step["with"]["key"] == "${{ inputs.key }}"
        assert step["with"]["path"] == "${{ runner.temp }}/${{ inputs.archive }}"
        assert "restore-keys" not in step["with"]


def test_cache_save_is_gated_on_acquisition_output():
    restore, acquire, save = _load(CACHED_ACTION)["runs"]["steps"]
    assert restore["id"] == "cache" and "if" not in restore
    assert acquire["env"]["CACHE_HIT"] == "${{ steps.cache.outputs.cache-hit }}"
    assert "if" not in acquire and "continue-on-error" not in acquire
    # Bash behavior is exercised in test_workflow_cached_image; these pins
    # cover GitHub's separate cache action and its output/condition wiring.
    assert save.get("if") == "steps.acquire.outputs.pulled == 'true'"
    assert save.get("continue-on-error") == "true"


def test_composite_external_actions_are_sha_pinned():
    uses = [step["uses"]
            for path in (ACTION, IMAGE_ACTION, CACHED_ACTION)
            for step in _load(path)["runs"]["steps"]
            if "uses" in step and not step["uses"].startswith(("./", "$/"))]
    assert uses
    for ref in uses:
        assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", ref), ref


def _docker_commands(run: str, verb: str) -> list[list[str]]:
    return [shlex.split(line) for line in run.replace("\\\n", " ").splitlines()
            if line.strip().startswith(f"docker {verb} ")]


def test_start_uses_the_saved_local_tag_without_a_pull():
    steps = _load(ACTION)["runs"]["steps"]
    image = steps[0]
    assert image.get("id") == "image" and "if" not in image
    start = steps[-1]
    assert "if" not in start
    commands = _docker_commands(start["run"], "run")
    assert len(commands) == 1
    args = commands[0]
    policies = [args[i + 1] for i, arg in enumerate(args) if arg == "--pull"]
    policies += [arg.split("=", 1)[1] for arg in args if arg.startswith("--pull=")]
    assert policies == ["never"]
    assert not _docker_commands(start["run"], "pull")
    assert args[-1] == "$POSTGRES_TAG"
    assert start["env"]["POSTGRES_TAG"] == "${{ steps.image.outputs.tag }}"
    outputs = _load(IMAGE_ACTION)["outputs"]
    assert outputs["tag"]["value"] == "${{ steps.image.outputs.tag }}"
    acquire = _step(CACHED_ACTION, id="acquire")
    assert acquire["env"]["IMAGE_TAG"] == "${{ inputs.tag }}"
    assert _docker_commands(acquire["run"], "tag") == [
        ["docker", "tag", "$ref", "$IMAGE_TAG"]]
    assert _docker_commands(acquire["run"], "save") == [
        ["docker", "save", "--output", "$archive", "$IMAGE_TAG"]]
    assert _docker_commands(acquire["run"], "load") == [
        ["docker", "load", "--input", "$archive"]]


def test_fallback_uses_the_primary_digest_and_is_tried_second():
    image = _step(IMAGE_ACTION, id="image")
    acquire = _step(CACHED_ACTION, id="acquire")
    assert image["env"].get("POSTGRES_FALLBACK_REPOSITORY") == (
        "mirror.gcr.io/library/postgres")
    outputs = dict(shlex.split(line)[1].split("=", 1)
                   for line in image["run"].splitlines()
                   if line.strip().startswith("echo "))
    assert outputs["ref"] == "${POSTGRES_REPOSITORY}@${POSTGRES_DIGEST}"
    assert outputs.get("fallback") == (
        "${POSTGRES_FALLBACK_REPOSITORY}@${POSTGRES_DIGEST}")
    assert acquire["env"].get("IMAGE_FALLBACK_REF") == (
        "${{ inputs.fallback-ref }}")
    assert ('for ref in "$IMAGE_REF" "$IMAGE_FALLBACK_REF"; do'
            in acquire["run"])


def test_postgres_failure_message_is_unchanged():
    """The label threads into the shared error text, word for word."""
    acquire = _step(CACHED_ACTION, id="acquire")
    assert acquire["env"]["IMAGE_LABEL"] == "${{ inputs.label }}"
    assert ('echo "::error::${IMAGE_LABEL} image cache had no entry; both '
            'registries failed after three attempts each. Registry answers '
            'follow:" >&2') in acquire["run"]


def test_postgres_publishes_the_input_port_and_waits_for_tcp_readiness():
    action = _load(ACTION)
    start = action["runs"]["steps"][-1]
    assert action["inputs"]["port"]["default"] == "5432"
    assert start["env"]["POSTGRES_PORT"] == "${{ inputs.port }}"
    args, = _docker_commands(start["run"], "run")
    ports = [args[i + 1] for i, arg in enumerate(args) if arg in ("--publish", "-p")]
    ports += [arg.split("=", 1)[1] for arg in args if arg.startswith("--publish=")]
    assert ports == ["127.0.0.1:${POSTGRES_PORT}:5432"]
    probes = re.findall(r"(?m)^ *if (docker exec[^\n]*); then$", start["run"])
    assert [shlex.split(probe) for probe in probes] == [[
        "docker", "exec", "$POSTGRES_CONTAINER", "pg_isready", "-q",
        "-h", "127.0.0.1", "-U", "postgres", "-d", "postgres"]]
