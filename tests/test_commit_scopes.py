"""Unit tests for scripts/ci/commit_scopes.py and the aggregate-job wiring.

commit_scopes.py refuses a pull-request head whose commit subjects break
CONTRIBUTING's format or pair a workflow-name scope with a type other
than `ci` (the scope rule ported from
the Python source in Nitjsefnie-Actions/claim (tests/commit_scopes.py)
and Nitjsefnie-Actions/pr-gate (scripts/ci/commit_scopes.py)). These tests
pin the subject grammar, the closed set of git- and GitHub-written
subjects, the workflow-name derivation from HEAD, the rules end to end
over fixture repos, the `tests` exemption, the outgoing-range-only reach,
the fail-closed refusal on an unreadable answer, and the aggregate-job
wiring beside the merge-conflict-marker step.
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tests.test_provider_rate_refresh import refresh

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    path = REPO_ROOT / "scripts" / "ci" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


cs = _load("commit_scopes")

TYPES = {"build", "chore", "ci", "docs", "feat", "fix", "perf", "refactor",
         "revert", "style", "test"}
GITHUB_MERGE = ("Merge a4cd43dcb7a424193d6b10ae3ef0653336157c62 into "
                "b17b31ef7777796f12ababe1d0280c86a81d750e")
WRITTEN_BY_GIT = [
    GITHUB_MERGE,
    "Merge branch 'master'",
    "Merge branch 'master' into ci-guards",
    "Merge branch 'topic' of https://github.com/o/r into master",
    "Merge remote-tracking branch 'origin/master'",
    "Merge remote-tracking branch 'origin/master' into topic",
    "Merge pull request #1316 from owner/topic",
    'Revert "feat: add a panel"',
    'Reapply "feat: add a panel"',
]
NOT_WRITTEN_BY_GIT = [
    GITHUB_MERGE.replace("a4cd", "a4c", 1),
    GITHUB_MERGE.replace("a4cd", "A4CD", 1),
    GITHUB_MERGE + " again",
    GITHUB_MERGE.rpartition(" ")[0] + " master",
    "Merge the topic",
    "Merge branch master",
    "Merge pull request from owner/topic",
    "Revert the panel",
    'Reverts "feat: add a panel"',
    'Revert "feat: add a panel',
    "Reapply feat: add a panel",
]
REFUSED = {
    "Fix a thing": "does not read as",
    "feat!(api): x": "does not read as",
    "feat(api)! x": "does not read as",
    "feat!!: x": "does not read as",
    "feat x": "does not read as",
    "feat:x": "does not read as",
    "feat:": "does not read as",
    "feat:  x": "does not read as",
    " feat: x": "does not read as",
    "feat(): x": "does not read as",
    "feat(a b): x": "does not read as",
    "feat(api!): x": "does not read as",
    "Fix: x": "type `Fix` is not one of",
    "feature: x": "type `feature` is not one of",
    "Dedup: re-point the limb": "type `Dedup` is not one of",
}


def test_the_type_table_is_contributings_eleven():
    assert cs.TYPES == TYPES


@pytest.mark.parametrize("subject,commit_type,scope", [
    ("ci(tests): seed the fixture", "ci", "tests"),
    ("ci: no scope", "ci", None),
    ("feat!: change the wire", "feat", None),
    ("feat(api)!: change the wire", "feat", "api"),
    *((f"{t}: x", t, None) for t in sorted(TYPES)),
])
def test_conforming_subjects_parse(subject, commit_type, scope):
    match = cs.conforming(subject)
    assert match is not None
    assert (match["type"], match["scope"]) == (commit_type, scope)
    assert cs.refusal(subject, set()) is None


@pytest.mark.parametrize("subject,observed", REFUSED.items())
def test_a_subject_outside_the_format_is_refused(subject, observed):
    assert cs.conforming(subject) is None
    assert observed in cs.refusal(subject, set())


@pytest.mark.parametrize("subject", WRITTEN_BY_GIT)
def test_git_and_github_subjects_are_admitted(subject):
    assert cs.written_by_git(subject)


@pytest.mark.parametrize("subject", NOT_WRITTEN_BY_GIT)
def test_near_misses_of_git_subjects_are_refused(subject):
    assert not cs.written_by_git(subject)
    assert cs.refusal(subject, set()) is not None


def _automation_subjects() -> list[str]:
    """Subjects the automation writes, built from its own templates."""
    workflows = REPO_ROOT / ".github" / "workflows"
    templates = re.findall(r"message='([^']*)'",
                           (workflows / "ratchet-push.yml").read_text())
    templates += re.findall(r'commit -m "([^"]*)"',
                            (workflows / "release-dispatch.yml").read_text())
    assert len(templates) == 3
    subjects = [re.sub(r"\$\w+", "1.2.3", t) for t in templates]
    result = SimpleNamespace(moves=[], refusals=["a refusal"])
    subjects.append(refresh.commit_message(result, "").splitlines()[0])
    updates = yaml.safe_load(
        (REPO_ROOT / ".github" / "dependabot.yml").read_text())["updates"]
    for update in updates:
        message = update["commit-message"]
        assert message["include"] == "scope"
        subjects += [f"{message['prefix']}({scope}): Bump x from 1.0 to 1.1"
                     for scope in ("deps", "deps-dev")]
    return subjects


def test_the_automation_subjects_pass_against_the_real_workflows():
    names = cs.workflow_name_set(REPO_ROOT)
    subjects = _automation_subjects()
    assert {s.partition("(")[0] for s in subjects} == {"build", "chore", "ci"}
    assert [s for s in subjects if cs.refusal(s, names) is not None] == []


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
        _run(["git", "-C", str(repo), "commit", "--allow-empty",
              "--cleanup=verbatim", "-m", subject])
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
    done = _check(tmp_path, ["ci(tests): restructure the leg",
                             "ci(codeql): tweak the analysis"])
    assert done.returncode == 0
    assert "Examined 2 commit subjects in origin/master..HEAD" in done.stdout


def test_the_shared_tests_scope_is_exempt(tmp_path):
    done = _check(tmp_path, ["fix(tests): flake in the fixture"])
    assert done.returncode == 0


def _listed(stdout: str) -> list[str]:
    return [m[1] for line in stdout.splitlines()
            if (m := re.fullmatch(r" +[0-9a-f]{40} (.*)", line))]


def _header(stdout: str) -> str:
    return next(line for line in stdout.splitlines() if line.startswith("Examined "))


def test_a_scopeless_conforming_subject_is_examined_and_passes(tmp_path):
    done = _check(tmp_path, ["ci: x"])
    assert done.returncode == 0
    assert _header(done.stdout) == ("Examined 1 commit subject in origin/master..HEAD; "
                                    "0 written by git or GitHub, not examined.")
    assert _listed(done.stdout) == []


def test_only_the_refused_subject_of_a_mixed_head_is_listed(tmp_path):
    done = _check(tmp_path, ["fix: before", "Fix a thing", "fix: after"])
    assert done.returncode == 1
    assert _listed(done.stdout) == ["Fix a thing"]


def test_subjects_outside_the_format_fail_the_head(tmp_path):
    done = _check(tmp_path, list(REFUSED) + NOT_WRITTEN_BY_GIT)
    assert done.returncode == 1
    assert sorted(_listed(done.stdout)) == sorted(list(REFUSED)
                                                  + NOT_WRITTEN_BY_GIT)
    assert "does not read as `type(scope)!: subject`" in done.stdout


def test_git_written_subjects_are_listed_green(tmp_path):
    done = _check(tmp_path, WRITTEN_BY_GIT + ["fix: x"])
    assert done.returncode == 0
    assert _header(done.stdout) == (f"Examined 1 commit subject in origin/master..HEAD; "
                                    f"{len(WRITTEN_BY_GIT)} written by git or GitHub, not examined:")
    assert sorted(_listed(done.stdout)) == sorted(WRITTEN_BY_GIT)


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
                  if "Refuse a commit subject" in n)
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
    body = _step_named("Refuse a commit subject")["run"]
    assert body.strip() == "python3 scripts/ci/commit_scopes.py"
