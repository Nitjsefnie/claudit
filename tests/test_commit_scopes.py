"""Unit tests for scripts/ci/commit_scopes.py and the aggregate-job wiring.

commit_scopes.py refuses a pull-request head whose commit subjects pair a
workflow-name scope with a type other than `ci` (issue #556, ported from
the Python source in Nitjsefnie-Actions/claim (tests/commit_scopes.py)
and Nitjsefnie-Actions/pr-gate (scripts/ci/commit_scopes.py)). These tests
pin the subject grammar, the workflow-name derivation from HEAD (a plain
top-level `name:` and nothing else — every other shape is refused rather
than guessed), the classification rule end to end over fixture repos, the
shared-name exemption for the `tests` scope, the outgoing-range-only
reach, the fail-closed refusal on an unreadable answer, and the
aggregate-job wiring: the check runs beside the merge-conflict-marker
step in the aggregate job on every run (docs-only changes included).
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    """Import scripts/ci/<name>.py by path (scripts/ci is no package)."""
    path = REPO_ROOT / "scripts" / "ci" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cs = _load("commit_scopes")

# Synthetic workflow names for the classification pins — the same
# single-word names the fixture repos commit below. The tree's own real
# workflow set is repository-managed data a test never pins (SV-TEST-DATA).
NAMES = {"tests", "codeql"}


# --- the subject grammar -----------------------------------------------------


def test_subject_parses_type_scope_and_summary():
    m = cs.SUBJECT.match("ci(tests): seed the fixture")
    assert m is not None
    assert m["type"] == "ci"
    assert m["scope"] == "tests"
    assert m["summary"] == "seed the fixture"


def test_subject_requires_a_scope():
    # The rule is about scopes: a subject with no scope carries nothing
    # to compare against the workflow-name set, so it does not parse and
    # is listed unexamined, never failed.
    assert cs.SUBJECT.match("ci: no scope") is None


def test_subject_admits_a_breaking_marker_on_either_side():
    for subject in ("feat(api)!: change the wire", "feat!(api): change the wire"):
        m = cs.SUBJECT.match(subject)
        assert m is not None
        em = "feat"
        sc = "api"
        assert m["type"] == em
        assert m["scope"] == sc


def test_subject_refuses_uppercase_and_punctuated_types():
    assert cs.SUBJECT.match("Dedup: re-point the limb") is None
    assert cs.SUBJECT.match("Fix(api): x") is None


# --- workflow_name: the one readable shape ------------------------------------


def test_workflow_name_reads_a_plain_top_level_name():
    assert cs.workflow_name("tests.yml", "name: tests\non: push\n") == "tests"


def test_workflow_name_ignores_indented_names():
    # A job's or a step's `name:` is indented and must not be collected:
    # collecting it would put a job display name under the ci-type rule
    # where the rule never put it.
    text = (
        "name: plain\n"
        "jobs:\n"
        "  suite:\n"
        "    name: codeql\n"
        "    steps:\n"
        "      - name: actionlint\n"
        "        run: true\n"
    )
    assert cs.workflow_name("tests.yml", text) == "plain"


def test_workflow_name_refuses_a_missing_name():
    with pytest.raises(cs.GateError):
        cs.workflow_name("w.yml", "on: push\n")


def test_workflow_name_refuses_a_quoted_value():
    with pytest.raises(cs.GateError):
        cs.workflow_name("w.yml", 'name: "plain"\n')


def test_workflow_name_refuses_a_trailing_comment():
    with pytest.raises(cs.GateError):
        cs.workflow_name("w.yml", "name: plain  # the suite\n")


def test_workflow_name_refuses_two_top_level_names():
    with pytest.raises(cs.GateError):
        cs.workflow_name("w.yml", "name: plain\n---\nname: other\n")


def test_workflow_name_refuses_a_continuation_line():
    with pytest.raises(cs.GateError):
        cs.workflow_name("w.yml", "name: plain\n  continued: yes\n")


# --- fixture repos ------------------------------------------------------------


def _run(argv: list[str], cwd: str | None = None) -> str:
    done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                          check=False)
    assert done.returncode == 0, done.stderr
    return done.stdout


def _fixture(tmp_path: Path, head_subjects: list[str]) -> Path:
    """A fixture repo with a bare `origin` whose master sits behind HEAD.

    Commits two workflow files with single-word top-level names (so a
    commit scope can reach them), pushes master, then adds the given
    subjects as empty commits on top. Returns the repo path; HEAD is the
    last head commit.
    """
    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    _run(["git", "init", "--bare", "-b", "master", str(origin)])
    _run(["git", "init", "-b", "master", str(repo)])
    _run(["git", "-C", str(repo), "remote", "add", "origin", str(origin)])
    _run(["git", "-C", str(repo), "config", "user.email", "fixture@example.com"])
    _run(["git", "-C", str(repo), "config", "user.name", "Fixture"])
    wf = repo / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "tests.yml").write_text("name: tests\non: push\njobs: {}\n")
    (wf / "codeql.yml").write_text("name: codeql\non: push\njobs: {}\n")
    (repo / "README.md").write_text("fixture\n")
    _run(["git", "-C", str(repo), "add", "."])
    _run(["git", "-C", str(repo), "commit", "-m", "chore(seed): fixture base"])
    _run(["git", "-C", str(repo), "push", "-q", str(origin), "master"])
    for subject in head_subjects:
        _run(["git", "-C", str(repo), "commit", "--allow-empty", "-m", subject])
    return repo


def _check(tmp_path: Path, head_subjects: list[str]) -> subprocess.CompletedProcess:
    repo = _fixture(tmp_path, head_subjects)
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "ci" / "commit_scopes.py"),
         "--root", str(repo)],
        capture_output=True, text=True, check=False)


def test_a_workflow_scope_outside_ci_fails_the_head(tmp_path):
    done = _check(tmp_path, ["fix(codeql): tweak the analysis"])
    assert done.returncode == 1
    assert "fix(codeql): tweak the analysis" in done.stdout
    assert "is the name of a workflow" in done.stdout


def test_the_ci_type_on_a_workflow_scope_passes(tmp_path):
    done = _check(tmp_path, ["ci(tests): restructure the leg"])
    assert done.returncode == 0
    assert "Examined 1 commit subject in origin/master..HEAD" in done.stdout


def test_the_shared_tests_scope_is_exempt(tmp_path):
    done = _check(tmp_path, ["fix(tests): flake in the fixture"])
    assert done.returncode == 0


def test_unparsed_subjects_are_listed_green(tmp_path):
    done = _check(tmp_path, ["Dedup: re-point the limb", "Merge branch 'x'"])
    assert done.returncode == 0
    assert "2 not examined" in done.stdout
    assert "Dedup: re-point the limb" in done.stdout


def test_only_the_outgoing_range_is_examined(tmp_path):
    # The seed commit is merged history: never re-judged, so an empty
    # outgoing range is green and examines nothing.
    done = _check(tmp_path, [])
    assert done.returncode == 0
    assert "Examined 0 commit subjects" in done.stdout


def test_an_unreadable_answer_is_a_refusal_not_a_green(monkeypatch, capsys):
    def refuse(root, *arguments, what):
        raise cs.GateError(f"cannot {what}")

    monkeypatch.setattr(cs, "git", refuse)
    assert cs.main(["commit_scopes.py", "--root", str(REPO_ROOT)]) == 1
    assert "commit scopes: cannot" in capsys.readouterr().err


# --- the aggregate-job wiring -------------------------------------------------


def _aggregate_steps() -> list[dict]:
    doc = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "ci-gate.yml").read_text())
    return doc["jobs"]["aggregate"]["steps"]


def _step_named(fragment: str) -> dict:
    matches = [s for s in _aggregate_steps() if fragment in s.get("name", "")]
    assert len(matches) == 1, f"expected exactly one step naming {fragment!r}"
    return matches[0]


def test_aggregate_job_runs_on_every_outcome():
    # The checks must run where docs-only changes land too: the legs all
    # skip there, and this job is the one read of the tree every run gets.
    doc = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "ci-gate.yml").read_text())
    assert doc["jobs"]["aggregate"]["if"] == "${{ always() }}"


def test_both_integrity_steps_precede_the_fold():
    names = [s.get("name", "") for s in _aggregate_steps()]
    conflict = next(i for i, n in enumerate(names)
                    if "merge-conflict marker" in n)
    scopes = next(i for i, n in enumerate(names)
                  if "scope names a workflow" in n)
    fold = next(i for i, n in enumerate(names) if "Fold the leg results" in n)
    assert conflict < scopes < fold


def test_conflict_marker_step_keeps_the_guard_shape():
    body = _step_named("merge-conflict marker")["run"]
    lines = [line.strip() for line in body.splitlines()]
    assert "found=0" in lines
    assert ("git grep -nI -E '^(<{7}( |$)|>{7}( |$)|={7}$)' -- ."
            " || found=$?") in lines
    assert 'if [ "$found" -gt 1 ]; then' in lines
    assert 'exit "$found"' in lines
    assert 'if [ "$found" -eq 0 ]; then' in lines
    assert ('echo "A merge-conflict marker is committed. Resolve the '
            'conflict, rebase onto the target branch and push again."'
            ) in lines


def test_scopes_step_runs_the_check():
    body = _step_named("scope names a workflow")["run"]
    assert body.strip() == "python3 scripts/ci/commit_scopes.py"


def test_contributing_states_the_rule_and_the_shared_name():
    text = (REPO_ROOT / "CONTRIBUTING.md").read_text()
    assert f"`{cs.SHARED_NAME_SCOPE}`" in text
    assert "only with the `ci` type" in text
