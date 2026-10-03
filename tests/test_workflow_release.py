"""Workflow shape: release.yml waits for a real verdict before it tags.

The "Wait for the other gates on this commit" step polls the commit's
check runs and, on the face of it, requires every gate it judges to
have completed success/skipped/neutral. WHICH rows it judges is
scripts/ci/release_gate.py's subject and is pinned in
tests/test_release_gate.py (issues #557 and #579); what these tests pin
is the step around it:

- the wait proceeds ONLY when a check run named exactly `aggregate`,
  completed with conclusion success and produced by the github-actions
  app, is among the judged rows of a single check-runs read; absent,
  queued, not yet completed, conclusion `skipped`, or a foreign app
  merely named `aggregate` keeps it polling until the deadline exits 1;
- the wait keeps its existing refusals: it judges only the gates the
  selector keeps, refuses immediately when a judged check completed
  failure/cancelled/timed_out, and cannot race ahead when only its own
  job exists;
- its deadline and poll are env-seamed (RELEASE_WAIT_SECONDS /
  RELEASE_WAIT_POLL_SECONDS; production defaults 2700 s / 20 s) so
  tests can shorten the wait, and its two reads carry the selector the
  app slug of the aggregate rule is projected from.

The "Refuse to re-release an existing tag" step is pinned the same way
(issue #248): it treats ONLY an HTTP 404 as "absent" and fails closed
on every other probe failure — a 403, a 5xx or a network error must
never read as "the version is free" — and both probes go through
`gh api` with their output captured, nothing discarded.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
RELEASE = WORKFLOWS / "release.yml"

ubuntu_step_body = pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="the tests execute release.yml ubuntu run blocks through POSIX bash, "
           "which Windows cannot run; the steps only ever execute on "
           "ubuntu-latest",
)

WAIT_STEP = "Wait for the other gates on this commit"
REFUSAL_STEP = "Refuse to re-release an existing tag"
PROCEEDING = "Every check passed — proceeding."


def _load(path):
    # BaseLoader, not safe_load: YAML 1.1 parses the bare key `on` as the
    # boolean True, and these tests read the trigger map by name.
    return yaml.load(path.read_text(encoding="utf-8"),
                     Loader=yaml.BaseLoader) or {}


def _release():
    return _load(RELEASE)


def _step_run(step_name: str) -> str:
    for job in (_release()["jobs"] or {}).values():
        for step in (job or {}).get("steps") or []:
            if step.get("name") == step_name:
                assert "run" in step, step_name
                return step["run"]
    raise AssertionError(f"no step named {step_name!r} in release.yml")


def _run_step(body: str, stub: str, env: dict[str, str],
              timeout: float = 60) -> subprocess.CompletedProcess:
    # GitHub Actions runs a run-body under `bash -e`; the stub is a shell
    # function defined ahead of the body in the same command string, so
    # the body's own `gh` calls resolve to it.
    full_env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ.get("HOME", ""),
        "TAG": "v9.9.9",
        "REPO": "Nitjsefnie/claudit",
        "SHA": "0" * 40,
        "GH_TOKEN": "stubbed",
        "SELF_JOB": "release",
        "SELF_RUN_ID": SELF_RUN,
        "RUNNER_TEMP": os.environ.get("RUNNER_TEMP", ""),
    }
    full_env.update(env)
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c",
         f"{stub}\n{body}"],
        env=full_env, capture_output=True, timeout=timeout, check=False,
        encoding="utf-8", errors="replace",
    )


# The push-triggered ci-gate run the selector treats as a gate of the
# commit, its workflow-run listing, and the waiter's own run.
PUSH_RUN = "37138486204"
SELF_RUN = "90000000001"


def _row(status: str, conclusion: str, name: str,
         app: str = "github-actions", run_id: str = PUSH_RUN) -> dict:
    # One check run, as the commit check-runs endpoint returns it.
    return {"name": name, "status": status, "conclusion": conclusion,
            "app": {"slug": app},
            "html_url": (f"https://github.com/Nitjsefnie/claudit/actions/runs/"
                         f"{run_id}/job/1")}


def _run(run_id: str, event: str = "push",
         head_sha: str = "0" * 40,
         path: str = ".github/workflows/ci-gate.yml") -> dict:
    return {"id": int(run_id), "event": event, "head_sha": head_sha,
            "path": path}


def _checks(*rows: dict) -> str:
    return json.dumps({"check_runs": list(rows)})


def _runs(*runs: dict) -> str:
    return json.dumps({"workflow_runs": list(runs)})


# Emulates the wait step's two reads: the commit's check runs and the
# workflow runs stamped with that SHA. Both arrive via the environment,
# and the REAL selector runs over them — the stub bypasses no logic.
WAIT_STUB = (
    'gh() { case "$*" in '
    '*check-runs*) printf "%s" "$STUB_CHECKS" ;; '
    '*) printf "%s" "$STUB_RUNS" ;; '
    'esac; }'
)

# Prints one set of check runs on the first check-runs read, another on
# every later one, counting reads in a file so polling is observable.
COUNTING_STUB = (
    'gh() { case "$*" in '
    '*check-runs*) '
    'local n; '
    'n="$(cat "$STUB_CALLFILE" 2>/dev/null || echo 0)"; '
    'n=$((n + 1)); '
    'printf "%s" "$n" > "$STUB_CALLFILE"; '
    'if [ "$n" -eq 1 ]; then printf "%s" "$STUB_CHECKS_FIRST"; '
    'else printf "%s" "$STUB_CHECKS_LATER"; fi ;; '
    '*) printf "%s" "$STUB_RUNS" ;; '
    'esac; }'
)


def _short_wait() -> dict[str, str]:
    return {"RELEASE_WAIT_SECONDS": "10", "RELEASE_WAIT_POLL_SECONDS": "1"}


# The refusal step calls gh twice — the tag probe, then the release
# probe — so the stub answers by CALL ORDER: the first call speaks for
# the tag probe (STUB_FIRST_*), every later call for the release probe
# (STUB_LATER_*). The output is what `2>&1` would capture, the return
# code what the probe would exit with.
REFUSAL_STUB = (
    'gh() { '
    'local n out rc; '
    'n="$(cat "$STUB_CALLFILE" 2>/dev/null || echo 0)"; '
    'n=$((n + 1)); '
    'printf "%s" "$n" > "$STUB_CALLFILE"; '
    'if [ "$n" -eq 1 ]; then out="$STUB_FIRST_OUT"; rc="$STUB_FIRST_RC"; '
    'else out="$STUB_LATER_OUT"; rc="$STUB_LATER_RC"; fi; '
    'printf "%s\\n" "$out"; return "$rc"; }'
)


def _refusal_env(first_rc: int, first_out: str,
                 later_rc: int, later_out: str,
                 tmp_path) -> dict[str, str]:
    return {
        "STUB_CALLFILE": str(Path(tmp_path) / "gh-calls"),
        "STUB_FIRST_RC": str(first_rc),
        "STUB_FIRST_OUT": first_out,
        "STUB_LATER_RC": str(later_rc),
        "STUB_LATER_OUT": later_out,
    }


TAG_404 = "gh: HTTP 404: Not Found (https://api.github.com/repos/Nitjsefnie/claudit/git/ref/tags/v9.9.9)"
RELEASE_404 = "gh: HTTP 404: Not Found (https://api.github.com/repos/Nitjsefnie/claudit/releases/tags/v9.9.9)"


@ubuntu_step_body
def test_wait_proceeds_when_aggregate_success_among_all_success(tmp_path):
    checks = _checks(_row("completed", "success", "tests"),
                     _row("completed", "success", "lint"),
                     _row("completed", "success", "aggregate"))
    proc = _run_step(_step_run(WAIT_STEP), WAIT_STUB,
                     _short_wait() | {"STUB_CHECKS": checks,
                                      "STUB_RUNS": _runs(_run(PUSH_RUN)),
                                      "RUNNER_TEMP": str(tmp_path)})
    assert proc.returncode == 0, proc.stderr
    assert PROCEEDING in proc.stdout


@ubuntu_step_body
def test_wait_times_out_without_an_aggregate_verdict(tmp_path):
    # A master push whose gate never reported a verdict — the exact hole
    # of issue #247: everything green, no aggregate row at all.
    checks = _checks(_row("completed", "success", "version-guard",
                          run_id="37138486016"))
    proc = _run_step(_step_run(WAIT_STEP), WAIT_STUB,
                     _short_wait() | {"STUB_CHECKS": checks,
                                      "STUB_RUNS": _runs(_run(PUSH_RUN)),
                                      "RUNNER_TEMP": str(tmp_path)})
    assert proc.returncode == 1
    assert PROCEEDING not in proc.stdout
    assert "Timed out" in proc.stderr


@ubuntu_step_body
def test_wait_times_out_when_the_aggregate_conclusion_is_skipped(tmp_path):
    # `skipped` sits inside the wait's allowed set — without the verdict
    # rule the wait proceeds over an aggregate that skipped.
    checks = _checks(_row("completed", "success", "version-guard",
                          run_id="37138486016"),
                     _row("completed", "skipped", "aggregate"))
    proc = _run_step(_step_run(WAIT_STEP), WAIT_STUB,
                     _short_wait() | {"STUB_CHECKS": checks,
                                      "STUB_RUNS": _runs(_run(PUSH_RUN)),
                                      "RUNNER_TEMP": str(tmp_path)})
    assert proc.returncode == 1
    assert PROCEEDING not in proc.stdout
    assert "Timed out" in proc.stderr


@ubuntu_step_body
def test_wait_times_out_when_the_aggregate_is_a_foreign_app(tmp_path):
    # A check merely NAMED aggregate from another app proves nothing
    # about our gate; only the Actions app's own verdict counts.
    checks = _checks(_row("completed", "success", "version-guard",
                          run_id="37138486016"),
                     _row("completed", "success", "aggregate",
                          app="acme-ci"),
                     _row("completed", "success", "lint"))
    proc = _run_step(_step_run(WAIT_STEP), WAIT_STUB,
                     _short_wait() | {"STUB_CHECKS": checks,
                                      "STUB_RUNS": _runs(_run(PUSH_RUN)),
                                      "RUNNER_TEMP": str(tmp_path)})
    assert proc.returncode == 1
    assert PROCEEDING not in proc.stdout


@ubuntu_step_body
def test_wait_proceeds_once_the_aggregate_reports(tmp_path):
    # The first read shows the aggregate queued; a later read completes
    # it with success and the wait proceeds.
    first = _checks(_row("completed", "success", "version-guard",
                         run_id="37138486016"),
                    _row("queued", "-", "aggregate"))
    later = _checks(_row("completed", "success", "version-guard",
                         run_id="37138486016"),
                    _row("completed", "success", "aggregate"))
    env = _short_wait() | {
        "STUB_CALLFILE": str(tmp_path / "gh-calls"),
        "STUB_CHECKS_FIRST": first,
        "STUB_CHECKS_LATER": later,
        "STUB_RUNS": _runs(_run(PUSH_RUN)),
        "RUNNER_TEMP": str(tmp_path),
    }
    proc = _run_step(_step_run(WAIT_STEP), COUNTING_STUB, env)
    assert proc.returncode == 0, proc.stderr
    assert PROCEEDING in proc.stdout


@ubuntu_step_body
def test_wait_refuses_promptly_when_a_check_failed(tmp_path):
    # A failed non-aggregate check refuses on the first read — no wait.
    checks = _checks(_row("completed", "failure", "tests"),
                     _row("completed", "success", "aggregate"))
    proc = _run_step(_step_run(WAIT_STEP), WAIT_STUB,
                     {"STUB_CHECKS": checks, "STUB_RUNS": _runs(_run(PUSH_RUN)),
                      "RUNNER_TEMP": str(tmp_path)}, timeout=30)
    assert proc.returncode == 1
    assert "Refusing to release" in proc.stderr
    assert PROCEEDING not in proc.stdout


@ubuntu_step_body
def test_wait_refuses_promptly_when_a_check_was_cancelled(tmp_path):
    # `cancelled` counts as failure: a cancelled run means a newer push
    # superseded this SHA, which is not a commit to release.
    checks = _checks(_row("completed", "cancelled", "tests"),
                     _row("completed", "success", "aggregate"))
    proc = _run_step(_step_run(WAIT_STEP), WAIT_STUB,
                     {"STUB_CHECKS": checks, "STUB_RUNS": _runs(_run(PUSH_RUN)),
                      "RUNNER_TEMP": str(tmp_path)}, timeout=30)
    assert proc.returncode == 1
    assert "Refusing to release" in proc.stderr
    assert PROCEEDING not in proc.stdout


@ubuntu_step_body
def test_wait_refuses_on_a_red_gate_the_selector_keeps(tmp_path):
    # Issue #557 end to end: a PUSH-run gate whose check name begins
    # `release`. The old prefix filter dropped this row and released over
    # it; the selector keeps it, so the wait refuses.
    checks = _checks(_row("completed", "failure", "release-notes"),
                     _row("completed", "success", "aggregate"))
    proc = _run_step(_step_run(WAIT_STEP), WAIT_STUB,
                     {"STUB_CHECKS": checks, "STUB_RUNS": _runs(_run(PUSH_RUN)),
                      "RUNNER_TEMP": str(tmp_path)}, timeout=30)
    assert proc.returncode == 1
    assert "Refusing to release" in proc.stderr
    assert "release-notes" in proc.stderr


@ubuntu_step_body
def test_wait_ignores_a_red_claim_row_from_an_issue_comment_run(tmp_path):
    # Issue #579 end to end: the red `claim` row an `issue_comment` run
    # stamps on the default branch's tip. Skipped by the selector, so it
    # neither refuses the release nor waits on it.
    checks = _checks(_row("completed", "failure", "claim",
                          run_id="37138981898"),
                     _row("completed", "success", "aggregate"))
    runs = _runs(_run(PUSH_RUN),
                 _run("37138981898", event="issue_comment",
                      path=".github/workflows/claim.yml"))
    proc = _run_step(_step_run(WAIT_STEP), WAIT_STUB,
                     {"STUB_CHECKS": checks, "STUB_RUNS": runs,
                      "RUNNER_TEMP": str(tmp_path)}, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert PROCEEDING in proc.stdout
    assert "skipping check 'claim'" in proc.stderr


@ubuntu_step_body
def test_wait_times_out_when_only_its_own_job_exists(tmp_path):
    # The selector drops this job, so with nothing else present there is
    # nothing to wait for — and nothing to proceed on.
    checks = _checks(_row("completed", "success", "release",
                          run_id=SELF_RUN))
    runs = _runs(_run(PUSH_RUN), _run(SELF_RUN,
                                      path=".github/workflows/release.yml"))
    proc = _run_step(_step_run(WAIT_STEP), WAIT_STUB,
                     _short_wait() | {"STUB_CHECKS": checks,
                                      "STUB_RUNS": runs,
                                      "RUNNER_TEMP": str(tmp_path)})
    assert proc.returncode == 1
    assert PROCEEDING not in proc.stdout
    assert "Timed out" in proc.stderr


def test_the_wait_step_delegates_selection_to_the_selector():
    # The projection and its exclusion used to live in an inline --jq,
    # where no test could reach them (issues #557, #579 both hid there).
    body = _step_run(WAIT_STEP)
    assert "scripts/ci/release_gate.py" in body
    assert "--jq" not in body
    assert "startswith(\"release\")" not in body
    assert "actions/runs?head_sha=$SHA" in body
    assert '${{ github.job }}' in RELEASE.read_text(encoding="utf-8")
    assert '${{ github.run_id }}' in RELEASE.read_text(encoding="utf-8")


def test_the_selector_projection_carries_the_app_slug():
    # The aggregate rule reads the app column; without it in the
    # projection it would read a column that is not there. The projection
    # lives in the selector now, so it is pinned there.
    selector = (ROOT / "scripts" / "ci" / "release_gate.py").read_text(
        encoding="utf-8")
    assert 'check_run.get("app") or {}' in selector
    assert 'app.get("slug") or "-"' in selector


@ubuntu_step_body
def test_refusal_proceeds_when_both_probes_answer_404(tmp_path):
    # The one shape that may proceed: both probes proved absent.
    proc = _run_step(_step_run(REFUSAL_STEP), REFUSAL_STUB,
                     _refusal_env(1, TAG_404, 1, RELEASE_404, tmp_path))
    assert proc.returncode == 0, proc.stderr
    assert "v9.9.9 is free" in proc.stdout


@ubuntu_step_body
def test_refusal_refuses_when_the_tag_probe_succeeds(tmp_path):
    proc = _run_step(_step_run(REFUSAL_STEP), REFUSAL_STUB,
                     _refusal_env(0, '{"ref": "refs/tags/v9.9.9"}',
                                  1, "", tmp_path))
    assert proc.returncode == 1
    assert "Tag v9.9.9 already exists" in proc.stderr


@ubuntu_step_body
def test_refusal_refuses_when_the_release_probe_succeeds(tmp_path):
    proc = _run_step(_step_run(REFUSAL_STEP), REFUSAL_STUB,
                     _refusal_env(1, TAG_404, 0, '{"id": 12345}', tmp_path))
    assert proc.returncode == 1
    assert "Release v9.9.9 already exists" in proc.stderr


@ubuntu_step_body
def test_refusal_fails_closed_when_the_tag_probe_answers_403(tmp_path):
    # A 403 says nothing about the tag's existence; proceeding on it
    # would green-light a version collision the step exists to catch.
    proc = _run_step(_step_run(REFUSAL_STEP), REFUSAL_STUB,
                     _refusal_env(1, "gh: HTTP 403: Forbidden", 1, "", tmp_path))
    assert proc.returncode == 1
    assert "could not verify the tag probe for v9.9.9" in proc.stderr


@ubuntu_step_body
def test_refusal_fails_closed_when_the_tag_probe_answers_503(tmp_path):
    proc = _run_step(_step_run(REFUSAL_STEP), REFUSAL_STUB,
                     _refusal_env(1, "gh: HTTP 503: Service Unavailable",
                                  1, "", tmp_path))
    assert proc.returncode == 1
    assert "could not verify the tag probe for v9.9.9" in proc.stderr


@ubuntu_step_body
def test_refusal_fails_closed_when_the_tag_probe_hits_a_network_error(tmp_path):
    proc = _run_step(_step_run(REFUSAL_STEP), REFUSAL_STUB,
                     _refusal_env(1, 'gh: Get "https://api.github.com": '
                                  'dial tcp: connection refused',
                                  1, "", tmp_path))
    assert proc.returncode == 1
    assert "could not verify the tag probe for v9.9.9" in proc.stderr


@ubuntu_step_body
def test_refusal_fails_closed_when_the_release_probe_answers_403(tmp_path):
    proc = _run_step(_step_run(REFUSAL_STEP), REFUSAL_STUB,
                     _refusal_env(1, TAG_404, 1, "gh: HTTP 403: Forbidden",
                                  tmp_path))
    assert proc.returncode == 1
    assert "could not verify the release probe for v9.9.9" in proc.stderr


def test_refusal_probes_go_through_captured_gh_api():
    # The failure this pins (issue #248) lived in the discard: a probe
    # whose output is thrown away cannot be told apart from one that
    # never answered, so any nonzero exit read as "absent". Both probes
    # must call `gh api` and capture what came back.
    body = _step_run(REFUSAL_STEP)
    assert ">/dev/null 2>&1" not in body
    assert "gh release view" not in body
    assert body.count("gh api") == 2
    assert body.count("2>&1") == 2
