"""The re-seed walk's reach is covered by every loader checkout's depth.

Two pins, one contract (issue #582). ``scripts/ci/reseed.py`` reads the
suite-cost re-seed marker over a bounded ancestry walk (at most
``_MAX_VISITS`` commits from HEAD, the pull request's head lineage
first). A CI checkout that runs the thresholds loader must carry at
least that much ancestry, or the walk hits the shallow boundary, reads
the window as closed, and gates a family-absent document as a
violation -- the fail-closed red that marked every pull request during
the 2026-10 re-seed window.

The first test pins the MECHANIC on a synthetic marker-on-base history:
a clone shallow enough to cut below the marker reads ``in_flight()``
False while one carrying ``_MAX_VISITS + 1`` hops reads True. The
second pins the WORKFLOW SHAPE: every checkout whose job runs the
thresholds loader fetches full history (or at least the walk's bound
plus one), so the next bound change cannot silently re-open the
failure.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load(name="reseed"):
    """Import a scripts/ci module by path.

    scripts/ci is not a package and deliberately has no __init__.py — it
    holds standalone CI entry points, not an importable library.
    """
    path = REPO_ROOT / "scripts/ci" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["reseed"] = module
    spec.loader.exec_module(module)
    return module


reseed = _load()

# The walk's bound is deliberately module-private; this pin reads it
# anyway — the reading IS the contract (issue #582).
MAX_VISITS = reseed._MAX_VISITS  # pylint: disable=protected-access

# The loader jobs the explicit table pins (issue #582's fix set): every
# job in the three other suite-running workflows plus tests.yml's
# portable cells. tests.yml's primary pytest cell and ratchet-push are
# caught by the generic sweep below — a rename fails loudly as a
# KeyError here instead of silently leaving a loader job shallow.
LOADER_JOBS = {
    "tests.yml": ("pytest-portable",),
    "test-data.yml": ("perturbed-suite",),
    "test-data-explore.yml": ("perturbed-suite-explore",),
    "refresh-pricing.yml": ("refresh",),
}
KNOWN_LOADER_JOBS = {
    (name, job) for name, jobs in LOADER_JOBS.items() for job in jobs
}


def _git(root: Path, *args: str) -> str:
    """One git command's stdout; asserts on failure."""
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True, check=False, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _build_history(repo: Path) -> None:
    """A marker-on-base history under a GitHub-style merge commit.

    The marker sits TWO bot commits under the base-side tip: three
    parent-hops from the merge commit (visit 5 in the walk's order), so
    a depth-2 clone of the merge ref carries neither it nor the
    family-present base, while ``_MAX_VISITS + 1`` hops carry whatever
    the walk can visit.
    """
    def commit_doc(present: bool, message: str) -> None:
        doc: dict = {"coverage": {"python": {"measured": 90.0, "floor": 88.5}}}
        if present:
            doc["suite_cost"] = {
                "collection": {"measured": 12.5, "floor": 14.0}}
        path = repo / ".github" / "ci-thresholds.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(doc, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        _git(repo, "add", str(path))
        _git(repo, "commit", "-m", message)

    def commit_noise(name: str, message: str) -> None:
        # A commit that does not touch the thresholds document — the
        # hourly bot's shape while the window is open.
        path = repo / name
        path.write_text(f"{name}\n", encoding="utf-8")
        _git(repo, "add", str(path))
        _git(repo, "commit", "-m", message)

    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "master")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")
    commit_doc(True, "base: the family present, window closed")
    commit_doc(
        False,
        "[suite-cost-re-seed] Delete the suite_cost budgets ahead of "
        "the re-seed")
    commit_noise("bot-1.txt", "Ratchet ci-thresholds (automated)")
    commit_noise("bot-2.txt", "Refresh OpenRouter provider rates (automated)")
    _git(repo, "checkout", "-qb", "pr")
    (repo / "pr.txt").write_text("pr\n", encoding="utf-8")
    _git(repo, "add", str(repo / "pr.txt"))
    _git(repo, "commit", "-m", "pr head: the lineage a marker may ride")
    _git(repo, "checkout", "-q", "master")
    merge_tree = _git(repo, "rev-parse", "pr^{tree}").strip()
    merge = _git(repo, "commit-tree", merge_tree,
                 "-p", "master", "-p", "pr",
                 "-m", "Merge pull request #1 from Nitjsefnie/pr").strip()
    # commit-tree writes no ref: point a branch at the merge so the
    # clones below clone the two-parent shape, not the linear base tip.
    _git(repo, "update-ref", "refs/heads/merge", merge)


def _clone(source: Path, target: Path, depth: int | None) -> Path:
    """A clone of ``source``'s merge ref; None depth means full history."""
    args = ["git", "clone", "-q", "--branch", "merge"]
    if depth is not None:
        args += ["--depth", str(depth)]
    # file:// transport: --depth is ignored over a local path.
    args += [source.resolve().as_uri(), str(target)]
    proc = subprocess.run(args, capture_output=True, check=False, text=True)
    assert proc.returncode == 0, proc.stderr
    return target


def test_marker_on_base_reachable_at_max_visits_plus_one(
        tmp_path: Path) -> None:
    """Depth 2 cuts below a base-side marker; the walk's bound does not."""
    if shutil.which("git") is None:
        pytest.skip("git is required: this check builds a synthetic history")
    repo = tmp_path / "repo"
    _build_history(repo)
    merge = _git(repo, "rev-parse", "refs/heads/merge").strip()

    full = _clone(repo, tmp_path / "full", None)
    # The clones' HEAD is the merge commit itself — the walk's root —
    # and it carries no marker; the walk must reach the marker three
    # parent-hops down the base side.
    assert merge == _git(full, "rev-parse", "HEAD").strip()
    assert reseed.in_flight(root=full), (
        "a full clone of a marker-on-base merge ref must read the window "
        "as open")

    shallow = _clone(repo, tmp_path / "d2", 2)
    assert not reseed.in_flight(root=shallow), (
        "a depth-2 clone of a marker-on-base merge ref fails closed: the "
        "marker is unreachable, and the loader gates the family-absent "
        "document as a violation")

    wide = _clone(repo, tmp_path / "wide", MAX_VISITS + 1)
    assert reseed.in_flight(root=wide), (
        "a clone carrying _MAX_VISITS + 1 hops reaches any commit the "
        "walk can visit")


def test_every_loader_checkout_covers_the_reseed_walk() -> None:
    """A loader job's checkout carries the ancestry the walk reads.

    The loader jobs are named explicitly in LOADER_JOBS — a rename
    fails loudly (KeyError) instead of silently leaving a loader job
    shallow. A generic sweep beside them catches any OTHER job whose
    steps run pytest or name the thresholds loader, so a new loader
    job cannot open shallow; the sweep asserts the residue is exactly
    the ratchet-push job, whose loader runs in a temp repo fetched
    without a depth bound (pinned at the end).
    """
    workflows = REPO_ROOT / ".github" / "workflows"
    docs = {
        path.name: yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for path in sorted(workflows.glob("*.yml"))
    }

    def checkout_depth(name: str, job_name: str) -> int | None:
        job = (docs[name].get("jobs") or {})[job_name]
        for step in job.get("steps") or []:
            if isinstance(step, dict) and "actions/checkout" in (
                    step.get("uses") or ""):
                return (step.get("with") or {}).get("fetch-depth", 1)
        return None

    for name, job_names in LOADER_JOBS.items():
        for job_name in job_names:
            depth = checkout_depth(name, job_name)
            assert depth == 0 or depth >= MAX_VISITS + 1, (
                f"{name}:{job_name}: fetch-depth {depth} cannot carry the "
                "re-seed walk's ancestry; use full history (fetch-depth: 0) "
                "or >= _MAX_VISITS + 1 (issue #582)")

    # Generic sweep: every other job that runs pytest or names the
    # thresholds loader must satisfy the same bound. The residue (jobs
    # with no checkout of their own) must be exactly ratchet-push.
    swept = set()
    for name, doc in docs.items():
        for job_name, job in (doc.get("jobs") or {}).items():
            if not isinstance(job, dict) or (name, job_name) in KNOWN_LOADER_JOBS:
                continue
            steps = job.get("steps") or []
            scripts = "\n".join(
                step.get("run") or "" for step in steps
                if isinstance(step, dict))
            if "pytest" not in scripts and "thresholds" not in scripts:
                continue
            swept.add((name, job_name))
            depth = checkout_depth(name, job_name)
            if depth is None:
                continue  # no checkout: only ratchet-push reaches here
            assert depth == 0 or depth >= MAX_VISITS + 1, (
                f"{name}:{job_name}: fetch-depth {depth} cannot carry the "
                "re-seed walk's ancestry (issue #582)")
    assert swept == {("tests.yml", "pytest"), ("ratchet-push.yml", "push")}, (
        f"unexpected additional loader jobs: "
        f"{sorted(swept - {('tests.yml', 'pytest'), ('ratchet-push.yml', 'push')})}")

    # ratchet-push loads the loader from a temp repo it fetches itself;
    # that fetch must stay unbounded (no --depth), or the same
    # fail-closed red opens there.
    for step in (docs["ratchet-push.yml"]["jobs"]["push"].get("steps") or []):
        if isinstance(step, dict) and step.get("run"):
            assert "--depth" not in step["run"], (
                "ratchet-push.yml:push: a bounded fetch would cut the temp "
                "repo's re-seed walk short (issue #582)")
