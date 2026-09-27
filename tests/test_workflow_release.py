"""Workflow shape: release.yml waits for a real verdict before it tags.

The "Wait for the other gates on this commit" step polls the commit's
check runs and, on the face of it, requires every non-`release` check
to have completed success/skipped/neutral. But every leg green is also
what an all-skipped gate looks like: without a verdict there is no
proof the gates ran at all (issue #247). The invariants pinned here:

- the wait proceeds ONLY when a check run named exactly `aggregate`,
  completed with conclusion success and produced by the github-actions
  app, is among the rows of a single check-runs read; absent, queued,
  not yet completed, conclusion `skipped`, or a foreign app merely
  named `aggregate` keeps it polling until the deadline exits 1;
- the wait keeps its existing refusals: it excludes its own `release`
  checks, refuses immediately when a non-release check completed
  failure/cancelled/timed_out, and cannot race ahead when only its own
  checks exist;
- its deadline and poll are env-seamed (RELEASE_WAIT_SECONDS /
  RELEASE_WAIT_POLL_SECONDS; production defaults 2700 s / 20 s) so
  tests can shorten the wait, and its --jq projection carries the app
  slug the aggregate rule reads.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
RELEASE = WORKFLOWS / "release.yml"

WAIT_STEP = "Wait for the other gates on this commit"
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
    }
    full_env.update(env)
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c",
         f"{stub}\n{body}"],
        env=full_env, capture_output=True, timeout=timeout, check=False,
        encoding="utf-8", errors="replace",
    )


def _row(status: str, conclusion: str, name: str,
         app: str = "github-actions") -> str:
    # One projected check-run row, as the step's --jq would print it:
    # status, conclusion, name, app slug — tab-separated.
    return f"{status}\t{conclusion}\t{name}\t{app}"


# Emulates the wait step's `gh api --jq`: applies the projection's own
# release-exclusion, then prints the ready-made tab-separated rows the
# real jq would have produced. The rows arrive via the environment.
WAIT_STUB = (
    "gh() { printf '%s\\n' \"$STUB_RUNS\" "
    "| awk -F'\\t' '$3 !~ /^release/'; }"
)

# Prints one set of rows on the first gh call, another on every later
# call, counting calls in a file so polling is observable.
COUNTING_STUB = (
    'gh() { '
    'local n; '
    'n="$(cat "$STUB_CALLFILE" 2>/dev/null || echo 0)"; '
    'n=$((n + 1)); '
    'printf "%s" "$n" > "$STUB_CALLFILE"; '
    'if [ "$n" -eq 1 ]; then '
    'printf "%s\\n" "$STUB_RUNS_FIRST"; '
    'else '
    'printf "%s\\n" "$STUB_RUNS_LATER"; '
    'fi; }'
)


def _short_wait() -> dict[str, str]:
    return {"RELEASE_WAIT_SECONDS": "2", "RELEASE_WAIT_POLL_SECONDS": "1"}


def test_wait_proceeds_when_aggregate_success_among_all_success():
    runs = "\n".join([
        _row("completed", "success", "tests"),
        _row("completed", "success", "lint"),
        _row("completed", "success", "aggregate"),
    ])
    proc = _run_step(_step_run(WAIT_STEP), WAIT_STUB,
                     _short_wait() | {"STUB_RUNS": runs})
    assert proc.returncode == 0, proc.stderr
    assert PROCEEDING in proc.stdout


def test_wait_times_out_without_an_aggregate_verdict():
    # A master push whose gate never reported a verdict — the exact hole
    # of issue #247: everything green, no aggregate row at all.
    runs = _row("completed", "success", "version-guard")
    proc = _run_step(_step_run(WAIT_STEP), WAIT_STUB,
                     _short_wait() | {"STUB_RUNS": runs})
    assert proc.returncode == 1
    assert PROCEEDING not in proc.stdout
    assert "Timed out" in proc.stderr


def test_wait_times_out_when_the_aggregate_conclusion_is_skipped():
    # `skipped` sits inside the wait's allowed set — without the verdict
    # rule the wait proceeds over an aggregate that skipped.
    runs = "\n".join([
        _row("completed", "success", "version-guard"),
        _row("completed", "skipped", "aggregate"),
    ])
    proc = _run_step(_step_run(WAIT_STEP), WAIT_STUB,
                     _short_wait() | {"STUB_RUNS": runs})
    assert proc.returncode == 1
    assert PROCEEDING not in proc.stdout


def test_wait_times_out_when_the_aggregate_is_a_foreign_app():
    # A check merely NAMED aggregate from another app proves nothing
    # about our gate; only the Actions app's own verdict counts.
    runs = "\n".join([
        _row("completed", "success", "version-guard"),
        _row("completed", "success", "aggregate", app="acme-ci"),
        _row("completed", "success", "lint"),
    ])
    proc = _run_step(_step_run(WAIT_STEP), WAIT_STUB,
                     _short_wait() | {"STUB_RUNS": runs})
    assert proc.returncode == 1
    assert PROCEEDING not in proc.stdout


def test_wait_proceeds_once_the_aggregate_reports(tmp_path):
    # The first read shows the aggregate queued; a later read completes
    # it with success and the wait proceeds.
    first = "\n".join([
        _row("completed", "success", "version-guard"),
        _row("queued", "-", "aggregate"),
    ])
    later = "\n".join([
        _row("completed", "success", "version-guard"),
        _row("completed", "success", "aggregate"),
    ])
    env = _short_wait() | {
        "STUB_CALLFILE": str(tmp_path / "gh-calls"),
        "STUB_RUNS_FIRST": first,
        "STUB_RUNS_LATER": later,
    }
    proc = _run_step(_step_run(WAIT_STEP), COUNTING_STUB, env)
    assert proc.returncode == 0, proc.stderr
    assert PROCEEDING in proc.stdout


def test_wait_refuses_promptly_when_a_check_failed():
    # A failed non-aggregate check refuses on the first read — no wait.
    runs = "\n".join([
        _row("completed", "failure", "tests"),
        _row("completed", "success", "aggregate"),
    ])
    proc = _run_step(_step_run(WAIT_STEP), WAIT_STUB,
                     {"STUB_RUNS": runs}, timeout=30)
    assert proc.returncode == 1
    assert "Refusing to release" in proc.stderr
    assert PROCEEDING not in proc.stdout


def test_wait_refuses_promptly_when_a_check_was_cancelled():
    # `cancelled` counts as failure: a cancelled run means a newer push
    # superseded this SHA, which is not a commit to release.
    runs = "\n".join([
        _row("completed", "cancelled", "tests"),
        _row("completed", "success", "aggregate"),
    ])
    proc = _run_step(_step_run(WAIT_STEP), WAIT_STUB,
                     {"STUB_RUNS": runs}, timeout=30)
    assert proc.returncode == 1
    assert "Refusing to release" in proc.stderr
    assert PROCEEDING not in proc.stdout


def test_wait_times_out_when_only_release_prefixed_checks_exist():
    # Its own release checks are excluded, so with only those present
    # there is nothing else to wait for — and nothing to proceed on.
    runs = _row("completed", "success", "release")
    proc = _run_step(_step_run(WAIT_STEP), WAIT_STUB,
                     _short_wait() | {"STUB_RUNS": runs})
    assert proc.returncode == 1
    assert PROCEEDING not in proc.stdout


def test_wait_projection_carries_the_app_slug():
    # The stub bypasses jq, so the projection itself has no behavioural
    # test — pin it at the source level instead: without the app slug in
    # the projection, the aggregate rule reads a column that is not there.
    assert ".app.slug" in _step_run(WAIT_STEP)
