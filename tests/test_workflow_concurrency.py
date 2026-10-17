"""A master push owns a per-SHA concurrency group, so a newer push cannot
cancel or supersede its gate runs. GitHub retains only one pending run per
group even with cancel-in-progress false. PR groups remain per number so
superseded PR runs can cancel; the tests canary deliberately serializes
master runs through its queue."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

# The per-SHA push-side group suffix every covered workflow must fold to.
PER_SHA_SUFFIX = (
    "${{ github.event.pull_request.number "
    "|| (github.event_name == 'push' && github.sha) || github.ref }}"
)

# workflow file -> expected `cancel-in-progress`: version-guard stays an
# unconditional true (its per-SHA push groups make that a no-op on pushes,
# so the cancel only ever hits superseded PR runs); the remaining leg keeps
# the PR-only expression. A push run's group is unique, so neither value can
# cancel or replace one.
EXPECTED_CANCEL = {
    "version-guard.yml": "true",
    "test-data.yml": "${{ github.event_name == 'pull_request' }}",
}


def _load(path):
    # BaseLoader, not safe_load: YAML 1.1 parses the bare key `on` as the
    # boolean True, and these tests read decoded scalars.
    return yaml.load(path.read_text(encoding="utf-8"),
                     Loader=yaml.BaseLoader) or {}


@pytest.mark.parametrize("workflow", sorted(EXPECTED_CANCEL))
def test_master_push_runs_get_a_per_sha_concurrency_group(workflow):
    # Exact equality on the decoded scalar: the concurrency semantics are
    # GitHub's, so the shape is the observable consequence here — but the
    # DECODED value, not the spelling in the file.
    doc = _load(WORKFLOWS / workflow)
    concurrency = doc["concurrency"]
    group = concurrency["group"]
    prefix = f"{Path(workflow).stem}-"
    assert group.startswith(prefix), group
    assert group.removeprefix(prefix) == PER_SHA_SUFFIX, group
    assert concurrency["cancel-in-progress"] == EXPECTED_CANCEL[workflow], (
        concurrency["cancel-in-progress"])
