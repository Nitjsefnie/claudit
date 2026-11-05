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
import shlex
from itertools import takewhile
from pathlib import Path
from typing import Iterator

import yaml
import pytest

ROOT = Path(__file__).resolve().parents[1]
BACKEND_REQUIREMENTS = "backend/requirements.txt"
HASHED_FLAGS = ("--require-hashes", "--only-binary=:all:", "--no-binary=PyYAML")
HASHED_PYYAML = "pip install " + " ".join(HASHED_FLAGS) + " -r requirements-pyyaml.txt"
PIP_INSTALL = re.compile(r"(?:RUN )?(?:python3? -m )?pip (?:install|wheel) ")
RUNNER_TEMP = re.compile(r"\$RUNNER_TEMP(?!\w)|\$\{RUNNER_TEMP\}")


def _jobs() -> Iterator[tuple[str, str, list]]:
    """Yield (workflow, job, steps) for every job of every workflow."""
    workflows = ROOT / ".github" / "workflows"
    for path in sorted(workflows.glob("*.yml")) + sorted(workflows.glob("*.yaml")):
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


@pytest.mark.parametrize("job_id", ["pytest-shards", "pytest-portable"])
def test_only_one_pytest_shard_saves_the_shared_pip_cache(job_id):
    steps = next(steps for workflow, job, steps in _jobs()
                 if (workflow, job) == ("tests.yml", job_id))
    save, = [step for step in steps if _action(step) == "actions/cache/save"]
    assert save["if"] == (
        "github.event_name == 'push' && github.ref == 'refs/heads/master' "
        "&& matrix.shard == 1")


def test_types_installs_and_caches_the_full_import_toolchain():
    steps = next(steps for workflow, job, steps in _jobs()
                 if (workflow, job) == ("types.yml", "pyright"))
    install, = [step for step in steps if step.get("name") == "Install dependencies"]
    commands = install["run"].split("&&")
    requirements, = [c for c in commands if "-r backend/requirements.txt" in c]
    assert "-r requirements-dev.txt" in requirements, (
        "runtime deps too, so pyright resolves third-party imports")
    assert "python -m pip install --upgrade pip" in install["run"]
    caches = [step for step in steps
              if _action(step) in ("actions/cache/restore", "actions/cache/save")]
    assert len(caches) == 2


def _commands(script: str, links: object = None) -> list[list[str]]:
    audited = re.search(r"pip[-_]audit", script)
    segments = re.split(r"&&|\n", script.replace("\\\n", ""))
    commands = [[RUNNER_TEMP.sub("${{ runner.temp }}", word) for word in shlex.split(segment, comments=True)]
                for segment in segments
                if PIP_INSTALL.match(segment.strip()) or (audited and BACKEND_REQUIREMENTS in segment)]
    return commands if links is None else [[f"PIP_FIND_LINKS={links}", *words] for words in commands]


def _step_links(step: dict) -> object:
    env = step.get("env")
    return env.get("PIP_FIND_LINKS") if isinstance(env, dict) else None


def _install_sites() -> dict[str, list[tuple[bool, list[list[str]]]]]:
    sites = {
        f"{workflow}:{job}": [("if" in step or bool(step.get("continue-on-error")),
                               _commands(step["run"], _step_links(step)))
                              for step in steps if "run" in step]
        for workflow, job, steps in _jobs()}
    stages = re.split(r"(?im)^[ \t]*FROM\b", (ROOT / "Dockerfile").read_text(encoding="utf-8"))
    for number, stage in enumerate(stages):
        sites[f"Dockerfile:{number}" if number else "Dockerfile"] = [(False, _commands(stage))]
    return sites


def _installs_backend(words: list[str]) -> bool:
    return BACKEND_REQUIREMENTS in words


def _is_hashed_pyyaml(words: list[str], verb: str = "install") -> bool:
    return (verb in words and all(flag in words for flag in HASHED_FLAGS)
            and "requirements-pyyaml.txt" in words and "||" not in words)


def _wheel_dir(words: list[str]) -> str | None:
    found = []
    for word, following in zip(words, [*words[1:], None]):
        if word in ("-w", "--wheel-dir"):
            found.append(following)
        elif word.startswith("--wheel-dir="):
            found.append(word.removeprefix("--wheel-dir="))
        elif word.startswith("-w"):
            found.append(word.removeprefix("-w"))
    return found[0] if len(found) == 1 else None


def _audit_takes_pyyaml_from_hashed_wheel(words: list[str], earlier: list[list[str]]) -> bool:
    assignments = list(takewhile(lambda word: re.match(r"\w+=", word), words))
    links = next((word.removeprefix("PIP_FIND_LINKS=") for word in reversed(assignments)
                  if word.startswith("PIP_FIND_LINKS=")), None)
    return ("PIP_ONLY_BINARY=pyyaml" in assignments and links is not None
            and any(_is_hashed_pyyaml(before, "wheel") and _wheel_dir(before) == links
                    for before in earlier))


def _backend_sites(sites: dict[str, list[tuple[bool, list[list[str]]]]]) -> list[str]:
    return [site for site, steps in sites.items()
            if any(_installs_backend(words) for _, installs in steps for words in installs)]


def _lacks_hashed_pyyaml(installs: list[list[str]], first: int) -> bool:
    words = installs[first]
    audit = not {"install", "wheel"} & set(words)
    return (not any(_is_hashed_pyyaml(before) for before in installs[:first])
            or audit and not _audit_takes_pyyaml_from_hashed_wheel(words, installs[:first]))


def _sites_without_hashed_pyyaml_first(sites: dict[str, list[tuple[bool, list[list[str]]]]]) -> list[str]:
    violations = []
    for site in _backend_sites(sites):
        earlier: list[list[str]] = []
        for skippable, installs in sites[site]:
            first = next((index for index, words in enumerate(installs)
                          if _installs_backend(words)), None)
            if first is not None and _lacks_hashed_pyyaml(earlier + installs, len(earlier) + first):
                violations.append(site)
                break
            if not skippable:
                earlier += installs
    return violations


def test_every_backend_requirements_install_follows_the_hashed_pyyaml_install():
    sites = _install_sites()
    assert not _sites_without_hashed_pyyaml_first(sites)
    assert len(_backend_sites(sites)) >= 17
