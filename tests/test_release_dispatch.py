"""Release preflight and wait logic with invented GitHub facts."""
from __future__ import annotations

import json
import subprocess

import pytest

from scripts.ci import release_dispatch
from scripts.ci.release_dispatch import (
    Decision,
    decide,
    main,
    parse_release_version,
    wait_release,
)

SHA = "a1" * 20
REPO = "example/releases"


class FakeClock:
    def __init__(self):
        self.elapsed = 0.0
        self.sleeps = []

    def __call__(self):
        return self.elapsed

    def sleep(self, seconds):
        assert len(self.sleeps) < 12, "polling failed to terminate"
        self.sleeps.append(seconds)
        self.elapsed += seconds


def run_row(status="completed", conclusion: str | None = "success", *, run_id=42,
            path=".github/workflows/release.yml", event="push"):
    return {"id": run_id, "name": "Release package", "status": status,
            "conclusion": conclusion, "path": path, "event": event}


def pages(*rows_by_page):
    return " \n".join(
        json.dumps({"workflow_runs": rows}) for rows in rows_by_page
    ) + "\n "


def reader(*responses):
    calls = []

    def read(argv):
        assert argv == [
            "gh", "api", "-H", "Cache-Control: no-cache", "--paginate",
            f"repos/{REPO}/actions/runs?head_sha={SHA}&per_page=100",
        ]
        assert len(calls) < 13, "polling failed to terminate"
        response = responses[min(len(calls), len(responses) - 1)]
        calls.append(argv)
        return response

    return read


@pytest.mark.parametrize("text", ["0.5.0", "10.20.30", "01.2.3"])
def test_parse_release_version_accepts(text):
    assert parse_release_version(text) == text


@pytest.mark.parametrize("text", [
    "", "  ", "v0.5.0", "0.5", "0.5.0-dev", "0.5.0.1",
    "0.5.0-rc1", "0.5.0-dev!", "0.5.0\n", " 0.5.0", "0.5.0 ",
])
def test_parse_release_version_refuses(text):
    with pytest.raises(ValueError, match="X.Y.Z"):
        parse_release_version(text)


def test_decide_free():
    result = decide("9.9.9", False, False)
    assert isinstance(result, Decision)
    assert result.ok is True
    assert "9.9.9" in result.reason
    assert "free" in result.reason


@pytest.mark.parametrize("tag,release,blocker", [
    (True, False, "tag"), (False, True, "release"), (True, True, "tag"),
])
def test_decide_refuses_existing_publication(tag, release, blocker):
    result = decide("9.9.9", tag, release)
    assert result.ok is False
    assert blocker in result.reason
    assert "v9.9.9" in result.reason


def test_decide_refuses_bad_version():
    result = decide("9.9.9-dev", False, False)
    assert result.ok is False
    assert "X.Y.Z" in result.reason
    assert "9.9.9-dev" in result.reason


def test_wait_release_consumes_all_pages():
    timer = FakeClock()
    read = reader(pages([run_row(path=".github/workflows/ci-gate.yml")],
                        [run_row()]))
    assert wait_release(SHA, read=read, repo=REPO,
                        clock=timer, sleep=timer.sleep) is None
    assert not timer.sleeps


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", None])
def test_wait_release_failure_is_immediate(conclusion):
    timer = FakeClock()
    with pytest.raises(ValueError) as error:
        wait_release(SHA, read=reader(pages([run_row(conclusion=conclusion)])),
                     repo=REPO, timeout=100, clock=timer, sleep=timer.sleep)
    assert "Release package" in str(error.value)
    assert "42" in str(error.value)
    assert str(conclusion) in str(error.value)
    assert timer.elapsed == 0
    assert not timer.sleeps


def test_wait_release_polls_until_success_at_deadline():
    timer = FakeClock()
    read = reader(pages([run_row("in_progress", None)]),
                  pages([run_row("queued", None)]), pages([run_row()]))
    wait_release(SHA, read=read, repo=REPO, timeout=8, grace=2, interval=4,
                 clock=timer, sleep=timer.sleep)
    assert timer.elapsed == 8
    assert timer.sleeps == [4, 4]


@pytest.mark.parametrize("rows", [
    [],
    [run_row(conclusion="failure", event="workflow_dispatch"),
     run_row(conclusion="failure", path=".github/workflows/ci-gate.yml")],
])
def test_wait_release_absence_fails_at_grace(rows):
    timer = FakeClock()
    with pytest.raises(ValueError) as error:
        wait_release(SHA, read=reader(pages(rows)), repo=REPO,
                     timeout=100, grace=6, interval=2,
                     clock=timer, sleep=timer.sleep)
    assert f"no release run registered for {SHA} within 6 s" in str(error.value)
    assert timer.elapsed == pytest.approx(6)
    assert timer.sleeps == [2, 2, 2]


def test_wait_release_active_run_times_out():
    timer = FakeClock()
    with pytest.raises(ValueError, match="timeout") as error:
        wait_release(SHA, read=reader(pages([run_row("in_progress", None)])),
                     repo=REPO, timeout=6, grace=2, interval=2,
                     clock=timer, sleep=timer.sleep)
    assert SHA in str(error.value)
    assert timer.elapsed == 6
    assert timer.sleeps == [2, 2, 2]


def test_wait_release_two_successful_runs():
    timer = FakeClock()
    wait_release(SHA, read=reader(pages([run_row(), run_row(run_id=43)])),
                 repo=REPO, clock=timer, sleep=timer.sleep)
    assert not timer.sleeps


@pytest.mark.parametrize("status", ["completed", "in_progress"])
def test_wait_release_one_failed_among_two_precedes_timeout(status):
    timer = FakeClock()
    rows = [run_row(status, "success"),
            run_row(conclusion="failure", run_id=43)]
    with pytest.raises(ValueError, match="43.*failure"):
        wait_release(SHA, read=reader(pages(rows)), repo=REPO, timeout=0,
                     clock=timer, sleep=timer.sleep)
    assert not timer.sleeps


def test_wait_release_all_runs_must_finish():
    timer = FakeClock()
    read = reader(pages([run_row(), run_row("in_progress", None, run_id=43)]),
                  pages([run_row(), run_row(run_id=43)]))
    wait_release(SHA, read=read, repo=REPO, interval=3,
                 clock=timer, sleep=timer.sleep)
    assert timer.sleeps == [3]


@pytest.fixture(name="cli_env")
def _cli_env(monkeypatch):
    monkeypatch.setenv("REPO", REPO)
    for name in ("RELEASE_DISPATCH_WAIT_SECONDS",
                 "RELEASE_DISPATCH_WAIT_POLL_SECONDS",
                 "RELEASE_DISPATCH_WAIT_GRACE_SECONDS"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("tag,release,code,reason", [
    ("FaLsE", "FALSE", 0, "free"),
    ("TRUE", "false", 1, "tag"),
    ("false", "TrUe", 1, "release"),
])
def test_cli_decide(tag, release, code, reason, capsys):
    assert main(["decide", "--version", "9.9.9", "--tag-exists", tag,
                 "--release-exists", release]) == code
    output = capsys.readouterr()
    assert reason in output.out
    assert output.err == ""


def test_cli_wait_release_success(cli_env, capsys):
    assert main(["wait-release", SHA], read=reader(pages([run_row()]))) == 0
    assert capsys.readouterr().err == ""


def test_cli_wait_release_failure(cli_env, capsys):
    assert main(["wait-release", SHA], read=reader(
        pages([run_row(conclusion="failure")]))) == 1
    output = capsys.readouterr()
    assert "failure" in output.err
    assert "42" in output.err
    assert output.out == ""


@pytest.mark.parametrize("sha", [
    "abc", "A" * 40, "g" * 40, "a" * 39, "a" * 41, "a" * 40 + "\n",
])
def test_cli_wait_release_refuses_bad_sha(sha, capsys):
    with pytest.raises(SystemExit) as error:
        main(["wait-release", sha])
    assert error.value.code == 2
    assert "sha" in capsys.readouterr().err.lower()


@pytest.mark.parametrize("repo", [None, ""])
def test_cli_wait_release_requires_repo(repo, monkeypatch, capsys):
    if repo is None:
        monkeypatch.delenv("REPO", raising=False)
    else:
        monkeypatch.setenv("REPO", repo)

    def read(_argv):
        pytest.fail("REPO must be checked before contacting GitHub")

    assert main(["wait-release", SHA], read=read) == 1
    assert "REPO" in capsys.readouterr().err


@pytest.mark.parametrize("error", [
    ValueError("bad JSON"), OSError("gh is unavailable"),
    subprocess.CalledProcessError(1, ["gh"]),
])
def test_cli_wait_release_read_errors(cli_env, capsys, error):
    def read(_argv):
        raise error

    assert main(["wait-release", SHA], read=read) == 1
    assert str(error) in capsys.readouterr().err


@pytest.mark.parametrize("argv,env,elapsed,sleeps,reason", [
    ([], {"RELEASE_DISPATCH_WAIT_SECONDS": "6",
          "RELEASE_DISPATCH_WAIT_GRACE_SECONDS": "4",
          "RELEASE_DISPATCH_WAIT_POLL_SECONDS": "2"}, 4, [2, 2], "no release"),
    (["--grace", "3", "--interval", "1", "--timeout", "8"],
     {"RELEASE_DISPATCH_WAIT_SECONDS": "0",
      "RELEASE_DISPATCH_WAIT_GRACE_SECONDS": "0",
      "RELEASE_DISPATCH_WAIT_POLL_SECONDS": "9"}, 3, [1, 1, 1], "no release"),
    ([], {"RELEASE_DISPATCH_WAIT_SECONDS": "0",
          "RELEASE_DISPATCH_WAIT_GRACE_SECONDS": "10"}, 0, [], "timeout"),
])
def test_cli_wait_release_timing_options(
        cli_env, monkeypatch, capsys, argv, env, elapsed, sleeps, reason):
    timer = FakeClock()
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    real_wait = wait_release

    def fake_time_wait(sha, **kwargs):
        return real_wait(sha, **kwargs, clock=timer, sleep=timer.sleep)

    monkeypatch.setattr(release_dispatch, "wait_release", fake_time_wait)
    assert main(["wait-release", SHA, *argv], read=reader(pages([]))) == 1
    assert reason in capsys.readouterr().err
    assert timer.elapsed == elapsed
    assert timer.sleeps == sleeps


@pytest.mark.parametrize("option,env_name", [
    ("--timeout", "RELEASE_DISPATCH_WAIT_SECONDS"),
    ("--grace", "RELEASE_DISPATCH_WAIT_GRACE_SECONDS"),
    ("--interval", "RELEASE_DISPATCH_WAIT_POLL_SECONDS"),
])
@pytest.mark.parametrize("value", ["nan", "INF", "-inf", "1e309", "-1", "-0.5"])
@pytest.mark.parametrize("source", ["cli", "environment"])
def test_cli_wait_release_rejects_invalid_timing_before_polling(
        cli_env, monkeypatch, capsys, option, env_name, value, source):
    argv = ["wait-release", SHA]
    if source == "cli":
        argv.append(f"{option}={value}")
    else:
        monkeypatch.setenv(env_name, value)
    calls = []

    def read(args):
        calls.append(args)
        return pages([run_row()])

    with pytest.raises(SystemExit) as error:
        main(argv, read=read)
    assert error.value.code == 2
    assert not calls
    diagnostic = capsys.readouterr().err
    assert option in diagnostic
    assert "finite" in diagnostic
    assert "non-negative" in diagnostic
