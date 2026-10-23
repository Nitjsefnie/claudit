"""A workflow-run release must belong to the commit changing VERSION."""
from __future__ import annotations

import pytest

from test_workflow_release import _conjunction, _release, _run_step, _step_run, ubuntu_step_body

TARGET = "Verify this commit changed VERSION"
TARGET_GUARD = "steps.version.outputs.prerelease == '' && github.event_name == 'workflow_run'"
PARENT_STUB = r'''
gh() {
  case "$*" in
    "api repos/$CLAUDIT_REPO/commits/$CLAUDIT_SHA --jq .parents[0].sha")
      printf '%s\n' "$PARENT"; return "${PARENT_RC:-0}" ;;
    "api -H Accept: application/vnd.github.raw repos/$CLAUDIT_REPO/contents/VERSION?ref=$PARENT")
      printf '%s\n' "$PARENT_VERSION"; return "${VERSION_RC:-0}" ;;
    *) echo "unmodelled gh call: $*" >&2; return 99 ;;
  esac
}
'''


def test_target_position_guard_and_environment():
    steps = _release()["jobs"]["release"]["steps"]
    target, = [step for step in steps if step.get("name") == TARGET]
    index = steps.index(target)
    assert steps[index - 1]["name"] == "Read and validate VERSION"
    assert steps[index + 1]["name"] == "Refuse to re-release an existing tag"
    assert target["id"] == "target"
    assert target["if"] == TARGET_GUARD
    assert target["env"] == {
        "GH_TOKEN": "${{ github.token }}", "CLAUDIT_REPO": "${{ github.repository }}",
        "CLAUDIT_SHA": "${{ inputs.sha || github.event.workflow_run.head_sha || github.sha }}",
        "CLAUDIT_VERSION": "${{ steps.version.outputs.version }}",
    }
    for step in steps[index + 1:]:
        assert step["if"] == (
            "steps.version.outputs.prerelease == '' && steps.target.outputs.unchanged != 'true'")


@pytest.mark.parametrize("event,prerelease,expected", [
    ("workflow_run", "", True), ("workflow_run", "dev", False),
    ("workflow_dispatch", "", False), ("workflow_dispatch", "dev", False),
])
def test_target_guard_cases(event, prerelease, expected):
    target, = [step for step in _release()["jobs"]["release"]["steps"]
               if step.get("name") == TARGET]
    assert _conjunction(target["if"], {
        "github.event_name": event, "steps.version.outputs.prerelease": prerelease,
    }) is expected


@pytest.mark.parametrize("prerelease,unchanged,expected", [
    ("", "true", False), ("", "", True), ("dev", "", False),
])
def test_later_steps_skip_unchanged_version_and_admit_dispatch(prerelease, unchanged, expected):
    steps = _release()["jobs"]["release"]["steps"]
    for step in steps:
        if step.get("name") in {
                "Refuse to re-release an existing tag", "Wait for the other gates on this commit",
                "Create the release"}:
            assert _conjunction(step["if"], {
                "steps.version.outputs.prerelease": prerelease,
                "steps.target.outputs.unchanged": unchanged,
            }) is expected


@ubuntu_step_body
@pytest.mark.parametrize("parent_version,unchanged", [
    ("9.9.8-dev", False), (" \t9.9.9\n ", True),
])
def test_target_compares_parent_version(tmp_path, parent_version, unchanged):
    output = tmp_path / "output"
    proc = _run_step(_step_run(TARGET), PARENT_STUB, {
        "PARENT": "parent-head", "PARENT_VERSION": parent_version,
        "CLAUDIT_VERSION": " \t9.9.9\n ", "GITHUB_OUTPUT": str(output),
    })
    assert proc.returncode == 0, proc.stderr
    assert not proc.stderr
    if unchanged:
        assert proc.stdout == "::notice::this commit did not change VERSION — nothing to release from it\n"
        assert output.read_text(encoding="utf-8") == "unchanged=true\n"
    else:
        assert not proc.stdout
        assert not output.exists()


@ubuntu_step_body
@pytest.mark.parametrize("overrides", [
    {"VERSION_RC": "1", "PARENT_VERSION": "HTTP 404"},
    {"VERSION_RC": "1", "PARENT_VERSION": "HTTP 500"},
    {"PARENT_RC": "1"}, {"PARENT": ""}, {"PARENT": "null"},
])
def test_target_fails_closed_when_parent_is_unverifiable(tmp_path, overrides):
    output = tmp_path / "output"
    proc = _run_step(_step_run(TARGET), PARENT_STUB, {
        "PARENT": "parent-head", "PARENT_VERSION": "9.9.8-dev",
        "CLAUDIT_VERSION": "9.9.9", "GITHUB_OUTPUT": str(output), **overrides,
    })
    assert proc.returncode == 1
    assert "could not verify" in proc.stderr
    assert "failing closed" in proc.stderr
    assert not output.exists()
