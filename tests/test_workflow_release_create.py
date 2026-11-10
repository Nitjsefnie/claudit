"""release.yml's create step ends in exactly one release, or fails without announcing one.

The step body runs with `timeout` replaced by the stand-in that can end an
attempt as an expired one ends, and `gh` by one that models the create and
the read of the release for the tag and fails on any other call."""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_workflow_release import _run_step, _step_run, ubuntu_step_body
from tests.workflow_checks import bounded_calls

SHA = "0" * 40
OTHER = "1" * 40
RELEASED = f"Released v9.9.9 at {SHA}"
# READ: the release's target_commitish, or which failure the read answers with.
STUB = r'''
gh() {
  case "$*" in
    "release create $CLAUDIT_TAG --repo $CLAUDIT_REPO --target $CLAUDIT_SHA --title $CLAUDIT_TAG --generate-notes")
      echo create >> "$GH_CALLS"
      if [ "$CREATE" = fails ]; then echo "gh: HTTP 502: Bad Gateway" >&2; return 1; fi ;;
    "api repos/$CLAUDIT_REPO/releases/tags/$CLAUDIT_TAG --jq .target_commitish")
      echo read >> "$GH_CALLS"
      case "$READ" in
        absent) echo "gh: HTTP 404: Not Found" >&2; return 1 ;;
        unanswerable) echo "gh: HTTP 503: Service Unavailable" >&2; return 1 ;;
        *) echo "$READ" ;;
      esac ;;
    *) echo "unmodelled gh call: $*" >&2; return 99 ;;
  esac
}
sleep() { echo "sleep $*" >> "$GH_CALLS"; }
export -f gh sleep
'''


def _create(tmp_path: Path, read: str, create: str = "answers", ended: int = 0,
            only: str = "release create", status: str = "124"):
    """Run the step; `ended` first attempts of each bounded command holding `only` end with `status`.

    Returns the process, every bounded command in order as create/read, what
    reached gh (and each pause), and the step summary."""
    state, calls, summary = tmp_path / "timeout-state", tmp_path / "gh-calls", tmp_path / "summary"
    state.mkdir()
    calls.touch()
    body = _step_run("Create the release").replace("${{ github.repository }}", "Nitjsefnie/claudit")
    assert "${{" not in body
    proc = _run_step(body, STUB, {
        "RUNNER_TEMP": str(tmp_path), "GITHUB_STEP_SUMMARY": str(summary), "GH_CALLS": str(calls),
        "CREATE": create, "READ": read, "TIMEOUT_STATE": str(state), "TIMEOUT_EXPIRE": str(ended),
        "TIMEOUT_ONLY": only, "TIMEOUT_STATUS": status})
    bounded = bounded_calls(state) if (state / "calls").exists() else []
    assert all(seconds > 0 for seconds, _ in bounded), bounded
    kinds = ["create" if command.startswith("gh release create ") else "read" for _, command in bounded]
    return (proc, kinds, calls.read_text(encoding="utf-8").splitlines(),
            summary.read_text(encoding="utf-8") if summary.exists() else "")


def _announced_once(proc, summary: str) -> None:
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.count(RELEASED) == 1, proc.stdout
    assert summary.count("### Released") == 1 and summary.count(f"Commit `{SHA}`") == 1, summary


def _nothing_announced(proc, summary: str) -> None:
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "Released" not in proc.stdout + summary, proc.stdout + summary


def _exits(proc, *statuses: str) -> None:
    """The step logged each failed create's exit status, in attempt order, and nothing else of the kind."""
    lines = [line for line in proc.stderr.splitlines() if line.startswith("gh release create exited ")]
    assert lines == [f"gh release create exited {status} on attempt {number} of 3 (124 is timeout's status for its time limit, 137 for a KILL signal)"
                     for number, status in enumerate(statuses, 1)], proc.stderr


@ubuntu_step_body
def test_a_create_that_answers_is_one_create_and_no_read(tmp_path):
    proc, kinds, reached, summary = _create(tmp_path, read="unanswerable")
    _announced_once(proc, summary)
    assert kinds == ["create"] and reached == ["create"]
    _exits(proc)


@ubuntu_step_body
@pytest.mark.parametrize("status", ["124", "137"])
def test_a_create_the_bound_ended_that_landed_is_not_repeated(tmp_path, status):
    proc, kinds, reached, summary = _create(tmp_path, read=SHA, ended=1, status=status)
    _announced_once(proc, summary)
    assert kinds == ["create", "read"] and reached == ["read"]
    _exits(proc, status)
    assert f"release v9.9.9 exists at {SHA} although its create exited {status}" in proc.stdout


@ubuntu_step_body
def test_a_create_that_errored_but_landed_is_not_repeated(tmp_path):
    proc, kinds, reached, summary = _create(tmp_path, read=SHA, create="fails")
    _announced_once(proc, summary)
    assert kinds == ["create", "read"] and reached == ["create", "read"]
    _exits(proc, "1")
    assert f"release v9.9.9 exists at {SHA} although its create exited 1" in proc.stdout


@ubuntu_step_body
def test_a_create_the_bound_ended_that_never_landed_is_created_again(tmp_path):
    proc, kinds, reached, summary = _create(tmp_path, read="absent", ended=1)
    _announced_once(proc, summary)
    # The helper asks the absent release's read three times before the second create.
    assert kinds == ["create", "read", "read", "read", "create"]
    assert reached == ["read", "sleep 5", "read", "sleep 10", "read", "sleep 5", "create"]
    _exits(proc, "124")


@ubuntu_step_body
@pytest.mark.parametrize("create,ended,status", [("answers", 3, "137"), ("fails", 0, "1")], ids=["ended", "errored"])
def test_creates_that_never_land_stop_after_three_and_announce_nothing(tmp_path, create, ended, status):
    proc, kinds, _, summary = _create(tmp_path, read="absent", create=create, ended=ended, status=status)
    _nothing_announced(proc, summary)
    assert kinds == ["create", "read", "read", "read"] * 3
    _exits(proc, status, status, status)
    assert "failed after 3 attempts" in proc.stderr


@ubuntu_step_body
@pytest.mark.parametrize("read,only,said", [
    ("unanswerable", "release create", "gh: HTTP 503: Service Unavailable"),
    (SHA, "", "still failing after 3 attempts"),
], ids=["a 503", "every read ended by the bound"])
def test_a_read_back_without_an_answer_fails_closed(tmp_path, read, only, said):
    proc, kinds, _, summary = _create(tmp_path, read=read, ended=3, only=only)
    _nothing_announced(proc, summary)
    assert kinds == ["create", "read", "read", "read"]
    assert said in proc.stderr, "what the read answered is missing from the log"
    _exits(proc, "124")
    assert "could not verify the release read-back for v9.9.9 — failing closed" in proc.stderr


@ubuntu_step_body
@pytest.mark.parametrize("target", [OTHER, "master", SHA[:-1], SHA + "0"])
def test_a_release_at_another_target_fails_closed(tmp_path, target):
    proc, kinds, _, summary = _create(tmp_path, read=target, ended=1)
    _nothing_announced(proc, summary)
    assert kinds == ["create", "read"]
    _exits(proc, "124")
    assert f"release v9.9.9 exists at '{target}', not at {SHA} — failing closed" in proc.stderr
