"""docker-smoke gets both of its images through the shared image cache."""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/docker-smoke.yml"
OVERRIDE = ".github/docker-smoke.compose.yaml"
POSTGRES_IMAGE = ROOT / ".github/actions/postgres-image/action.yml"
CACHED = "$/.github/actions/cached-image"
CACHED_ACTION = ROOT / ".github/actions/cached-image/action.yml"
FILES = ["compose.yaml", OVERRIDE]
# `docker compose -f a -f b <subcommand>`; the file list and the subcommand.
COMPOSE = re.compile(r"docker compose((?:\s+-f\s+[^\s)\"]+)*)\s+([a-z]+)([^\n)\"]*)")


def _load(path: Path) -> dict:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def _steps() -> list[dict]:
    return _load(WORKFLOW)["jobs"]["docker-smoke"]["steps"]


def _compose_calls() -> list[tuple[int, list[str], str, str]]:
    return [(i, match.group(1).split()[1::2], match.group(2), match.group(3))
            for i, step in enumerate(_steps())
            for match in COMPOSE.finditer(step.get("run", ""))]


def test_both_images_are_acquired_before_any_compose_call():
    steps = _steps()
    postgres = [i for i, step in enumerate(steps)
                if step.get("uses") == "$/.github/actions/postgres-image"]
    base = [i for i, step in enumerate(steps) if step.get("uses") == CACHED]
    assert len(postgres) == 1 and len(base) == 1
    for i in postgres + base:
        assert "if" not in steps[i] and "continue-on-error" not in steps[i]
    calls = _compose_calls()
    assert {"config", "up"} <= {sub for _, _, sub, _ in calls}
    assert max(postgres + base) < min(i for i, _, _, _ in calls)


def test_every_compose_call_uses_the_same_file_set():
    calls = _compose_calls()
    assert calls
    for _, files, sub, _ in calls:
        assert files == FILES, sub
    up = [rest for _, _, sub, rest in calls if sub == "up"]
    # A pull flag would bypass the cached base and db images.
    assert up == [" -d --build"]


def test_override_pins_only_the_cached_postgres_tag_and_no_pull():
    override = _load(ROOT / OVERRIDE)
    image = next(step for step in _load(POSTGRES_IMAGE)["runs"]["steps"]
                 if step.get("id") == "image")
    digest = image["env"]["CLAUDIT_POSTGRES_DIGEST"]
    assert 'echo "tag=claudit-postgres:${CLAUDIT_POSTGRES_DIGEST#sha256:}"' in image["run"]
    assert override == {"services": {"db": {
        "image": f"claudit-postgres:{digest.removeprefix('sha256:')}",
        "pull_policy": "never"}}}


def test_cached_base_image_is_the_dockerfiles_from_reference():
    froms = [line.split()[1:] for line in
             (ROOT / "Dockerfile").read_text(encoding="utf-8").splitlines()
             if line.startswith("FROM ")]
    assert len(froms) == 1 and len(froms[0]) == 1
    steps = _steps()
    base = next(step for step in steps if step.get("id") == "base")
    reference = base["env"]["CLAUDIT_BASE_IMAGE"]
    assert reference == froms[0][0]
    assert base["env"]["CLAUDIT_BASE_FALLBACK_IMAGE"] == (
        "mirror.gcr.io/library/" + reference.rpartition("/library/")[2])
    cached = next(step for step in steps if step.get("uses") == CACHED)["with"]
    assert cached["ref"] == cached["tag"] == "${{ steps.base.outputs.ref }}"
    assert cached["fallback-ref"] == "${{ steps.base.outputs.fallback }}"
    assert cached["key"] == "${{ steps.base.outputs.key }}"
    assert 'echo "ref=${CLAUDIT_BASE_IMAGE}"' in base["run"]
    assert re.findall(r'(?m)^ *echo "(fallback=[^"]*)"$', base["run"]) == [
        "fallback=${CLAUDIT_BASE_FALLBACK_IMAGE}"]
    # A weekly bucket bounds how stale a cached tag-pinned base can get.
    assert "week=\"$(date -u +%G-W%V)\"" in base["run"]
    assert ("key=python-base-${RUNNER_OS}-${RUNNER_ARCH}-${CLAUDIT_BASE_IMAGE}-${week}"
            in base["run"])
    # One job restores both archives; one path would let a save overwrite
    # the other image's archive and share its cache version.
    postgres = next(step for step in _load(POSTGRES_IMAGE)["runs"]["steps"]
                    if step.get("uses") == CACHED)["with"]
    assert cached["archive"] != postgres["archive"]


def test_base_guard_rejects_a_dockerfile_that_names_another_base():
    base = next(step for step in _steps() if step.get("id") == "base")
    guard = (
        "if [ \"$(grep -c '^FROM ' Dockerfile)\" != 1 ] || "
        "! grep -Fxq \"FROM $CLAUDIT_BASE_IMAGE\" Dockerfile; then\n"
        "  echo \"::error::Dockerfile's FROM is not $CLAUDIT_BASE_IMAGE; "
        "update docker-smoke.yml with it\"\n"
        "  exit 1\n"
        "fi\n")
    assert guard in base["run"]
    # Failing after the outputs were written would still cache the base.
    assert base["run"].index(guard) < base["run"].index("GITHUB_OUTPUT")


def _acquire_run() -> str:
    return next(step for step in _load(CACHED_ACTION)["runs"]["steps"]
                if step.get("id") == "acquire")["run"]


@pytest.fixture(scope="module", name="acquire_runner")
def _acquire_runner(tmp_path_factory):
    """Run real Bash; skip only Windows or missing Bash, never Ubuntu CI."""
    bash = shutil.which("bash")
    if os.name == "nt" or bash is None:
        pytest.skip("acquire script requires POSIX Bash")
    step = next(step for step in _load(CACHED_ACTION)["runs"]["steps"]
                if step.get("id") == "acquire")
    bin_dir = tmp_path_factory.mktemp("acquire-bin")
    stubs = {
        "docker": '''#!/bin/sh
set -eu
printf '%s\t' "$@" >> "$DOCKER_LOG"
printf '\n' >> "$DOCKER_LOG"
case "$1" in
  pull)
    case "$2" in
      "$CLAUDIT_IMAGE_REF") rc="$PRIMARY_PULL_RC" ;;
      "$CLAUDIT_IMAGE_FALLBACK_REF") rc="$MIRROR_PULL_RC" ;;
      *) exit 2 ;;
    esac
    if [ "$rc" != 0 ]; then
      echo 'Error response from daemon: toomanyrequests: Data limit exceeded' >&2
    fi
    exit "$rc" ;;
  tag|save|image|load) exit 0 ;;
  *) exit 2 ;;
esac
''',
        "timeout": '#!/bin/sh\nshift\nexec "$@"\n',
        "sleep": "#!/bin/sh\nexit 0\n",
    }
    for name, body in stubs.items():
        stub = bin_dir / name
        stub.write_text(body, encoding="utf-8")
        stub.chmod(0o755)

    def run(tmp_path, primary_rc, mirror_rc, cache_hit):
        script = tmp_path / "acquire.sh"
        script.write_text(step["run"], encoding="utf-8")
        output, log = tmp_path / "output", tmp_path / "docker.log"
        output.write_text("", encoding="utf-8")
        log.write_text("", encoding="utf-8")
        values = {
            "steps.cache.outputs.cache-hit": cache_hit,
            "inputs.label": "Test image", "inputs.ref": "primary/image:test",
            "inputs.fallback-ref": "mirror/image:test",
            "inputs.tag": "local/image:test", "inputs.archive": "image.tar",
        }
        env = {name: values[expr.removeprefix("${{").removesuffix("}}").strip()]
               for name, expr in step["env"].items()}
        # Only cat uses the minimal system path; Docker, timers and sleeps
        # are stubs. Successful tag/save/image calls model an existing image.
        env.update(PATH=str(bin_dir) + os.pathsep + os.defpath,
                   RUNNER_TEMP=str(tmp_path), GITHUB_OUTPUT=str(output),
                   DOCKER_LOG=str(log), PRIMARY_PULL_RC=str(primary_rc),
                   MIRROR_PULL_RC=str(mirror_rc))
        result = subprocess.run(
            [bash, "--noprofile", "--norc", "-e", "-o", "pipefail", str(script)],
            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10,
            check=False)
        calls = [line.rstrip("\t").split("\t")
                 for line in log.read_text(encoding="utf-8").splitlines()]
        return result, calls, output.read_text(encoding="utf-8")

    return run


@pytest.mark.parametrize("case,primary_rc,mirror_rc,cache_hit", [
    ("refusal", 1, 1, ""), ("primary", 0, 1, ""),
    ("mirror", 1, 0, ""), ("hit", 1, 1, "true"),
])
def test_acquire_execution(case, primary_rc, mirror_rc, cache_hit,
                           tmp_path, acquire_runner):
    """Exercise Bash control flow; skip only Windows or unavailable Bash."""
    result, calls, output = acquire_runner(tmp_path, primary_rc, mirror_rc, cache_hit)
    archive = str(tmp_path / "image.tar")
    if case == "refusal":
        assert result.returncode != 0, (calls, output, result.stderr)
        assert not any(call[0] in ("tag", "save") for call in calls), calls
        assert "pulled=true" not in output, output
        assert "image cache had no entry" in result.stderr, result.stderr
        assert "toomanyrequests: Data limit exceeded" in result.stderr, result.stderr
        assert calls == ([["pull", "primary/image:test"]] * 3
                         + [["pull", "mirror/image:test"]] * 3)
    elif case == "hit":
        assert result.returncode == 0, result.stderr
        assert calls == [["load", "--input", archive]], calls
        assert "pulled=true" not in output, output
    else:
        assert result.returncode == 0, result.stderr
        source = "primary/image:test" if case == "primary" else "mirror/image:test"
        pulls = ([["pull", "primary/image:test"]] if case == "primary" else
                 [["pull", "primary/image:test"]] * 3 + [["pull", "mirror/image:test"]])
        assert calls == pulls + [["tag", source, "local/image:test"],
                                 ["save", "--output", archive, "local/image:test"]]
        assert output.splitlines() == ["pulled=true"], output


def test_acquire_pull_attempts_have_a_timeout_and_finite_backoff():
    run = _acquire_run()
    pulls = re.findall(r"(?m)^ *if ([^\n]*docker pull[^\n]*); then$", run)
    assert [shlex.split(command)[:5] for command in pulls] == [
        ["timeout", "120", "docker", "pull", "$ref"]]
    delays = re.findall(r"(?m)^ *for delay in (.*); do$", run)
    assert [delay.split() for delay in delays] == [["0", "5", "15"]]


def test_acquire_fails_on_command_errors_from_its_first_command():
    commands = [line.strip() for line in _acquire_run().splitlines()
                if line.strip() and not line.lstrip().startswith("#")]
    assert commands[0] == "set -euo pipefail"
