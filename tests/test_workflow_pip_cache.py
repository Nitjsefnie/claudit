"""Workflow shape: the pip cache restores on any event, saves from master.

`cache: pip` on a setup-python step saves the cross-run pip cache in a
post-job step, which runs AFTER the job has checked out and executed the
tree under test. On a pull_request event that is untrusted code writing
the cross-run state a later master push restores and installs from
(issue #126). The shape pinned here replaces it: no setup-python step
owns the cache, restore runs on every event, and save runs only from a
push of the default branch, immediately after that job's dependency
install so the freshly populated cache is what gets saved.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _jobs() -> Iterator[tuple[str, str, list]]:
    """Yield (workflow, job, steps) for every job of every workflow."""
    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for job_id, job in (doc.get("jobs") or {}).items():
            yield path.name, job_id, (job or {}).get("steps") or []


def _steps() -> Iterator[tuple[str, str, dict]]:
    """Yield (workflow, job, step) over every step of every workflow."""
    for workflow, job_id, steps in _jobs():
        for step in steps:
            yield workflow, job_id, step


def _action(step: dict) -> str:
    """Return the action name a `uses:` names, without its ref."""
    return (step.get("uses") or "").split("@", 1)[0]


def test_no_setup_python_step_owns_the_cache():
    for workflow, job, step in _steps():
        if _action(step) != "actions/setup-python":
            continue
        with_ = step.get("with") or {}
        assert "cache" not in with_, (workflow, job, step.get("with"))


def test_every_cache_save_is_gated_to_a_master_push():
    for workflow, job, step in _steps():
        if _action(step) != "actions/cache/save":
            continue
        gate = step.get("if") or ""
        assert "github.event_name == 'push'" in gate, (workflow, job, gate)
        assert "github.ref == 'refs/heads/master'" in gate, (workflow, job, gate)


def test_every_cache_restore_runs_on_every_event():
    for workflow, job, step in _steps():
        if _action(step) != "actions/cache/restore":
            continue
        assert "if" not in step, (workflow, job, step.get("if"))


def test_every_cache_save_directly_follows_the_dependency_install():
    # The save's position is deliberate: the install is what populates the
    # cache, so the save must be its immediate successor. A step moved
    # after the suite (or before the install) fails here.
    for workflow, job, steps in _jobs():
        for index, step in enumerate(steps):
            if _action(step) != "actions/cache/save":
                continue
            predecessor = steps[index - 1] if index else {}
            assert "pip install" in (predecessor.get("run") or ""), (
                workflow, job, index)


def test_a_matrix_job_saving_under_a_shared_key_saves_from_one_value():
    # A matrix runs the job once per value. When the save's key varies by
    # a matrix value (tests.yml keys its python-version matrix that way)
    # each job writes its own key and all may save. When the key does NOT
    # vary — the perturbed leg's three seed jobs share one pip key — N
    # jobs save under it, the losers fail on a key that already exists,
    # and the leg goes red on a cache race, never on a test. So that case
    # is pinned to a single matrix value.
    for workflow, job_id, steps in _jobs():
        for step in steps:
            if _action(step) != "actions/cache/save":
                continue
            doc = yaml.safe_load(
                (ROOT / ".github" / "workflows" / workflow).read_text(
                    encoding="utf-8")) or {}
            strategy = (doc["jobs"][job_id] or {}).get("strategy") or {}
            if not strategy.get("matrix"):
                continue
            key = (step.get("with") or {}).get("key") or ""
            if "matrix." in key:
                continue
            gate = step.get("if") or ""
            # The gate must EXCLUDE the other values, so the pinned shape
            # is exactly ONE equality naming a value the matrix actually
            # carries: a substring check would be satisfied by
            # `matrix.seed != ...`, which excludes nothing, and a gate
            # naming two of them admits a second saver.
            pinned = re.findall(r"matrix\.(\w+) == '([^']*)'", gate)
            assert len(pinned) == 1, (workflow, job_id, gate)
            axis, value = pinned[0]
            assert value in strategy["matrix"].get(axis, []), (
                workflow, job_id, gate)


def test_restore_and_save_steps_share_their_cache_key():
    # One key per job, spelled identically in both steps: save stores under
    # exactly the key restore looks up. Whitespace is normalised so a
    # folded-scalar spelling cannot split the pair.
    for workflow, job, steps in _jobs():
        restore_keys = set()
        save_keys = set()
        for step in steps:
            key = " ".join(
                ((step.get("with") or {}).get("key") or "").split())
            if _action(step) == "actions/cache/restore":
                restore_keys.add(key)
            if _action(step) == "actions/cache/save":
                save_keys.add(key)
        if not restore_keys and not save_keys:
            continue
        assert restore_keys == save_keys, (workflow, job)
