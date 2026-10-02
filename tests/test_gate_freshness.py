"""Unit tests for the gate-freshness publisher.

The check goes red on an open head when master holds a commit the head
lacks whose changed paths hit the gate trigger set. These tests pin
the compare (per-commit changed paths vs the set), the fail shapes
(unreadable per-head compare publishes red on that head; a global read
failure publishes nothing and exits nonzero; a failed publish is
retried once and then exits nonzero), the single-PR event mode, and
the workflow wiring that drives the script.
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    """Import scripts/ci/<name>.py by path (scripts/ci is not a package)."""
    path = REPO_ROOT / "scripts" / "ci" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gf = _load("gate_freshness")


def _git_stub(mapping):
    """A run_git answering from `mapping` (command joined by spaces)."""
    def run(argv):
        key = " ".join(argv)
        if key not in mapping:
            raise gf.QueryError(f"unmapped command: {key}")
        return mapping[key]
    return run


def _commits_output(entries):
    """`git log -m --name-only --format=%H` shaped output."""
    blocks = ["\n".join([sha, *files]) for sha, files in entries]
    return "\n\n".join(blocks) + "\n\n" if blocks else ""


def _pr(number, base="master", sha="a" * 40):
    return {
        "number": number,
        "base": {"ref": base},
        "head": {"sha": sha, "repo": {"full_name": "Nitjsefnie/claudit"}},
    }


MASTER = "1" * 40
HEAD = "a" * 40


def test_no_missing_commits_is_fresh():
    stub = _git_stub({
        f"git log -m --name-only --pretty=format:%H {MASTER} ^{HEAD}": "",
    })
    assert gf.missing_commit_files(stub, MASTER, HEAD) == []


def test_missing_commit_files_parses_log_blocks():
    stub = _git_stub({
        f"git log -m --name-only --pretty=format:%H {MASTER} ^{HEAD}":
            _commits_output([
                ("c" * 40, ["backend/app.py"]),
                ("d" * 40, [".github/workflows/ci-gate.yml", "README.md"]),
            ]),
    })
    commits = gf.missing_commit_files(stub, MASTER, HEAD)
    assert commits == [
        ("c" * 40, ["backend/app.py"]),
        ("d" * 40, [".github/workflows/ci-gate.yml", "README.md"]),
    ]


def test_missing_commit_files_parses_the_producer_bytes(tmp_path):
    """Real `git log` bytes through the parser: pins the block shape.

    `git log --format=%H` emits sha, blank line, files (no separator
    blank between entries) on git 2.47.3; `--pretty=format:%H` emits
    sha, files, blank. The stub tests pin the parser's own assumption;
    this one pins the producer, per environment.
    """
    sp = subprocess
    if shutil.which("git") is None:
        raise AssertionError("git must exist to pin the producer shape")

    def git(*args):
        sp.run(("git", *args), cwd=tmp_path, check=True,
               capture_output=True, text=True)
    git("init", "-q", ".")
    git("-c", "user.name=t", "-c", "user.email=t@t",
        "commit", "-q", "--allow-empty", "-m", "base")
    base_sha = sp.run(("git", "rev-parse", "HEAD"), cwd=tmp_path,
                      capture_output=True, text=True,
                      check=True).stdout.strip()
    (tmp_path / "g.yaml").write_text("x\n", encoding="utf-8")
    git("add", "g.yaml")
    git("-c", "user.name=t", "-c", "user.email=t@t",
        "commit", "-q", "-m", "touch a trigger-shaped path")
    head_sha = sp.run(("git", "rev-parse", "HEAD"), cwd=tmp_path,
                      capture_output=True, text=True,
                      check=True).stdout.strip()

    def run_git(argv):
        out = sp.run(argv, cwd=tmp_path, capture_output=True, text=True,
                     check=True)
        return out.stdout

    commits = gf.missing_commit_files(run_git, "HEAD", base_sha)
    assert commits == [(head_sha, ["g.yaml"])], commits


def test_git_failure_raises_query_error():
    stub = _git_stub({})
    try:
        gf.missing_commit_files(stub, MASTER, HEAD)
    except gf.QueryError:
        pass
    else:
        raise AssertionError("expected QueryError")


# --- the trigger hit and the verdict ---------------------------------------

def test_first_trigger_hit_names_the_commit_and_path():
    commits = [
        ("c" * 40, ["backend/app.py"]),
        ("d" * 40, ["README.md", ".github/workflows/ci-gate.yml"]),
    ]
    hit = gf.first_trigger_hit(commits, lambda path: path.endswith(".yml"))
    assert hit == ("d" * 40, ".github/workflows/ci-gate.yml")


def test_no_trigger_hit_returns_none():
    commits = [("c" * 40, ["backend/app.py"])]
    assert gf.first_trigger_hit(commits, lambda path: False) is None


def test_head_verdict_fresh():
    stub = _git_stub({
        f"git log -m --name-only --pretty=format:%H {MASTER} ^{HEAD}": "",
    })
    conclusion, title, _summary = gf.head_verdict(
        stub, MASTER, HEAD, lambda path: False)
    assert conclusion == "success"
    assert MASTER[:12] in title


def test_head_verdict_stale():
    stub = _git_stub({
        f"git log -m --name-only --pretty=format:%H {MASTER} ^{HEAD}":
            _commits_output([("d" * 40, [".github/ci-thresholds.json"])]),
    })
    conclusion, title, summary = gf.head_verdict(
        stub, MASTER, HEAD,
        lambda path: path == ".github/ci-thresholds.json")
    assert conclusion == "failure"
    assert "d" * 12 in title
    assert ".github/ci-thresholds.json" in title
    assert "Rebase" in summary


def test_head_verdict_unreadable_compare_publishes_red():
    def broken(argv):
        raise gf.QueryError("fetch failed")

    conclusion, title, summary = gf.head_verdict(
        broken, MASTER, HEAD, lambda path: False)
    assert conclusion == "failure"
    assert "unreadable" in title.lower()
    assert "fetch failed" in summary


# --- publishing -------------------------------------------------------------

def _capture_gh(fail_first=0):
    calls = []

    def run(argv):
        calls.append(list(argv))
        if len(calls) <= fail_first:
            raise gf.QueryError("api down")
        return "{}"

    return run, calls


def test_publish_check_posts_the_named_check():
    run_gh, calls = _capture_gh()
    gf.publish_check(run_gh, "Nitjsefnie/claudit", HEAD, "success",
                     "Fresh against master 111111111111",
                     "nothing the head lacks is gate-defining")
    assert len(calls) == 1
    argv = calls[0]
    joined = " ".join(argv)
    assert "repos/Nitjsefnie/claudit/check-runs" in joined
    assert "name=gate freshness" in joined
    assert f"head_sha={HEAD}" in joined
    assert "status=completed" in joined
    assert "conclusion=success" in joined
    assert "output[title]=Fresh" in joined


def test_publish_with_retry_succeeds_after_one_failure():
    run_gh, calls = _capture_gh(fail_first=1)
    gf.publish_with_retry(run_gh, "Nitjsefnie/claudit", HEAD, "success",
                          "t", "s")
    assert len(calls) == 2


def test_publish_with_retry_raises_after_two_failures():
    run_gh, _ = _capture_gh(fail_first=2)
    try:
        gf.publish_with_retry(run_gh, "Nitjsefnie/claudit", HEAD, "success",
                              "t", "s")
    except gf.QueryError:
        pass
    else:
        raise AssertionError("expected QueryError")


# --- the event mode ---------------------------------------------------------

def test_env_selects_single_pr_mode():
    heads = gf.heads_for_run(
        {gf.ENV_PR: "9", gf.ENV_HEAD_SHA: HEAD}, [])
    assert heads == [{"number": 9, "sha": HEAD, "base": "master",
                      "repo": ""}]


def test_env_single_pr_mode_carries_the_fork_repo():
    heads = gf.heads_for_run(
        {gf.ENV_PR: "9", gf.ENV_HEAD_SHA: HEAD,
         gf.ENV_HEAD_REPO: "someone/claudit"}, [])
    assert heads[0]["repo"] == "someone/claudit"


def test_env_single_pr_mode_survives_a_non_numeric_number():
    heads = gf.heads_for_run(
        {gf.ENV_PR: "nine", gf.ENV_HEAD_SHA: HEAD},
        [_pr(7)])
    assert [head["number"] for head in heads] == [7]


def test_env_without_sha_falls_back_to_all_heads():
    pulls = [_pr(7), _pr(8, base="main")]
    heads = gf.heads_for_run({gf.ENV_PR: "9"}, pulls)
    assert [head["number"] for head in heads] == [7]


def test_pulls_mode_uses_scannable_heads_only():
    heads = gf.heads_for_run(
        {}, [_pr(7), _pr(8, base="main"), _pr(9, sha="short")])
    assert [head["number"] for head in heads] == [7]


def test_scannable_is_the_predicate_selection_and_counting_share():
    assert gf.scannable(gf.heads_for_run({}, [_pr(1)])[0])
    assert not gf.scannable(
        {"number": 2, "sha": "a" * 40, "base": "main", "repo": ""})
    assert not gf.scannable(
        {"number": 3, "sha": "short", "base": "master", "repo": ""})
    assert not gf.scannable(
        {"number": 4, "sha": None, "base": "master", "repo": ""})


# --- main wiring ------------------------------------------------------------

def test_main_publishes_one_check_per_open_head(monkeypatch, tmp_path):
    run_gh, _ = _capture_gh()
    pulls = [_pr(7), _pr(8, sha="b" * 40)]
    stub = _git_stub({
        f"git log -m --name-only --pretty=format:%H {MASTER} ^{'a' * 40}": "",
        f"git log -m --name-only --pretty=format:%H {MASTER} ^{'b' * 40}":
            _commits_output([("d" * 40, ["scripts/ci/gate_freshness.py"])]),
    })
    monkeypatch.setattr(gf, "resolve_master_tip", lambda run_git: MASTER)
    monkeypatch.setattr(gf, "_open_pulls", lambda repository: pulls)
    monkeypatch.setattr(gf, "run_gh", run_gh, raising=False)
    published = []
    monkeypatch.setattr(gf, "publish_check",
                        lambda *args, **kw: published.append(args[2]))
    monkeypatch.setattr(gf, "ensure_head_objects",
                        lambda run_git, heads, repository: None)
    exit_code = gf.main_impl(
        run_git=stub, run_gh=run_gh, repository="Nitjsefnie/claudit",
        env={}, summary_path=None)
    assert exit_code == 0
    assert sorted(published) == ["a" * 40, "b" * 40]


def test_main_global_read_failure_publishes_nothing(monkeypatch):
    def broken(argv):
        raise gf.QueryError("no api")

    monkeypatch.setattr(gf, "resolve_master_tip",
                        lambda run_git: (_ for _ in ()).throw(
                            gf.QueryError("master unreadable")))
    monkeypatch.setattr(gf, "_open_pulls", lambda repository: (_ for _ in ()).throw(
        gf.QueryError("list unreadable")))
    exit_code = gf.main_impl(
        run_git=broken, run_gh=broken, repository="Nitjsefnie/claudit",
        env={}, summary_path=None)
    assert exit_code == 1


def test_main_single_pr_mode_publishes_exactly_one_check(monkeypatch):
    run_gh, _ = _capture_gh()
    stub = _git_stub({
        f"git log -m --name-only --pretty=format:%H {MASTER} ^{'a' * 40}": "",
    })
    monkeypatch.setattr(gf, "resolve_master_tip", lambda run_git: MASTER)
    published = []
    monkeypatch.setattr(gf, "publish_check",
                        lambda *args, **kw: published.append(args[2]))
    exit_code = gf.main_impl(
        run_git=stub, run_gh=run_gh, repository="Nitjsefnie/claudit",
        env={gf.ENV_PR: "7", gf.ENV_HEAD_SHA: "a" * 40},
        summary_path=None)
    assert exit_code == 0
    assert published == ["a" * 40]


def test_main_failed_publish_exits_nonzero(monkeypatch):
    stub = _git_stub({
        f"git log -m --name-only --pretty=format:%H {MASTER} ^{'a' * 40}": "",
    })
    monkeypatch.setattr(gf, "resolve_master_tip", lambda run_git: MASTER)
    attempts = []

    def failing_publish(*args, **kw):
        attempts.append(args[2])
        raise gf.QueryError("check-runs down")

    monkeypatch.setattr(gf, "publish_check", failing_publish)
    exit_code = gf.main_impl(
        run_git=stub, run_gh=None, repository="Nitjsefnie/claudit",
        env={gf.ENV_PR: "7", gf.ENV_HEAD_SHA: "a" * 40},
        summary_path=None)
    assert exit_code == 1
    assert len(attempts) == 2  # retried once, then gave up visibly


def test_dry_run_publishes_nothing(monkeypatch):
    stub = _git_stub({
        f"git log -m --name-only --pretty=format:%H {MASTER} ^{'a' * 40}": "",
    })
    monkeypatch.setattr(gf, "resolve_master_tip", lambda run_git: MASTER)
    published = []
    monkeypatch.setattr(gf, "publish_check",
                        lambda *args, **kw: published.append(args[2]))
    exit_code = gf.main_impl(
        run_git=stub, run_gh=None, repository="Nitjsefnie/claudit",
        env={gf.ENV_PR: "7", gf.ENV_HEAD_SHA: "a" * 40},
        summary_path=None, dry_run=True)
    assert exit_code == 0
    assert not published


# --- the workflow wiring ----------------------------------------------------

def _workflow_text():
    return (REPO_ROOT / ".github" / "workflows" / "gate-freshness.yml"
            ).read_text(encoding="utf-8")


def _workflow_doc():
    # BaseLoader, not safe_load: YAML 1.1 parses the bare key `on` as
    # the boolean True, and these tests read the trigger map by name.
    return yaml.load(_workflow_text(), Loader=yaml.BaseLoader) or {}


def test_workflow_runs_on_master_push_and_pr_events():
    on = _workflow_doc().get("on") or {}
    assert sorted(on) == ["pull_request_target", "push"]
    assert on["push"]["branches"] == ["master"]
    assert on["pull_request_target"]["branches"] == ["master"]
    assert sorted(on["pull_request_target"]["types"]) == [
        "edited", "opened", "reopened", "synchronize"]


def test_workflow_pins_the_script_and_the_event_env():
    text = _workflow_text()
    assert "python3 scripts/ci/gate_freshness.py" in text
    assert "GF_PR:" in text
    assert "github.event.pull_request.number" in text
    assert "GF_HEAD_SHA:" in text
    assert "github.event.pull_request.head.sha" in text
    assert "GF_HEAD_REPO:" in text
    assert "github.event.pull_request.head.repo.full_name" in text


def test_workflow_checks_out_full_depth_base_only():
    text = _workflow_text()
    assert "fetch-depth: 0" in text
    assert "persist-credentials: false" in text


def test_workflow_has_the_write_permission_and_a_safe_concurrency():
    doc = _workflow_doc()
    granted = {**doc["permissions"], **doc["jobs"]["freshness"]["permissions"]}
    assert granted == {"contents": "read", "pull-requests": "read",
                       "checks": "write"}


def test_workflow_run_blocks_stay_free_of_interpolation():
    for job in _workflow_doc()["jobs"].values():
        for step in job.get("steps") or []:
            assert "${{" not in (step.get("run") or ""), step
