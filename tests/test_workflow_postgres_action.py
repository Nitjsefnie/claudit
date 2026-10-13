"""CI starts one digest-pinned, cached Postgres before database consumers."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / ".github/actions/postgres/action.yml"
LOCAL_ACTION = "$/.github/actions/postgres"
# The first database consumer in each job, including wrapper scripts.
SITES = (
    ("tests.yml", "pytest", "python -m pytest tests/"),
    ("test-data.yml", "perturbed-suite", "scripts/ci/perturbed_leg_bench.py"),
    ("test-data-explore.yml", "perturbed-suite-explore", "python -m pytest tests/"),
    ("smoke.yml", "smoke", "scripts/ci/smoke.py"),
    ("refresh-pricing.yml", "refresh", "python -m pytest tests/"),
)


def _load(path: Path) -> dict:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


@pytest.mark.parametrize("workflow,job_name,consumer", SITES)
def test_workflows_start_postgres_before_database_use(workflow, job_name, consumer):
    jobs = _load(ROOT / ".github/workflows" / workflow)["jobs"]
    assert all("services" not in job for job in jobs.values()), workflow
    steps = jobs[job_name]["steps"]
    starts = [i for i, step in enumerate(steps)
              if step.get("uses") == LOCAL_ACTION]
    assert len(starts) == 1, workflow
    consumers = [i for i, step in enumerate(steps)
                 if consumer in step.get("run", "")]
    assert consumers and starts[0] < min(consumers), workflow


def test_image_digest_matches_compose_and_keys_both_cache_operations():
    steps = _load(ACTION)["runs"]["steps"]
    image = next(step for step in steps if step.get("id") == "image")
    digest = image["env"]["POSTGRES_DIGEST"]
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
    compose_image = _load(ROOT / "compose.yaml")["services"]["db"]["image"]
    assert compose_image.rpartition("@")[2] == digest
    # The key is emitted from the same digest used by the pull, not a copy.
    acquire = next(step for step in steps if step.get('id') == 'acquire')
    assert 'docker pull "$ref"' in acquire['run']
    assert acquire['env']['POSTGRES_REF'] == '${{ steps.image.outputs.ref }}'
    assert '@${POSTGRES_DIGEST}' in image["run"]
    assert ('key=postgres-${RUNNER_OS}-${RUNNER_ARCH}-${POSTGRES_DIGEST}'
            in image["run"])
    caches = [step for step in steps
              if step.get("uses", "").startswith("actions/cache/")]
    assert {step["uses"].split("@")[0] for step in caches} == {
        "actions/cache/restore", "actions/cache/save"}
    for step in caches:
        assert step["with"]["key"] == "${{ steps.image.outputs.key }}"
        assert "restore-keys" not in step["with"]


def test_composite_external_actions_are_sha_pinned():
    uses = [step["uses"] for step in _load(ACTION)["runs"]["steps"]
            if "uses" in step and not step["uses"].startswith(("./", "$/"))]
    assert uses
    for ref in uses:
        assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", ref), ref
