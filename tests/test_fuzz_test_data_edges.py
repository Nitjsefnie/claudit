"""Focused coverage for reserved fuzz rows and shard crash cleanup."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

from backend import pricing

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ci" / "fuzz_test_data.py"
RESERVED_NAMESPACE = "zz-fuzz-local/"
RATE_FIELDS = pricing.RATE_FIELDS
RATES_A = {"fresh": 1.0, "create_5m": 1.25, "create_1h": 2.0,
           "read": 0.1, "output": 5.0}
RATES_B = {"fresh": 2.0, "create_5m": 2.5, "create_1h": 4.0,
           "read": 0.2, "output": 10.0}
FLOOR_STAMP = "2026-06-01T00:00:00Z"


def _load_fuzzer():
    spec = importlib.util.spec_from_file_location(
        "fuzz_test_data_edges", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["fuzz_test_data_edges"] = module
    spec.loader.exec_module(module)
    return module


def _load_sharding():
    """The sharding module, registered BEFORE the fuzzer loads so the
    fuzzer's own `import fuzz_sharding` resolves to this same object."""
    spec = importlib.util.spec_from_file_location(
        "fuzz_sharding", SCRIPT.parent / "fuzz_sharding.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["fuzz_sharding"] = module
    spec.loader.exec_module(module)
    return module


fuzz_sharding = _load_sharding()
fuzz_module = _load_fuzzer()


def _seed_document() -> dict:
    return {
        "models": {
            "acme/existing-9": [
                {"from": None, **RATES_A},
                {"from": FLOOR_STAMP, **RATES_B},
            ],
        },
        "providers": {
            "acme/existing-9": {
                "KnownHost": [{"from": FLOOR_STAMP, **RATES_B}],
            },
        },
        "provider_rates_fetched": FLOOR_STAMP,
        "openrouter": {"data_region": "global", "models": {}},
    }


def _all_provider_rows(doc: dict) -> set[tuple[str, str]]:
    return {(model, host) for model, hosts in doc["providers"].items()
            for host in hosts}


def _stamp(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _prepare_fuzz_tree(monkeypatch: pytest.MonkeyPatch,
                       tmp_path: Path) -> tuple[Path, Path, dict]:
    repo = tmp_path / "repo"
    pricing_path = repo / "src" / "pricing.json"
    pricing_path.parent.mkdir(parents=True)
    original = _seed_document()
    pristine = json.dumps(original, indent=2, sort_keys=True) + "\n"
    pricing_path.write_text(pristine, encoding="utf-8")

    def restore(_root: Path) -> None:
        pricing_path.write_text(pristine, encoding="utf-8")

    monkeypatch.setattr(fuzz_module, "restore_baseline", restore)
    monkeypatch.setattr(fuzz_module, "run_suite",
                        lambda _root: (0, "suite ok"))
    return repo, pricing_path, original


def _assert_new_model_row(doc: dict, key: str, result: dict) -> None:
    entry = doc["models"][key][0]
    assert key.startswith(RESERVED_NAMESPACE)
    assert entry["from"] is None
    assert set(entry) == {"from", "note", *RATE_FIELDS}
    assert key in result["keys"]


def _assert_new_host_row(doc: dict, row: tuple[str, str],
                         result: dict) -> None:
    model, host = row
    entry = doc["providers"][model][host][0]
    assert host.startswith(RESERVED_NAMESPACE)
    assert set(entry) == {"from", "note", *RATE_FIELDS}
    assert (entry["from"] is None
            or _stamp(entry["from"]) > _stamp(FLOOR_STAMP))
    assert f"{model} via {host}" in result["keys"]


def _check_seeded_iteration(repo: Path, pricing_path: Path, original: dict,
                            iteration: int) -> tuple[set[str], bool]:
    result = fuzz_module.fuzz_iteration(repo, iteration, 263, pricing_path.parent)
    doc = json.loads(pricing_path.read_text(encoding="utf-8"))
    new_models = set(doc["models"]) - original["models"].keys()
    new_hosts = _all_provider_rows(doc) - _all_provider_rows(original)
    assert len(new_models) + len(new_hosts) <= 1
    if new_models:
        _assert_new_model_row(doc, next(iter(new_models)), result)
    if new_hosts:
        _assert_new_host_row(doc, next(iter(new_hosts)), result)
    assert result["rows_touched"] == len(result["keys"])
    assert pricing.load_tables(doc)
    assert pricing_path.read_text(encoding="utf-8") == (
        json.dumps(doc, indent=2, sort_keys=True) + "\n")
    return ({"model"} if new_models else set()) | \
        ({"host"} if new_hosts else set()), bool(new_models or new_hosts)


def test_seeded_iterations_add_valid_reserved_model_or_host_rows(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """New rows are valid and use names isolated from live test models."""
    repo, pricing_path, original = _prepare_fuzz_tree(monkeypatch, tmp_path)
    seen_kinds: set[str] = set()
    iterations_with_new_rows = 0
    for iteration in range(100):
        kinds, added_row = _check_seeded_iteration(
            repo, pricing_path, original, iteration)
        seen_kinds.update(kinds)
        iterations_with_new_rows += added_row

    assert seen_kinds == {"model", "host"}
    assert 0 < iterations_with_new_rows < 100


class _OwnedChild:
    """A child whose wait call records whether the parent joined it."""

    def __init__(self, exit_code: int):
        self.exit_code = exit_code
        self.waited = False

    def wait(self) -> int:
        self.waited = True
        return self.exit_code


def test_a_crashed_shard_waits_for_all_owned_children_before_raising(
        tmp_path: Path) -> None:
    missing = tmp_path / "crashed-result.json"
    successful = tmp_path / "successful-result.json"
    successful.write_text(json.dumps({"results": []}), encoding="utf-8")
    crashed_child = _OwnedChild(2)
    remaining_child = _OwnedChild(0)

    with pytest.raises(RuntimeError, match="without a result file"):
        # pylint: disable-next=protected-access
        fuzz_module._collect_shards([
            (tmp_path / "shard-crashed", missing, crashed_child),
            (tmp_path / "shard-running", successful, remaining_child),
        ])

    assert crashed_child.waited
    assert remaining_child.waited


class _FakeShardChild:
    """A Popen stand-in recording its kill and wait traffic."""

    def __init__(self, pid: int, hang: bool = False):
        self.pid = pid
        self._hang = hang
        self.waits = 0

    def poll(self) -> int | None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        self.waits += 1
        if self._hang and self.waits == 1:
            raise subprocess.TimeoutExpired(cmd="shard",
                                            timeout=timeout or 0.0)
        return 0


def test_terminate_shards_signals_whole_process_groups(monkeypatch) -> None:
    """The interrupt teardown kills each shard's process GROUP, not the
    bare child: one killpg reaches the shard child and the suite child
    it is running together, so no orphan outlives the run to recreate
    the snapshot tree after its removal."""
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg",
                        lambda pid, sig: kills.append((pid, sig)))
    children = [_FakeShardChild(4242), _FakeShardChild(4243)]

    # pylint: disable-next=protected-access
    fuzz_sharding._terminate_shards(
        [(Path("/tmp/s0"), Path("/tmp/s0/r.json"), child)
         for child in children])

    assert kills == [(4242, signal.SIGTERM), (4243, signal.SIGTERM)]
    assert all(child.waits == 1 for child in children)


def test_terminate_shards_escalates_to_sigkill_when_a_child_hangs(
        monkeypatch) -> None:
    """A shard still alive after the grace window gets SIGKILL for its
    whole group and is then reaped; SIGTERM always went first."""
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg",
                        lambda pid, sig: kills.append((pid, sig)))
    child = _FakeShardChild(4244, hang=True)

    # pylint: disable-next=protected-access
    fuzz_sharding._terminate_shards(
        [(Path("/tmp/s0"), Path("/tmp/s0/r.json"), child)])

    assert kills == [(4244, signal.SIGTERM), (4244, signal.SIGKILL)]
    assert child.waits == 2


@pytest.mark.skipif(not hasattr(signal, "pthread_sigmask"),
                    reason="needs POSIX signal masking")
def test_the_interrupt_windows_block_the_interrupt_signals() -> None:
    """Spawning and teardown run with the interrupt signals blocked, so
    a handler cannot fire mid-spawn (leaving a spawned child unpending,
    unkillable) or mid-teardown (half-killing the tree)."""
    # pylint: disable-next=protected-access
    with fuzz_sharding._blocked_signals():
        current = signal.pthread_sigmask(signal.SIG_BLOCK, set())
        assert set(fuzz_sharding.interrupt_signals()) <= current
    current = signal.pthread_sigmask(signal.SIG_BLOCK, set())
    assert not current & set(fuzz_sharding.interrupt_signals())


def test_install_and_restore_round_trips_the_handlers() -> None:
    """install_interrupt_handlers takes over the interrupt signals and
    its restore callable puts every previous handler back exactly."""
    previous = {sig: signal.getsignal(sig)
                for sig in fuzz_sharding.interrupt_signals()}
    restore = fuzz_sharding.install_interrupt_handlers()
    try:
        for sig in fuzz_sharding.interrupt_signals():
            assert signal.getsignal(sig) != previous[sig]
    finally:
        restore()
    for sig, handler in previous.items():
        assert signal.getsignal(sig) == handler


@pytest.mark.parametrize("signame", ["SIGTERM", "SIGINT", "SIGHUP"])
@pytest.mark.skipif(os.name != "posix",
                    reason="delivers real OS signals")
def test_a_delivered_signal_exits_through_the_cleanup_path(
        signame: str) -> None:
    """A delivered SIGTERM/SIGINT/SIGHUP raises SystemExit(128 + signum)
    instead of the default sudden death, so main's `finally` — the
    baseline restore, the snapshot removal — runs (issue #331)."""
    signum = getattr(signal, signame)
    restore = fuzz_sharding.install_interrupt_handlers()
    try:
        with pytest.raises(SystemExit) as exit_info:
            os.kill(os.getpid(), signum)
        assert exit_info.value.code == 128 + signum
    finally:
        restore()


def _build_runnable_repo(repo: Path) -> None:
    """A synthetic tree the REAL fuzz script runs end to end: the seed
    document, one trivial test, and a MINIMAL loader stand-in for the
    one backend module it imports — the real pricing.py reads the live
    rate rows at import (SV-TEST-DATA bars the suite from depending on
    them), and loader semantics are pinned by the other tests. Each
    shard snapshot is runnable as-is."""
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src" / "pricing.json").write_text(
        json.dumps(_seed_document(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    (repo / "tests" / "test_placeholder.py").write_text(
        "def test_ok(): ...\n", encoding="utf-8")
    backend = repo / "backend"
    backend.mkdir()
    (backend / "__init__.py").write_text("", encoding="utf-8")
    # RATE_FIELDS is the real constant's value at run time.
    (backend / "pricing.py").write_text(
        f"RATE_FIELDS = {pricing.RATE_FIELDS!r}\n\n\n"
        "def load_tables(doc):\n    return doc\n",
        encoding="utf-8")
    scripts = repo / "scripts" / "ci"
    scripts.mkdir(parents=True)
    for name in ("fuzz_test_data.py", "fuzz_sharding.py"):
        shutil.copyfile(ROOT / "scripts" / "ci" / name, scripts / name)


def _git_commit_all(repo: Path) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=fuzz test",
         "-c", "user.email=fuzz@localhost", "commit", "-q", "-m",
         "baseline"], check=True)


def _procs_referencing(token: str) -> list[int]:
    """Live PIDs whose cmdline or cwd references `token` (Linux /proc;
    empty everywhere else). Zombie entries and races are skipped."""
    if not Path("/proc").is_dir():
        return []
    found: list[int] = []
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        try:
            cmdline = Path(entry.path, "cmdline").read_bytes()
            cwd = os.readlink(str(Path(entry.path, "cwd")))
        except OSError:
            continue
        if (token.encode() in cmdline
                or cwd == token or cwd.startswith(token + "/")):
            found.append(int(entry.name))
    return found


@pytest.mark.skipif(os.name != "posix",
                    reason="delivers a real SIGTERM to a real child tree")
def test_a_sigterm_during_a_sharded_run_removes_the_snapshots_and_shards(
        tmp_path: Path) -> None:
    """(issue #331) The contract, end to end: SIGTERM the sharded parent
    mid-run and its snapshot directory is REMOVED and no process of the
    run survives — where the default disposition killed the parent with
    the tree left on disk and both shard children running."""
    repo = tmp_path / "repo"
    _build_runnable_repo(repo)
    _git_commit_all(repo)
    scratch = tmp_path / "tmp"  # TMPDIR: hermetic snapshot placement
    scratch.mkdir()
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    env = dict(os.environ, TMPDIR=str(scratch))
    with subprocess.Popen(
        [sys.executable,
         str(repo / "scripts" / "ci" / "fuzz_test_data.py"),
         "--iterations", "200", "--jobs", "2", "--seed", "1",
         "--artifact-dir", str(artifacts)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as proc:
        try:
            deadline = time.monotonic() + 90
            snaps: list[Path] = []
            while not snaps:
                assert proc.poll() is None, "run died before snapshotting"
                assert time.monotonic() < deadline, "no snapshot appeared"
                snaps = sorted(scratch.glob("fuzz-test-data-*"))
                time.sleep(0.2)
            shard_seen = None
            deadline = time.monotonic() + 30
            while shard_seen is None:
                assert proc.poll() is None, "run died before spawning shards"
                assert time.monotonic() < deadline, "no shard child spawned"
                others = [pid for pid in _procs_referencing(str(scratch))
                          if pid != proc.pid]
                shard_seen = others[0] if others else None
                time.sleep(0.2)
            os.kill(proc.pid, signal.SIGTERM)
            out = proc.communicate(timeout=60)[0].decode()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
    assert proc.returncode == 128 + signal.SIGTERM
    assert "Traceback" not in out
    assert not list(scratch.glob("fuzz-test-data-*"))
    deadline = time.monotonic() + 15
    while _procs_referencing(str(scratch)) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not _procs_referencing(str(scratch))


def test_a_sigterm_during_a_sequential_run_restores_the_baseline(
        monkeypatch, tmp_path: Path) -> None:
    """(issue #331) The sequential path gets the same unwinding: the
    SIGTERM raises SystemExit out of main, whose `finally` puts the
    operator's pricing document back — the tree is never left carrying
    the interrupted iteration's perturbed document."""
    repo = tmp_path / "repo"
    _build_runnable_repo(repo)
    _git_commit_all(repo)
    pricing_path = repo / "src" / "pricing.json"
    baseline = pricing_path.read_text(encoding="utf-8")

    def dying_suite(_root):
        os.kill(os.getpid(), signal.SIGTERM)
        raise AssertionError("the SIGTERM must interrupt the suite call")

    monkeypatch.setattr(fuzz_module, "run_suite", dying_suite)
    with pytest.raises(SystemExit) as exit_info:
        fuzz_module.main(["--iterations", "2", "--seed", "7",
                          "--artifact-dir", str(tmp_path)],
                         repo_root=repo)
    assert exit_info.value.code == 128 + signal.SIGTERM
    assert pricing_path.read_text(encoding="utf-8") == baseline
