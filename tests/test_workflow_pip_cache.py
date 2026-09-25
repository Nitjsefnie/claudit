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

from pathlib import Path
from typing import Iterator

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _steps() -> Iterator[tuple[str, str, dict]]:
    """Yield (workflow, job, step) over every step of every workflow."""
    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for job_id, job in (doc.get("jobs") or {}).items():
            for step in (job or {}).get("steps") or []:
                yield path.name, job_id, step


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
