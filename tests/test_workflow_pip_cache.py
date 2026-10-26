"""Workflow shape: the pip cache restores on any event, saves from master.

`cache: pip` on a setup-python step saves the cross-run pip cache in a
post-job step, which runs AFTER the job has checked out and executed the
tree under test. On a pull_request event that is untrusted code writing
the cross-run state a later master push restores and installs from
. The shape pinned here replaces it: no setup-python step
owns the cache, restore runs on every event, and save runs only from a
push of the default branch, immediately after that job's dependency
install so the freshly populated cache is what gets saved.
"""
from __future__ import annotations

import math
import re
import sys
from pathlib import Path
from typing import Iterator

import yaml
import pytest

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
    for workflow, job, steps in _jobs():
        for index, step in enumerate(steps):
            if _action(step) != "actions/cache/save":
                continue
            predecessor = steps[index - 1] if index else {}
            assert "pip install" in (predecessor.get("run") or ""), (
                workflow, job, index)


def _github_number(value: str | int | float | bool | None) -> float:
    # Fail closed beyond JSON decimals, trimmed-empty zero, and plain ASCII
    # nonnumber words: actions/runner ExpressionUtility.ParseNumber (called by
    # EvaluationResult.CoerceTypes) also accepts hex, octal and infinity forms.
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return 0.0
        if re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?", value):
            return float(value)
        assert (re.fullmatch(r"[A-Za-z]+", value)
                and value.casefold() not in {"infinity", "nan"}), (
                    "unmodelled Actions numeric conversion", value)
        return math.nan
    if value is None:
        return 0.0
    return float(value)


def _github_equal(value: object, literal: str | int) -> bool:
    assert value is None or isinstance(value, (str, int, float, bool)), value
    if isinstance(value, float):
        assert math.isfinite(value), value
    if isinstance(value, str) and isinstance(literal, str):
        return value.casefold() == literal.casefold()
    return _github_number(value) == _github_number(literal)


def _assert_one_writer(matrix: dict, gate: str) -> None:
    assert isinstance(matrix, dict) and matrix, matrix
    assert not {"include", "exclude"}.intersection(matrix), matrix
    terms = [term.strip() for term in gate.split("&&")]
    assert terms[:2] == ["github.event_name == 'push'",
                         "github.ref == 'refs/heads/master'"], gate
    pinned = {}
    for term in terms[2:]:
        match = re.fullmatch(r"matrix\.([\w-]+) == (?:'([^']*)'|(\d+))", term)
        assert match, gate
        axis, quoted, numeric = match.groups()
        assert axis not in pinned, gate
        pinned[axis] = quoted if quoted is not None else int(numeric)
    assert pinned.keys() == matrix.keys(), gate
    admitted_jobs = 1
    for axis, literal in pinned.items():
        assert isinstance(matrix[axis], list), matrix
        admitted_jobs *= sum(_github_equal(value, literal) for value in matrix[axis])
    assert admitted_jobs == 1, (gate, admitted_jobs)


def test_a_matrix_job_saving_under_a_shared_key_saves_from_one_value():
    # A matrix runs the job once per value. When the save's key varies by
    # a matrix value (tests.yml keys its python-version matrix that way)
    # each job writes its own key and all may save. When the key does NOT
    # vary — the perturbed leg's three seed jobs share one pip key — N
    # jobs save under it, the losers fail on a key that already exists,
    # and the leg goes red on a cache race, never on a test. So that case
    # is pinned to one matrix job across all axes.
    for workflow, job_id, steps in _jobs():
        for step in steps:
            if _action(step) != "actions/cache/save":
                continue
            doc = yaml.safe_load(
                (ROOT / ".github" / "workflows" / workflow).read_text(
                    encoding="utf-8")) or {}
            strategy = (doc["jobs"][job_id] or {}).get("strategy") or {}
            if "matrix" not in strategy:
                continue
            key = (step.get("with") or {}).get("key") or ""
            if "matrix." in key:
                continue
            # Schedule/dispatch-only workflows cannot enter the master-push save gate.
            events = doc.get("on", doc.get(True, {}))
            if not any(event in events for event in ("push", "workflow_call")):
                continue
            _assert_one_writer(strategy["matrix"], step.get("if") or "")


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
        restore_only = {"coverage-combine", "js-coverage", "reparse-gate",
                        "pytest-thresholds", "ratchet", "baselines"}
        if workflow == "tests.yml" and job in restore_only:
            assert len(restore_keys) == 1 and not save_keys, (workflow, job)
        else:
            assert restore_keys == save_keys, (workflow, job)


def test_only_one_pytest_shard_saves_the_shared_pip_cache():
    steps = next(steps for workflow, job, steps in _jobs()
                 if (workflow, job) == ("tests.yml", "pytest-shards"))
    save, = [step for step in steps if _action(step) == "actions/cache/save"]
    assert save["if"] == (
        "github.event_name == 'push' && github.ref == 'refs/heads/master' "
        "&& matrix.shard == 1")


def test_types_installs_and_caches_the_full_import_toolchain():
    steps = next(steps for workflow, job, steps in _jobs()
                 if (workflow, job) == ("types.yml", "pyright"))
    install, = [step for step in steps if step.get("name") == "Install dependencies"]
    assert install["run"].splitlines() == [
        "python -m pip install --upgrade pip",
        "pip install -r backend/requirements.txt -r requirements-dev.txt -r requirements-test.txt && pip install --require-hashes --only-binary=:all: --no-binary=PyYAML -r requirements-pyyaml.txt"]
    caches = [step for step in steps
              if _action(step) in ("actions/cache/restore", "actions/cache/save")]
    assert len(caches) == 2


@pytest.mark.parametrize("matrix,gate,accepted", [
    ({"seed": ["42", "43"], "shard": [1, 2]}, "matrix.seed == '42' && matrix.shard == 1", True),
    ({"shard": [1, 2]}, "matrix.shard == 1", True),
    ({"shard": [1, 1, 2]}, "matrix.shard == 1", False),
    ({"shard": [1, "1", 2]}, "matrix.shard == 1", False),
    pytest.param({"shard": [1, "0x1"]}, "matrix.shard == 1", False, id="hex-alias"),
    pytest.param({"shard": [1, "+1"]}, "matrix.shard == 1", False, id="leading-plus"),
    pytest.param({"shard": [1, "01"]}, "matrix.shard == 1", False, id="leading-zero"),
    pytest.param({"shard": [0, " "]}, "matrix.shard == 0", False, id="whitespace-zero"),
    ({"shard": ["1", "other"]}, "matrix.shard == 1", True),
    ({"shard": ["one", "ONE"]}, "matrix.shard == 'one'", False),
    ({"shard": [1], "include": [{"shard": 1}]}, "matrix.shard == 1", False),
    ({"shard": [1], "exclude": [{"shard": 1}]}, "matrix.shard == 1", False),
    ({}, "matrix.shard == 1", False),
    ([], "matrix.shard == 1", False),
    ({"seed": ["42", "43"], "shard": [1, 2]}, "matrix.seed == '42'", False),
    ({"shard": [1, 2]}, "matrix.shard == 1 || github.ref == 'refs/heads/master'", False),
    ({"shard": [1, 2]}, "matrix.shard == 3", False),
])
def test_shared_cache_gate_accepts_exactly_one_matrix_job(tmp_path, monkeypatch, matrix, gate, accepted):
    workflows = tmp_path / ".github/workflows"
    workflows.mkdir(parents=True)
    step = {"uses": "actions/cache/save", "with": {"key": "shared"},
            "if": "github.event_name == 'push' && github.ref == 'refs/heads/master' && " + gate}
    doc = {"on": {"workflow_call": None},
           "jobs": {"suite": {"strategy": {"matrix": matrix}, "steps": [step]}}}
    (workflows / "sample.yml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    if accepted:
        test_a_matrix_job_saving_under_a_shared_key_saves_from_one_value()
    else:
        with pytest.raises(AssertionError):
            test_a_matrix_job_saving_under_a_shared_key_saves_from_one_value()
