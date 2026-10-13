"""docker-smoke gets both of its images through the shared image cache."""
from __future__ import annotations

import re
import shlex
from pathlib import Path

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
    digest = image["env"]["POSTGRES_DIGEST"]
    assert 'echo "tag=claudit-postgres:${POSTGRES_DIGEST#sha256:}"' in image["run"]
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
    reference = base["env"]["BASE_IMAGE"]
    assert reference == froms[0][0]
    assert base["env"]["BASE_FALLBACK_IMAGE"] == (
        "mirror.gcr.io/library/" + reference.rpartition("/library/")[2])
    cached = next(step for step in steps if step.get("uses") == CACHED)["with"]
    assert cached["ref"] == cached["tag"] == "${{ steps.base.outputs.ref }}"
    assert cached["fallback-ref"] == "${{ steps.base.outputs.fallback }}"
    assert cached["key"] == "${{ steps.base.outputs.key }}"
    assert 'echo "ref=${BASE_IMAGE}"' in base["run"]
    assert re.findall(r'(?m)^ *echo "(fallback=[^"]*)"$', base["run"]) == [
        "fallback=${BASE_FALLBACK_IMAGE}"]
    # A weekly bucket bounds how stale a cached tag-pinned base can get.
    assert "week=\"$(date -u +%G-W%V)\"" in base["run"]
    assert ("key=python-base-${RUNNER_OS}-${RUNNER_ARCH}-${BASE_IMAGE}-${week}"
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
        "! grep -Fxq \"FROM $BASE_IMAGE\" Dockerfile; then\n"
        "  echo \"::error::Dockerfile's FROM is not $BASE_IMAGE; "
        "update docker-smoke.yml with it\"\n"
        "  exit 1\n"
        "fi\n")
    assert guard in base["run"]
    # Failing after the outputs were written would still cache the base.
    assert base["run"].index(guard) < base["run"].index("GITHUB_OUTPUT")


def _acquire_run() -> str:
    return next(step for step in _load(CACHED_ACTION)["runs"]["steps"]
                if step.get("id") == "acquire")["run"]


def test_acquire_saves_only_a_successfully_pulled_image():
    run = _acquire_run()
    assignments = list(re.finditer(r"(?m)^ *pulled=(\w+)$", run))
    assert [match[1] for match in assignments] == ["false", "true"]
    success = re.search(
        r'(?ms)^(?P<indent> *)if timeout 120 docker pull "\$ref" '
        r'> "\$log" 2>&1; then\n.*?^(?P=indent)fi$', run)
    assert (success and assignments[0].start() < success.start()
            < assignments[1].start() < success.end())
    # A closing fi bounds failure arms too; the flag must precede any switch.
    before_assignment = run[success.start():assignments[1].start()]
    assert not re.search(r"(?m)(?:^|;)[ \t]*(?:else|elif)\b", before_assignment)
    refusal = re.search(
        r'(?ms)^(?P<indent> *)if \[ "\$pulled" != true \]; then\n'
        r'(?P<body>.*?)^(?P=indent)fi$', run)
    assert refusal and re.search(r"(?m)^ *exit 1$", refusal["body"])
    assert refusal.end() < min(run.index("docker tag"), run.index("docker save"))


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
