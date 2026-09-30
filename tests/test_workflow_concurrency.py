"""Workflow shape: a master push's own runs cannot be cancelled by a newer push.

Three workflows still keyed their push-side concurrency group per ref after
PR #367 gave ci-gate itself per-SHA groups (issue #358), so an older commit's
run could still be cancelled or pending-superseded through them:

- ``version-guard.yml`` cancelled the older commit's in-flight guard run
  outright (`cancel-in-progress: true` on a per-ref group), stranding a
  cancelled check on that SHA — which release.yml's waiter refuses (issue
  #366);
- ``speed.yml`` and ``test-data.yml`` run the two longest gate legs, so with
  their per-ref push groups a third concurrent master push cancelled a
  merely-pending leg run — GitHub keeps at most one PENDING run per group,
  whatever `cancel-in-progress` says — and that middle commit's aggregate
  folded red (issue #409, observed on 1f0470e).

The invariant: on a push the group is the workflow name plus the pushed SHA,
so no two master pushes ever share a group. Pull requests keep the
per-number group; ``cancel-in-progress`` keeps cancelling only superseded
PR runs (and, for version-guard, never a push run, whose group is unique).
"""
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
# so the cancel only ever hits superseded PR runs); the two legs keep the
# PR-only expression. A push run's group is unique, so neither value can
# cancel or replace one.
EXPECTED_CANCEL = {
    "version-guard.yml": "true",
    "speed.yml": "${{ github.event_name == 'pull_request' }}",
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
