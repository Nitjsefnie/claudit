"""Exercise the audit workflow's retry handling with a scripted pip-audit.

The step audits THREE manifest sets per pass — the co-installed tree
(backend/requirements.txt + dev + test), then requirements-pip-audit.txt
and requirements-zizmor.txt each alone (a hash-pinned manifest beside
un-hashed ones flips pip-audit's one internal resolution into
--require-hashes mode) — so a clean pass invokes pip-audit 3 times and a
retry-then-succeed scenario consumes 4 scripted responses (fail, ok,
ok, ok). A set that fails terminally ends the step, so exhaustion and
no-retry scenarios never reach the later sets and their counts are
unchanged.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "audit.yml"
STEP_NAME = "Audit dependencies"

# A set's clean response; a retry-then-succeed pass scripts one failure
# followed by a clean response for EVERY set.
OK = ("audit ok\n", 0)


def _pass_after_one_failure(failure: tuple[str, int]) -> list[tuple[str, int]]:
    return [failure] + [OK] * 3


pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win") or shutil.which("bash") is None,
    reason="the simulation runs the ubuntu workflow shell under bash",
)


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _step(name: str) -> dict:
    jobs = _workflow().get("jobs") or {}
    job, = jobs.values()
    matches = [step for step in job.get("steps") or []
               if step.get("name") == name]
    assert len(matches) == 1, f"expected exactly one step named {name!r}"
    step, = matches
    return step


def _pip_audit_shim() -> str:
    return r"""#!/usr/bin/env bash
set -eu
count=$(cat "$FAKE_COUNT_FILE" 2>/dev/null || printf 0)
count=$((count + 1))
printf '%s\n' "$count" > "$FAKE_COUNT_FILE"
cat "$FAKE_SCENARIO/$count.out"
exit "$(cat "$FAKE_SCENARIO/$count.status")"
"""


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _run_audit_step(
        tmp_path: Path,
        responses: list[tuple[str, int]]) -> tuple[subprocess.CompletedProcess, int]:
    bin_dir = tmp_path / "bin"
    scenario = tmp_path / "scenario"
    bin_dir.mkdir()
    scenario.mkdir()
    count_file = tmp_path / "calls"

    _write_executable(bin_dir / "pip-audit", _pip_audit_shim())
    _write_executable(bin_dir / "sleep", "#!/usr/bin/env bash\nexit 0\n")
    for attempt, (output, status) in enumerate(responses, start=1):
        (scenario / f"{attempt}.out").write_text(output, encoding="utf-8")
        (scenario / f"{attempt}.status").write_text(
            f"{status}\n", encoding="utf-8")

    env = os.environ.copy()
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    env["FAKE_COUNT_FILE"] = str(count_file)
    env["FAKE_SCENARIO"] = str(scenario)
    script = tmp_path / "audit-step.sh"
    script.write_text(_step(STEP_NAME)["run"], encoding="utf-8")

    result = subprocess.run(
        ["bash", "-e", str(script)],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    calls = int(count_file.read_text(encoding="utf-8"))
    return result, calls


def _output(result: subprocess.CompletedProcess) -> str:
    return result.stdout + result.stderr


def test_pypi_503_service_error_retries_and_succeeds(tmp_path: Path):
    failure = (
        "requests.exceptions.HTTPError: 503 Server Error: Backend is "
        "unhealthy for url: https://pypi.org/pypi/boto3/1.43.99/json\n"
        "pip_audit._service.interface.ServiceError: request failed\n"
    )
    result, calls = _run_audit_step(tmp_path, _pass_after_one_failure((failure, 1)))

    assert result.returncode == 0, _output(result)
    assert calls == 4


def test_repeated_pypi_503_service_errors_exhaust_retries_as_failure(
        tmp_path: Path):
    failure = (
        "requests.exceptions.HTTPError: 503 Server Error: Backend is "
        "unhealthy for url: https://pypi.org/pypi/boto3/1.43.99/json\n"
        "pip_audit._service.interface.ServiceError: request failed\n"
    )
    result, calls = _run_audit_step(tmp_path, [(failure, 1)] * 3)

    output = _output(result)
    assert result.returncode != 0
    assert calls == 3
    assert "reported findings" not in output.lower()
    assert "transport/service failure" in output.lower()


def test_vulnerability_finding_fails_without_retry(tmp_path: Path):
    finding = (
        "Found 1 known vulnerability in 1 package\n"
        "Name    Version ID             Fix Versions\n"
        "demo    1.0.0   GHSA-example  1.0.1\n"
    )
    result, calls = _run_audit_step(tmp_path, [(finding, 1)])

    assert result.returncode == 1
    assert calls == 1
    assert "pip-audit reported findings:" in _output(result)


def test_existing_connection_reset_error_still_retries(tmp_path: Path):
    result, calls = _run_audit_step(
        tmp_path,
        _pass_after_one_failure(("Connection reset by peer\n", 1)))

    assert result.returncode == 0, _output(result)
    assert calls == 4


@pytest.mark.parametrize(
    "message",
    [
        "ConnectionError: Could not connect to PyPI's vulnerability feed\n",
        "ConnectionError: Could not connect to OSV's vulnerability feed\n",
    ],
    ids=("pypi-connect-timeout", "osv-connect-timeout"),
)
def test_pip_audit_feed_connection_errors_retry(tmp_path: Path, message: str):
    result, calls = _run_audit_step(
        tmp_path, _pass_after_one_failure((message, 1)))

    assert result.returncode == 0, _output(result)
    assert calls == 4


def test_ssl_error_retries(tmp_path: Path):
    result, calls = _run_audit_step(
        tmp_path,
        _pass_after_one_failure(
            ("requests.exceptions.SSLError: certificate verify failed\n", 1)),
    )

    assert result.returncode == 0, _output(result)
    assert calls == 4


@pytest.mark.parametrize(
    "message",
    ["502 Bad Gateway\n", "503 Service Unavailable\n",
     "504 Gateway Timeout\n"],
    ids=("502-bad-gateway", "503-service-unavailable", "504-gateway-timeout"),
)
def test_bare_gateway_errors_retry(tmp_path: Path, message: str):
    result, calls = _run_audit_step(
        tmp_path, _pass_after_one_failure((message, 1)))

    assert result.returncode == 0, _output(result)
    assert calls == 4


def test_http_429_retries_then_succeeds(tmp_path: Path):
    rate_limit = (
        "429 Client Error: Too Many Requests for url: "
        "https://pypi.org/pypi/demo/json\n"
    )
    result, calls = _run_audit_step(
        tmp_path, _pass_after_one_failure((rate_limit, 1)))

    assert result.returncode == 0, _output(result)
    assert calls == 4


def test_package_named_service_error_is_reported_without_retry(tmp_path: Path):
    finding = (
        "Found 1 known vulnerability in 1 package\n"
        "Name          Version ID          Fix Versions\n"
        "ServiceError  1.0.0   GHSA-demo  1.0.1\n"
    )
    result, calls = _run_audit_step(tmp_path, [(finding, 1)] * 3)

    assert result.returncode == 1
    assert calls == 1
    assert "pip-audit reported findings:" in _output(result)


def test_http_404_is_not_retried(tmp_path: Path):
    result, calls = _run_audit_step(
        tmp_path,
        [(
            "404 Client Error: Not Found for url: https://pypi.org/package\n"
            "pip_audit._service.interface.ServiceError: package not found\n",
            1,
        )],
    )

    assert result.returncode == 1
    assert calls == 1
    assert "404 Client Error" in _output(result)
