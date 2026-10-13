"""docker-smoke gets both of its images through the shared image cache."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/docker-smoke.yml"
OVERRIDE = ".github/docker-smoke.compose.yaml"
POSTGRES_IMAGE = ROOT / ".github/actions/postgres-image/action.yml"
CACHED = "$/.github/actions/cached-image"
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
