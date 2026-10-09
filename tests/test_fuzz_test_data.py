"""SV-TEST-DATA's fuzz half: scripts/ci/fuzz_test_data.py under test.

Light, by design: argument handling, entry-shape validity, and the
determinism of a seeded iteration. No test here runs the real suite —
`run_suite` is monkeypatched — and no test touches the tree's own
src/pricing.json: every tree is synthetic, under tmp_path. The
full-suite path is smoke-verified, not unit-tested.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from tests.refresh_fixture_builders import RATES_A, RATES_B, seed_doc


from backend import pricing

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ci" / "fuzz_test_data.py"

RATE_FIELDS = pricing.RATE_FIELDS
DOC_MAX_STAMP = "2026-06-01T00:00:00Z"


def _load():
    spec = importlib.util.spec_from_file_location("fuzz_test_data", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["fuzz_test_data"] = module
    spec.loader.exec_module(module)
    return module


fuzz_module = _load()


def _seed_doc() -> dict:
    """One two-entry dated model row, one null-only model row, one
    dated provider row carrying a schedule."""
    return seed_doc(
        models={
            "acme/acme-9": [
                {"from": None, **RATES_A},
                {"from": DOC_MAX_STAMP, **RATES_B},
            ],
            "free/acme-0": [{"from": None, **RATES_A}],
        },
        providers={
            "acme/acme-9": {
                "HostCo": [{"from": "2026-01-01T00:00:00Z", **RATES_A,
                            "schedule": [{"days": ["sunday"],
                                          "start": 2200, "end": 200,
                                          "rates": RATES_B}]}],
            },
        },
        fetched=DOC_MAX_STAMP,
    )


def _seed_tree(tmp_path: Path) -> tuple[Path, Path, str]:
    """A synthetic repo root whose src/pricing.json holds the seed doc."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    pricing_path = repo / "src" / "pricing.json"
    pristine = json.dumps(_seed_doc(), indent=2, sort_keys=True) + "\n"
    pricing_path.write_text(pristine, encoding="utf-8")
    return repo, pricing_path, pristine


def _fake_restore(pristine: str, pricing_path: Path):
    """The restore step's stand-in: the pristine document again."""
    def restore(_repo_root: Path) -> None:
        pricing_path.write_text(pristine, encoding="utf-8")
    return restore


def _rows_of(doc: dict) -> dict[str, list]:
    """Every rate row keyed as the fuzzer names it in its results."""
    rows = dict(doc["models"])
    for model, hosts in doc["providers"].items():
        rows.update({f"{model} via {host}": entries
                     for host, entries in hosts.items()})
    return rows


def _instant(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _run_one(monkeypatch, tmp_path: Path, base_seed: int = 7,
             iteration: int = 0, code: int = 0,
             output: str = "suite ok") -> tuple[dict, dict, dict, str]:
    """Seed a tree, run ONE fuzz iteration against it with the suite and
    the restore step mocked, and return (result, perturbed, original,
    pristine text)."""
    repo, pricing_path, pristine = _seed_tree(tmp_path)
    monkeypatch.setattr(fuzz_module, "restore_baseline",
                        _fake_restore(pristine, pricing_path))
    monkeypatch.setattr(fuzz_module, "run_suite",
                        lambda _root: (code, output))
    result = fuzz_module.fuzz_iteration(repo, iteration, base_seed, tmp_path)
    perturbed = json.loads(pricing_path.read_text(encoding="utf-8"))
    return result, perturbed, _seed_doc(), pristine


def test_the_appended_entry_has_five_fields_a_stamp_and_a_note(
        monkeypatch, tmp_path):
    result, perturbed, _original, _pristine = _run_one(monkeypatch, tmp_path)
    assert result["ok"] is True
    assert result["rows_touched"] == len(result["keys"])
    for key in result["keys"]:
        appended = _rows_of(perturbed)[key][-1]
        assert set(appended) == {"from", "note", *RATE_FIELDS}
        assert appended["note"] == fuzz_module.NOTE


def test_every_field_value_comes_from_the_pool(monkeypatch, tmp_path):
    result, perturbed, _original, _pristine = _run_one(monkeypatch, tmp_path)
    for key in result["keys"]:
        appended = _rows_of(perturbed)[key][-1]
        for field in RATE_FIELDS:
            assert appended[field] in fuzz_module.VALUE_POOL


def test_the_fields_are_drawn_independently(monkeypatch, tmp_path):
    """Ten seeded iterations against the same tree do not produce one
    fixed five-field draw: the tuple of values varies, and no field is
    glued to another's value across the scan."""
    repo, pricing_path, pristine = _seed_tree(tmp_path)
    monkeypatch.setattr(fuzz_module, "restore_baseline",
                        _fake_restore(pristine, pricing_path))
    monkeypatch.setattr(fuzz_module, "run_suite",
                        lambda _root: (0, "suite ok"))
    draws = []
    for iteration in range(10):
        fuzz_module.fuzz_iteration(repo, iteration, 7, tmp_path)
        doc = json.loads(pricing_path.read_text(encoding="utf-8"))
        # The row subset is random per (seed, iteration); scan every
        # row's last entry and keep the ones this harness appended.
        for entries in _rows_of(doc).values():
            entry = entries[-1]
            if entry.get("note") == fuzz_module.NOTE:
                draws.append(tuple(entry[f] for f in RATE_FIELDS))
    assert len(set(draws)) >= 8
    # No two fields always move together: gluing the five draws into one
    # would make two columns identical.
    columns = list(zip(*draws, strict=True))
    assert len(set(columns)) == len(columns)


def test_the_stamp_is_one_second_after_the_row_floor(monkeypatch, tmp_path):
    """The appended instant is the row's newest real instant + 1s; a
    row whose only entry is null-from floors at the DOCUMENT's newest
    real instant instead."""
    result, perturbed, original, _pristine = _run_one(
        monkeypatch, tmp_path, base_seed=7, iteration=3)
    doc_max = _instant(DOC_MAX_STAMP)
    for key in result["keys"]:
        prior = _rows_of(original)[key][-1]
        floor = (_instant(prior["from"]) if prior["from"] is not None
                 else doc_max)
        appended = _rows_of(perturbed)[key][-1]
        assert _instant(appended["from"]) == floor + timedelta(seconds=1)


def test_offset_spellings_carry_the_same_later_instant():
    """A quarter of the appended stamps spell their instant with a
    ±HH:MM offset (issue #264's normalisation): the text parses, and
    the instant is the row floor + 1s — the SAME instant the Z spelling
    would carry, never the offset-local wall time mislabeled."""
    entries = [{"from": DOC_MAX_STAMP, **RATES_A}]
    floor = _instant(DOC_MAX_STAMP)
    offset_seen = 0
    for iteration in range(60):
        entry = fuzz_module._appended_entry(  # pylint: disable=protected-access
            entries, floor, fuzz_module.iteration_rng(7, iteration))
        text = entry["from"]
        assert _instant(text) == floor + timedelta(seconds=1)
        if not text.endswith("Z"):
            assert re.fullmatch(r".*[+-][0-9]{2}:[0-9]{2}", text), text
            offset_seen += 1
    assert offset_seen > 0


def test_an_offset_spelled_predecessor_bounds_the_appended_instant():
    """(issue #264) The row floor is parsed from whatever spelling the
    predecessor carries. When that spelling is an offset, the appended
    stamp is normalised to UTC BEFORE the literal-Z format renders it:
    strftime writes the datetime's own wall time, so a raw strftime
    spells offset-local wall time as Z — hours early — and the loader
    refuses the document."""
    entries = [{"from": "2026-10-01T12:00:00-05:00", **RATES_A}]
    floor = _instant(entries[0]["from"])  # the instant 17:00:00Z
    for iteration in range(20):
        entry = fuzz_module._appended_entry(  # pylint: disable=protected-access
            entries, floor, fuzz_module.iteration_rng(9, iteration))
        assert _instant(entry["from"]) == floor + timedelta(seconds=1)


def test_an_offset_spelled_document_fuzzes_cleanly(monkeypatch, tmp_path):
    """End to end: a document whose newest real stamp is spelled with a
    ±HH:MM offset perturbs cleanly — every iteration validates through
    the loader, which refuses a stamp at or before its predecessor."""
    doc = _seed_doc()
    doc["models"]["acme/acme-9"][-1]["from"] = "2026-10-01T12:00:00-05:00"
    repo, pricing_path, _pristine = _seed_tree(tmp_path)
    pristine = json.dumps(doc, indent=2, sort_keys=True) + "\n"
    pricing_path.write_text(pristine, encoding="utf-8")
    monkeypatch.setattr(fuzz_module, "restore_baseline",
                        _fake_restore(pristine, pricing_path))
    monkeypatch.setattr(fuzz_module, "run_suite",
                        lambda _root: (0, "suite ok"))
    for iteration in range(8):
        result = fuzz_module.fuzz_iteration(repo, iteration, 7, tmp_path)
        assert result["ok"] is True


def test_a_nonempty_subset_is_touched_and_the_rest_is_untouched(
        monkeypatch, tmp_path):
    result, perturbed, original, _pristine = _run_one(monkeypatch, tmp_path)
    original_rows = _rows_of(original)
    existing_touched = set(result["keys"]) & original_rows.keys()
    assert 1 <= len(existing_touched) <= len(original_rows)
    added_rows = set(_rows_of(perturbed)) - original_rows.keys()
    assert len(added_rows) <= 1
    assert added_rows <= set(result["keys"])
    assert all(fuzz_module.RESERVED_NAMESPACE in key for key in added_rows)
    assert result["rows_touched"] == len(result["keys"])
    for key, entries in _rows_of(perturbed).items():
        if key not in original_rows:
            assert len(entries) == 1
        elif key in result["keys"]:
            assert len(entries) == len(original_rows[key]) + 1
            assert entries[:-1] == original_rows[key]
        else:
            assert entries == original_rows[key]


def test_the_same_seed_reproduces_the_iteration_byte_for_byte(
        monkeypatch, tmp_path):
    """Same tree content, same (seed, iteration) → byte-identical
    perturbed document: the row subset, the five pool draws and the
    stamp spelling all derive from the iteration's generator."""
    texts = []
    for name in ("one", "two"):
        tree = tmp_path / name
        tree.mkdir()
        repo, pricing_path, pristine = _seed_tree(tree)
        monkeypatch.setattr(fuzz_module, "restore_baseline",
                            _fake_restore(pristine, pricing_path))
        monkeypatch.setattr(fuzz_module, "run_suite",
                            lambda _root: (0, "suite ok"))
        fuzz_module.fuzz_iteration(repo, 3, 42, tree)
        texts.append(pricing_path.read_text(encoding="utf-8"))
    assert texts[0] == texts[1]


def test_a_document_with_no_rate_rows_is_refused(monkeypatch, tmp_path):
    repo, pricing_path, _unused = _seed_tree(tmp_path)
    empty = json.dumps({
        "models": {}, "providers": {},
        "provider_rates_fetched": DOC_MAX_STAMP,
        "long_context_meters": [],
    })
    monkeypatch.setattr(fuzz_module, "restore_baseline",
                        _fake_restore(empty, pricing_path))
    with pytest.raises(ValueError, match="no rate rows"):
        fuzz_module.fuzz_iteration(repo, 0, 7, tmp_path)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def _git_commit(repo: Path, message: str) -> None:
    _git(repo, "-c", "user.name=fuzz test", "-c",
         "user.email=fuzz@localhost", "commit", "-q", "-m", message)


@pytest.fixture(name="git_repo")
def _git_repo_fixture(tmp_path: Path) -> Path:
    """A disposable git checkout shaped like the tree: src/pricing.json
    committed, the tree clean. The refusal, cleanup and shard tests run
    against it with the REAL per-iteration restore and clean-check."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src" / "pricing.json").write_text(
        json.dumps(_seed_doc(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    (repo / "tests" / "test_placeholder.py").write_text("", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git_commit(repo, "baseline")
    return repo


def test_a_dirty_pricing_document_refuses_the_run(
        monkeypatch, git_repo, tmp_path):
    """The run refuses to start over a pricing document that carries
    local changes: the per-iteration restore is `git checkout --`,
    which would silently discard them. Nothing runs, nothing moves."""
    pricing_path = git_repo / "src" / "pricing.json"
    dirty = pricing_path.read_text(encoding="utf-8") + "\n<!-- edit -->\n"
    pricing_path.write_text(dirty, encoding="utf-8")

    def boom(_root):
        raise AssertionError("the suite must not run over a dirty baseline")

    monkeypatch.setattr(fuzz_module, "run_suite", boom)
    with pytest.raises(SystemExit) as exit_info:
        fuzz_module.main(["--iterations", "1", "--seed", "7",
                          "--artifact-dir", str(tmp_path)],
                         repo_root=git_repo)
    assert exit_info.value.code != 0
    assert "local changes" in str(exit_info.value.code)
    assert pricing_path.read_text(encoding="utf-8") == dirty


def test_the_tree_is_restored_after_a_failing_run(
        monkeypatch, git_repo, tmp_path):
    """A failing run leaves the tree exactly as it was found: the
    failure artifact is saved first, the baseline is restored in a
    `finally` after it."""
    pricing_path = git_repo / "src" / "pricing.json"
    baseline = pricing_path.read_text(encoding="utf-8")
    monkeypatch.setattr(
        fuzz_module, "run_suite",
        lambda _root: (1, "FAILED tests/test_x.py::test_y\n"))
    exit_code = fuzz_module.main(
        ["--iterations", "1", "--seed", "7",
         "--artifact-dir", str(tmp_path)], repo_root=git_repo)
    assert exit_code == 1
    artifact = tmp_path / "fuzz-fail-0.json"
    assert artifact.exists()
    assert artifact.read_text(encoding="utf-8") != baseline
    assert pricing_path.read_text(encoding="utf-8") == baseline


def test_the_tree_is_restored_after_a_green_run(
        monkeypatch, git_repo, tmp_path):
    """A green run also leaves the tree as it was found — the last
    iteration's perturbed document does not outlive the run."""
    pricing_path = git_repo / "src" / "pricing.json"
    baseline = pricing_path.read_text(encoding="utf-8")
    monkeypatch.setattr(fuzz_module, "run_suite",
                        lambda _root: (0, "suite ok"))
    exit_code = fuzz_module.main(
        ["--iterations", "2", "--seed", "7",
         "--artifact-dir", str(tmp_path)], repo_root=git_repo)
    assert exit_code == 0
    assert pricing_path.read_text(encoding="utf-8") == baseline


def test_a_shard_snapshot_carries_uncommitted_work(git_repo, tmp_path):
    """Each shard runs from a snapshot of the WORKING tree — committed
    and uncommitted content alike — as its own git repository: a
    HEAD-only clone would silently drop uncommitted tests or code
    edits from every shard and could report a false green over stale
    code."""
    probe = git_repo / "tests" / "test_probe.py"
    probe.write_text("def test_committed(): ...\n", encoding="utf-8")
    _git(git_repo, "add", "-A")
    _git_commit(git_repo, "probe")
    probe.write_text("def test_uncommitted(): ...\n", encoding="utf-8")
    fresh = git_repo / "tests" / "test_new.py"
    fresh.write_text("def test_new(): ...\n", encoding="utf-8")

    shard = tmp_path / "shard"
    # pylint: disable-next=protected-access
    fuzz_module._snapshot_tree(git_repo, shard)
    assert "def test_uncommitted" in (shard / "tests" / "test_probe.py"
                                      ).read_text(encoding="utf-8")
    assert (shard / "tests" / "test_new.py").exists()
    status = subprocess.run(["git", "-C", str(shard), "status",
                             "--porcelain"], capture_output=True, text=True,
                            check=True)
    assert status.stdout == ""


@pytest.mark.skipif(
    not hasattr(os, "mkfifo"),
    reason="no fifo on this platform; the sharding refusal path for "
    "non-regular entries is exercised on POSIX")
def test_an_ignored_fifo_does_not_break_the_snapshot(git_repo, tmp_path):
    """The snapshot carries exactly the files git knows about — tracked
    plus untracked, unignored — never walking ignored runtime paths, so
    a NONCOPYABLE artifact under an ignored directory (a live Unix
    socket in a real checkout; a fifo here, same shape) cannot break
    the copy and cannot reach a shard. Untracked, unignored files still
    reach it, and the snapshot repository starts out clean."""
    (git_repo / ".gitignore").write_text("runtime/\n", encoding="utf-8")
    runtime = git_repo / "runtime"
    runtime.mkdir()
    os.mkfifo(runtime / "live.sock")
    (runtime / "notes.txt").write_text("runtime scratch\n",
                                       encoding="utf-8")
    reachable = git_repo / "scratch.txt"
    reachable.write_text("reaches the shard\n", encoding="utf-8")
    _git(git_repo, "add", "-A")  # the .gitignore keeps runtime/ out
    _git_commit(git_repo, "gitignore")

    shard = tmp_path / "shard"
    # pylint: disable-next=protected-access
    fuzz_module._snapshot_tree(git_repo, shard)
    assert not (shard / "runtime").exists()
    assert (shard / "scratch.txt").read_text(encoding="utf-8") == \
        "reaches the shard\n"
    status = subprocess.run(["git", "-C", str(shard), "status",
                             "--porcelain"], capture_output=True, text=True,
                            check=True)
    assert status.stdout == ""


def test_a_nested_ignored_test_dir_reaches_the_shard(git_repo, tmp_path):
    """A nested test directory this deny-by-default .gitignore shadows
    is git-ignored yet pytest COLLECTS it, so the shard must carry it
    from disk: a shard without it runs a smaller suite than the
    sequential run and reports a false green."""
    (git_repo / ".gitignore").write_text(
        "*\n!.gitignore\n!/tests/\n/tests/*\n!/tests/*.py\n",
        encoding="utf-8")
    _git(git_repo, "add", "-A")
    _git_commit(git_repo, "gitignore")
    nested = git_repo / "tests" / "new_case" / "test_feature.py"
    nested.parent.mkdir(parents=True)
    nested.write_text("def test_nested(): ...\n", encoding="utf-8")
    # The precondition: git genuinely cannot see the nested test.
    listed = subprocess.run(
        ["git", "-C", str(git_repo), "ls-files", "-co",
         "--exclude-standard", "tests/"],
        capture_output=True, text=True, check=True).stdout.splitlines()
    assert "tests/new_case/test_feature.py" not in listed

    shard = tmp_path / "shard"
    # pylint: disable-next=protected-access
    fuzz_module._snapshot_tree(git_repo, shard)
    got = shard / "tests" / "new_case" / "test_feature.py"
    assert got.read_text(encoding="utf-8") == "def test_nested(): ...\n"


@pytest.mark.skipif(
    not hasattr(os, "mkfifo"),
    reason="no fifo on this platform; the sharding refusal path for "
    "non-regular entries is exercised on POSIX")
def test_a_fifo_under_tests_refuses_sharding_but_not_sequential(
        monkeypatch, git_repo, tmp_path):
    """A non-regular file under tests/ cannot be snapshotted, so the
    population guarantee would fail silently — the sharded run refuses
    LOUDLY instead, naming the path. The sequential run never
    snapshots, so it may still proceed."""
    under_tests = git_repo / "tests" / "artifacts"
    under_tests.mkdir(parents=True)
    os.mkfifo(under_tests / "pipe")
    monkeypatch.setattr(fuzz_module, "run_suite",
                        lambda _root: (0, "suite ok"))

    with pytest.raises(SystemExit) as exit_info:
        fuzz_module.main(["--iterations", "1", "--jobs", "2", "--seed",
                          "7", "--artifact-dir", str(tmp_path)],
                         repo_root=git_repo)
    assert "not a regular file" in str(exit_info.value.code)
    assert "tests" in str(exit_info.value.code)

    exit_code = fuzz_module.main(
        ["--iterations", "1", "--jobs", "1", "--seed", "7",
         "--artifact-dir", str(tmp_path)], repo_root=git_repo)
    assert exit_code == 0


def test_shard_refusal_names_repo_relative_posix_path(tmp_path):
    repo_root = tmp_path / "repo"
    refused_path = repo_root / "tests" / "linked_test.py"
    # pylint: disable-next=protected-access
    message = fuzz_module._shard_refusal(refused_path, repo_root)
    assert message.startswith(
        "fuzz: refusing to shard: tests/linked_test.py is not a regular file")


def test_shard_refusal_uses_absolute_posix_path_outside_repo(tmp_path):
    repo_root = tmp_path / "repo"
    refused_path = tmp_path / "outside.py"
    # pylint: disable-next=protected-access
    message = fuzz_module._shard_refusal(refused_path, repo_root)
    assert message.startswith(
        f"fuzz: refusing to shard: {refused_path.absolute().as_posix()} "
        "is not a regular file")


def test_a_file_symlink_under_tests_refuses_sharding(
        monkeypatch, git_repo, tmp_path):
    """A file SYMLINK under tests/ is a non-regular entry: copying it
    into the shard either collides with the git-known pass or carries a
    symlink whose target was omitted — a silent population gap. It
    refuses the sharded run loudly, naming the path; a dangling target
    is refused the same way, and sequential proceeds."""
    link = git_repo / "tests" / "linked_test.py"
    os.symlink("nowhere/test_target.py", link)  # dangling: same refusal
    monkeypatch.setattr(fuzz_module, "run_suite",
                        lambda _root: (0, "suite ok"))

    with pytest.raises(SystemExit) as exit_info:
        fuzz_module.main(["--iterations", "1", "--jobs", "2", "--seed",
                          "7", "--artifact-dir", str(tmp_path)],
                         repo_root=git_repo)
    assert "not a regular file" in str(exit_info.value.code)
    assert "tests/linked_test.py" in str(exit_info.value.code)

    exit_code = fuzz_module.main(
        ["--iterations", "1", "--jobs", "1", "--seed", "7",
         "--artifact-dir", str(tmp_path)], repo_root=git_repo)
    assert exit_code == 0


def test_the_schedule_of_a_touched_provider_row_is_untouched(
        monkeypatch, tmp_path):
    """Appending to a scheduled provider row appends a PLAIN entry: the
    row's existing entries — its schedule among them — are untouched,
    and the appended entry carries no schedule of its own. The touched
    provider rows are identified from the ORIGINAL document, and the
    seed is chosen so at least one IS touched: the assertions cannot
    silently run zero times."""
    original = _seed_doc()
    provider_keys = {f"{model} via {host}"
                     for model, hosts in original["providers"].items()
                     for host in hosts}
    # The subset is a pure function of (seed, iteration), so a seed
    # touching the provider row can be searched without running.
    # pylint: disable-next=protected-access
    rows = fuzz_module._rows(original)
    seed = next(
        candidate for candidate in range(40)
        if provider_keys
        & {key for key, _entries in fuzz_module._choose_rows(  # pylint: disable=protected-access
            rows, fuzz_module.iteration_rng(candidate, 0))})
    result, perturbed, _original, _pristine = _run_one(
        monkeypatch, tmp_path, base_seed=seed, iteration=0)
    touched = provider_keys & set(result["keys"])
    assert touched
    for key in touched:
        model, host = key.split(" via ", 1)
        assert perturbed["providers"][model][host][:-1] == \
            original["providers"][model][host]
        appended = perturbed["providers"][model][host][-1]
        assert "schedule" not in appended
        assert set(appended) == {"from", "note", *RATE_FIELDS}


def test_sequential_main_runs_iterations_and_prints_a_summary(
        monkeypatch, git_repo, tmp_path, capsys):
    suite_calls, restore_calls = [], []

    def fake_suite(_root):
        suite_calls.append(_root)
        return 0, "suite ok"

    def counting_restore(root):
        real_restore(root)
        restore_calls.append(root)

    real_restore = fuzz_module.restore_baseline
    monkeypatch.setattr(fuzz_module, "restore_baseline", counting_restore)

    monkeypatch.setattr(fuzz_module, "restore_baseline", counting_restore)
    monkeypatch.setattr(fuzz_module, "run_suite", fake_suite)
    exit_code = fuzz_module.main(
        ["--iterations", "3", "--seed", "7",
         "--artifact-dir", str(tmp_path)], repo_root=git_repo)
    assert exit_code == 0
    assert len(suite_calls) == 3
    assert len(restore_calls) == 3
    out = capsys.readouterr().out
    assert "fuzz OK: 3 iterations green" in out
    assert "seed=7" in out
    assert "rows touched per iteration" in out
    # The tree is left as it was found: the final `finally` restores the
    # baseline the run recorded at start.
    pricing_path = git_repo / "src" / "pricing.json"
    doc = json.loads(pricing_path.read_text(encoding="utf-8"))
    assert doc == _seed_doc()


def test_a_failing_suite_saves_the_artifact_prints_and_exits_nonzero(
        monkeypatch, git_repo, tmp_path, capsys):
    failing = ("=============================== FAILURES =============\n"
               "FAILED tests/test_example.py::test_pinned - "
               "assert 9.0697 != 9.0698\n"
               "1 failed, 2408 passed in 4.2s\n")
    suite_calls = []
    monkeypatch.setattr(
        fuzz_module, "run_suite",
        lambda _root: (suite_calls.append(_root), (1, failing))[1])
    exit_code = fuzz_module.main(
        ["--iterations", "3", "--seed", "7",
         "--artifact-dir", str(tmp_path)], repo_root=git_repo)
    assert exit_code == 1
    # The run stopped at the first failing suite.
    assert len(suite_calls) == 1
    artifact = tmp_path / "fuzz-fail-0.json"
    assert artifact.exists()
    pricing_path = git_repo / "src" / "pricing.json"
    # The artifact carries the PERTURBED document, the tree the baseline.
    assert artifact.read_text(encoding="utf-8") != \
        pricing_path.read_text(encoding="utf-8")
    out = capsys.readouterr().out
    assert "iteration 0" in out
    assert "seed 7" in out
    assert "pytest tail" in out
    assert "FAILED tests/test_example.py::test_pinned" in out
    # The report names every row the failing iteration touched.
    touched = json.loads(artifact.read_text(encoding="utf-8"))
    baseline_doc = json.loads(
        pricing_path.read_text(encoding="utf-8"))
    for key in result_keys(touched, baseline_doc):
        assert key in out


def result_keys(touched: dict, pristine_doc: dict) -> list[str]:
    """The rows that gained an entry, read off the failing artifact."""
    original_rows = _rows_of(pristine_doc)
    return [key for key, entries in _rows_of(touched).items()
            if (key not in original_rows
                or len(entries) == len(original_rows[key]) + 1)]


def test_shards_partition_the_iterations_round_robin():
    # pylint: disable-next=protected-access
    shards = fuzz_module._shards(7, 3)
    covered = [first + n * step
               for first, step, count in shards
               for n in range(count)]
    assert sorted(covered) == list(range(7))
    assert [(s[0], s[1], s[2]) for s in shards] == \
        [(0, 3, 3), (1, 3, 2), (2, 3, 2)]
    # More shards than iterations: the trailing shards run nothing,
    # keeping their own first index.
    # pylint: disable-next=protected-access
    assert fuzz_module._shards(2, 5) == \
        [(0, 5, 1), (1, 5, 1), (2, 5, 0), (3, 5, 0), (4, 5, 0)]


class _FakeChild:
    """A Popen stand-in: an exit code and nothing else."""

    def __init__(self, code: int):
        self._code = code

    def wait(self) -> int:
        return self._code


def test_a_failing_shard_child_merges_instead_of_crashing(
        monkeypatch, tmp_path):
    """A shard child that ran to a failing suite exits 1 WITH its
    result file; the parent merges those results and reports — only a
    child that died WITHOUT one is a crash worth raising on."""
    result_file = tmp_path / "shard-result.json"
    result_file.write_text(json.dumps({
        "seed": 7,
        "results": [{"iteration": 1, "seed": 7, "ok": False,
                     "rows_touched": 2, "keys": ["a", "b"], "output": "x"}],
    }), encoding="utf-8")
    # pylint: disable-next=protected-access
    merged = fuzz_module._collect_shards(
        [(tmp_path, result_file, _FakeChild(1))])
    assert [r["iteration"] for r in merged] == [1]
    assert merged[0]["ok"] is False
    # A child that died without writing its file is still a crash.
    missing = tmp_path / "absent.json"
    with pytest.raises(RuntimeError, match="without a result file"):
        # pylint: disable-next=protected-access
        fuzz_module._collect_shards([(tmp_path, missing, _FakeChild(1))])
