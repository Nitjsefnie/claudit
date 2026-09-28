"""Sharded runs of the SV-TEST-DATA fuzzer.

Split out of scripts/ci/fuzz_test_data.py (issue #331) so the sharded
run could own its teardown: an interrupted parent used to leave the
snapshot tree in the system temp directory and every shard child
running. The module-size ceiling left no room in the fuzzer itself.

A shard child runs in its own session (start_new_session), so the
parent owns each shard's whole process subtree: one killpg reaches the
shard child and the suite child it is running together, and a terminal
^C to the parent's group cannot half-kill the tree.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from types import FrameType
from typing import Any

# The child command runs the SNAPSHOT's copy of the fuzzer.
FUZZER_NAME = "fuzz_test_data.py"
# Grace between SIGTERM and SIGKILL for a shard's process group.
KILL_GRACE_S = 5.0
INTERRUPT_SIGNAL_NAMES = ("SIGTERM", "SIGINT", "SIGHUP")
# The only names pruned from the collection-root union: never tests.
PRUNED_TEST_DIRS = frozenset({"__pycache__", ".pytest_cache"})


def _git(target: Path, *args: str) -> None:
    """One git command in `target`, failing loudly on a nonzero exit."""
    subprocess.run(["git", "-C", str(target), *args], check=True,
                   capture_output=True, text=True)


def interrupt_signals() -> list[int]:
    """The interrupt signals this platform has, in teardown order."""
    return [sig for sig in (getattr(signal, name, None)
                            for name in INTERRUPT_SIGNAL_NAMES)
            if sig is not None]


def install_interrupt_handlers() -> Callable[[], None]:
    """Route the interrupt signals through SystemExit(128 + signum) so
    main's `finally` cleanup — the pricing baseline restore, the
    snapshot-tree removal — runs on an interrupted run, where SIGTERM's
    default disposition killed the process with no unwinding at all
    (issue #331).

    The interrupt signals are UNBLOCKED first: a shard child inherits
    the parent's blocked spawn-window mask across fork/exec, and a
    handler installed over a blocked signal is a dead letter — the
    parent's SIGTERM teardown stage could never fire and every
    interrupted run would escalate to SIGKILL. Returns the restore
    callable; call it when the run ends."""
    if hasattr(signal, "pthread_sigmask"):
        signal.pthread_sigmask(signal.SIG_UNBLOCK, interrupt_signals())

    def raise_exit(signum: int, _frame: FrameType | None) -> None:
        raise SystemExit(128 + signum)

    restore_pairs: list[tuple[int, Any]] = []
    for sig in interrupt_signals():
        try:
            previous = signal.signal(sig, raise_exit)
        except (OSError, ValueError):  # not interruptible here
            continue
        restore_pairs.append((sig, previous))

    def restore() -> None:
        for sig, previous in restore_pairs:
            signal.signal(sig, previous)

    return restore


@contextmanager
def _blocked_signals() -> Generator[None]:
    """Block the interrupt signals for the critical windows — spawning
    a shard (a handler firing between Popen and the pending list would
    leave the child unpending, unkillable) and teardown (a second ^C
    would half-kill the tree). Restored in `finally`; a signal delivered
    while blocked is raised after the window closes, past the cleanup."""
    blocked = set(interrupt_signals())
    if not blocked or not hasattr(signal, "pthread_sigmask"):
        yield
        return
    signal.pthread_sigmask(signal.SIG_BLOCK, blocked)
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_UNBLOCK, blocked)


def _terminate_shards(
        pending: list[tuple[Path, Path, subprocess.Popen[bytes]]]) -> None:
    """Signal each live shard's whole process GROUP, then reap: one
    killpg reaches the shard child and the suite child it is running,
    so no orphan outlives the run and nothing recreates the snapshot
    tree after its removal. The SIGTERM stage is the graceful path — a
    shard child runs with the signals unblocked (see
    install_interrupt_handlers), so its own handler raises SystemExit,
    whose unwind through run_suite's subprocess.run kills the suite
    child (run() kills the child on ANY exception, bare except); the
    grandchild is in the child's group either way. SIGKILL after the
    grace only where SIGTERM was not enough. The interrupt signals
    stay blocked throughout."""
    with _blocked_signals():
        for _shard_dir, _result_file, child in pending:
            if child.poll() is not None:
                continue
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                continue
        deadline = time.monotonic() + KILL_GRACE_S
        for _shard_dir, _result_file, child in pending:
            try:
                child.wait(timeout=max(deadline - time.monotonic(), 0.1))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()


def _shards(total: int, jobs: int) -> list[tuple[int, int, int]]:
    """(first, step, count) per shard, round-robin: shard j owns the
    iterations j, j + jobs, ... The union over the shards is
    range(total), each iteration exactly once; `count` is 0 for a
    shard with more shards than iterations."""
    return [(j, jobs, (total - j + jobs - 1) // jobs) for j in range(jobs)]


def _run_sharded(repo_root: Path, base_seed: int, total: int, jobs: int,
                 artifact_dir: Path) -> list[dict]:
    """The iterations sharded across J children, each in its own clone
    of the tree; results merged in iteration order. An interrupted or
    crashed run takes every shard's process group down BEFORE the
    snapshot tree is removed (issue #331)."""
    with tempfile.TemporaryDirectory(prefix="fuzz-test-data-") as tmp:
        pending: list[tuple[Path, Path, subprocess.Popen[bytes]]] = []
        try:
            for first, step, count in _shards(total, jobs):
                if not count:
                    continue
                shard_dir = Path(tmp) / f"shard-{first}"
                _snapshot_tree(repo_root, shard_dir)
                result_file = shard_dir / "shard-result.json"
                with _blocked_signals():
                    child = subprocess.Popen(  # pylint: disable=consider-using-with
                        [sys.executable,
                         str(shard_dir / "scripts" / "ci" / FUZZER_NAME),
                         "--iterations", str(count),
                         "--iteration-first", str(first),
                         "--iteration-step", str(step),
                         "--seed", str(base_seed),
                         "--artifact-dir", str(artifact_dir),
                         "--result-file", str(result_file)],
                        cwd=str(shard_dir), start_new_session=True)
                    pending.append((shard_dir, result_file, child))
            merged = _collect_shards(pending)
        except BaseException:
            _terminate_shards(pending)
            raise
    merged.sort(key=lambda result: result["iteration"])
    return merged


def _collect_shards(
        pending: list[tuple[Path, Path, subprocess.Popen[bytes]]]) -> list[dict]:
    """Wait for every shard child and gather its results, failing
    loudly on a child that died without writing its result file."""
    completed = [(shard_dir, result_file, child, child.wait())
                 for shard_dir, result_file, child in pending]
    results: list[dict] = []
    for shard_dir, result_file, _child, code in completed:
        # Exit 1 is the child's documented "failing suite" exit, with
        # its results written; only a child that died WITHOUT them is
        # a crash worth raising on.
        if code not in (0, 1) or not result_file.exists():
            raise RuntimeError(
                f"fuzz shard {shard_dir.name}: child exited {code} "
                "without a result file")
        shard = json.loads(result_file.read_text(encoding="utf-8"))
        results.extend(shard["results"])
    return results


def _snapshot_tree(source: Path, dest: Path) -> None:
    """A disposable git checkout of the tree AS GIT KNOWS IT — tracked
    files plus untracked, unignored ones (`git ls-files -co
    --exclude-standard`) — so a shard runs committed and uncommitted
    work alike, exactly what the sequential run would: a HEAD-only
    clone would silently drop uncommitted tests or code edits and
    could report a false green over stale code. Only listed files are
    copied, so ignored runtime artifacts — a live socket, a fifo, a
    pid file — can neither break the copy nor reach a shard. The copy
    becomes its own git repository whose snapshot commit is the
    per-iteration restore's baseline."""
    listing = subprocess.run(
        ["git", "-C", str(source), "ls-files", "-z", "-co",
         "--exclude-standard"],
        check=True, capture_output=True, text=True).stdout
    dest.mkdir(parents=True)
    for name in listing.split("\x00"):
        if not name:
            continue
        src = source / name
        dst = dest / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_symlink():
            os.symlink(os.readlink(src), dst)
        elif src.is_file():
            shutil.copyfile(src, dst)
    _snapshot_collection_root(source, dest)
    _git(dest, "init", "-q")
    _git(dest, "add", "-A")
    _git(dest, "-c", "user.name=fuzz test", "-c",
         "user.email=fuzz@localhost", "commit", "-q", "-m",
         "fuzz shard baseline")


def _snapshot_collection_root(source: Path, dest: Path) -> None:
    """The collection-root union: every regular file under tests/ that
    the git-known set may have missed is copied too — a NESTED test
    directory is invisible to a deny-by-default .gitignore, so git
    ignores it, yet pytest collects it; a shard without it would run a
    smaller suite than the sequential run. Only __pycache__ and
    .pytest_cache are pruned; any other non-regular entry under tests/
    refuses the shard loudly: it cannot be snapshotted, so the
    population guarantee would fail silently. Outside tests/ nothing
    extra is walked."""
    tests_root = source / "tests"
    if not tests_root.is_dir():
        return
    for current, dirs, files in os.walk(tests_root):
        here = Path(current)
        dirs[:] = [d for d in dirs if d not in PRUNED_TEST_DIRS]
        for d in dirs:
            if (here / d).is_symlink():
                raise SystemExit(_shard_refusal(here / d, source))
        for name in files:
            src = here / name
            dst = dest / src.relative_to(source)
            if src.is_symlink() or not src.is_file():
                raise SystemExit(_shard_refusal(src, source))
            if not dst.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)


def _shard_refusal(path: Path, repo_root: Path) -> str:
    """Format the refused path as a repository-relative POSIX path."""
    display_path = (path.relative_to(repo_root).as_posix()
                    if path.is_relative_to(repo_root)
                    else path.absolute().as_posix())
    return (f"fuzz: refusing to shard: {display_path} is not a "
            "regular file, so it cannot be snapshotted and the shard "
            "would silently miss it; remove it or run without --jobs")
