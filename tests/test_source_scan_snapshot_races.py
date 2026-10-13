"""Generation consistency and retry budgets for source snapshots."""
from __future__ import annotations

import ast
from contextlib import contextmanager
import os
from pathlib import Path
import types
from typing import Any, Iterator

import pytest

from tests import source_scan_cache
from tests.test_source_scan_cache import _count_preprocessing, _failure, _old_clock


@pytest.mark.parametrize("persistent", [False, True])
def test_descriptor_changes_with_stable_path_retry_or_raise(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, persistent: bool) -> None:
    path = tmp_path / "descriptor.py"
    path.write_bytes(b"value = 1\n")
    path_stat = path.stat()
    original_stat = Path.stat
    original_fstat = os.fstat
    observations = 0

    def stable_stat(file: Path, *args: Any, **kwargs: Any) -> os.stat_result:
        if file == path:
            return path_stat
        return original_stat(file, *args, **kwargs)

    def changing_fstat(fd: int) -> Any:
        nonlocal observations
        stat = original_fstat(fd)
        observations += 1
        changed = observations % 2 == 0 if persistent else observations == 2
        return types.SimpleNamespace(
            st_dev=stat.st_dev, st_ino=stat.st_ino, st_size=stat.st_size + changed,
            st_mtime_ns=stat.st_mtime_ns, st_ctime_ns=stat.st_ctime_ns)

    monkeypatch.setattr(Path, "stat", stable_stat)
    monkeypatch.setattr(os, "fstat", changing_fstat)
    if persistent:
        with pytest.raises(RuntimeError, match="Source generation changed during read"):
            source_scan_cache.snapshot(path)
        assert observations == 6
    else:
        assert source_scan_cache.snapshot(path).raw == b"value = 1\n"
        assert observations == 4


def test_path_and_descriptor_signatures_may_differ(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "different_stat.py"
    text = "value = 1\n"
    path.write_text(text, encoding="utf-8")
    _old_clock((path,), monkeypatch)
    original_fstat = os.fstat

    def descriptor_stat(fd: int) -> Any:
        stat = original_fstat(fd)
        return types.SimpleNamespace(
            st_dev=stat.st_dev + 1, st_ino=stat.st_ino + 1,
            st_size=stat.st_size, st_mtime_ns=stat.st_mtime_ns,
            st_ctime_ns=stat.st_ctime_ns + 1)

    monkeypatch.setattr(os, "fstat", descriptor_stat)
    calls = _count_preprocessing(text, monkeypatch)
    one = source_scan_cache.snapshot(path)
    assert one.source() == text
    assert source_scan_cache.snapshot(path) is one
    assert calls[0][path.resolve()] == 1


@pytest.mark.parametrize("stage", ["after-open", "after-fstat-replace",
                                   "after-fstat-rewrite"])
def test_path_changes_outside_descriptor_observations_retry(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str) -> None:
    path = tmp_path / "racing.py"
    path.write_bytes(b"old = 1\n")
    replacement = tmp_path / "replacement.py"
    replacement.write_bytes(b"new_value = 2\n")
    original_open = Path.open
    reads = 0

    @contextmanager
    def racing_open(file: Path, *args: Any, **kwargs: Any) -> Iterator[Any]:
        nonlocal reads
        reads += 1
        with original_open(file, *args, **kwargs) as handle:
            if reads == 1 and stage == "after-open":
                try:
                    replacement.replace(path)
                except PermissionError:
                    if os.name != "nt":
                        raise
                    pytest.skip("Windows disallows replacing this open reader")
            yield handle
        if reads == 1 and stage == "after-fstat-replace":
            replacement.replace(path)
        if reads == 1 and stage == "after-fstat-rewrite":
            with original_open(path, "wb") as writer:
                writer.write(b"new_value = 2\n")

    monkeypatch.setattr(Path, "open", racing_open)
    assert source_scan_cache.snapshot(path).raw == b"new_value = 2\n"
    assert reads == 2


@pytest.mark.parametrize("persistent", [False, True])
def test_path_signature_mismatch_retries_are_bounded(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, persistent: bool) -> None:
    path = tmp_path / "metadata.py"
    path.write_bytes(b"value = 1\n")
    original_stat = Path.stat
    observations = 0

    def changing_stat(file: Path, *args: Any, **kwargs: Any) -> Any:
        nonlocal observations
        stat = original_stat(file, *args, **kwargs)
        if file != path:
            return stat
        observations += 1
        changed = observations % 2 == 0 if persistent else observations == 2
        return types.SimpleNamespace(
            st_dev=stat.st_dev, st_ino=stat.st_ino, st_size=stat.st_size,
            st_mtime_ns=stat.st_mtime_ns, st_ctime_ns=stat.st_ctime_ns + changed)

    monkeypatch.setattr(Path, "stat", changing_stat)
    if persistent:
        with pytest.raises(RuntimeError, match="Source generation changed during read"):
            source_scan_cache.snapshot(path)
        assert observations == 6
    else:
        assert source_scan_cache.snapshot(path).raw == b"value = 1\n"
        assert observations == 4


def test_replacement_between_path_stat_and_open_is_reread(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "replaced.py"
    path.write_text("old = 1\n", encoding="utf-8")
    replacement = tmp_path / "replacement.py"
    replacement.write_text("new = 2\n", encoding="utf-8")
    _old_clock((path, replacement), monkeypatch)
    original_open = Path.open
    reads = 0

    def replacing_open(file: Path, *args: Any, **kwargs: Any) -> Any:
        nonlocal reads
        reads += 1
        if reads == 1:
            replacement.replace(path)
        return original_open(file, *args, **kwargs)

    monkeypatch.setattr(Path, "open", replacing_open)
    one = source_scan_cache.snapshot(path)
    assert one.source() == "new = 2\n"
    assert source_scan_cache.snapshot(path) is one
    assert reads == 2
    assert source_scan_cache.snapshot(path) is one
    assert reads == 2


@pytest.mark.parametrize("recent", ["mtime", "ctime"])
def test_racy_reads_reuse_content_and_stop_when_clock_advances(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recent: str) -> None:
    path = tmp_path / "racy.py"
    text = f"value = 1  # {tmp_path.name}\n"
    path.write_text(text, encoding="utf-8")
    if recent == "ctime":
        os.utime(path, ns=(1, 1))
        now = [path.stat().st_ctime_ns]
    else:
        now = [path.stat().st_ctime_ns + 10_000_000_000]
        os.utime(path, ns=(1, now[0]))
    stat = path.stat()
    monkeypatch.setattr(source_scan_cache, "_now_ns", lambda: now[0],
                        raising=False)
    calls = _count_preprocessing(text, monkeypatch)
    one = source_scan_cache.snapshot(path)
    artifacts = source_scan_cache.for_source(one.source())
    expected = (one.source(), artifacts.tokens(), artifacts.tree(),
                artifacts.nodes(), artifacts.scan_code())
    for _ in range(3):
        assert source_scan_cache.snapshot(path) is one
        assert source_scan_cache.source(path) is expected[0]
        shared = source_scan_cache.for_source(source_scan_cache.source(path))
        assert shared.tokens() is expected[1]
        assert shared.tree() is expected[2]
        assert shared.nodes() is expected[3]
        assert shared.scan_code() is expected[4]
    assert calls[0][path.resolve()] == 10
    assert calls[1] == calls[2] == {text: 1}
    assert calls[3] == {"walk": 1, "compile": 1}
    now[0] = max(stat.st_mtime_ns, stat.st_ctime_ns) + 2_000_000_000
    # Equality at the margin remains racy; one ns older allows a fast path.
    assert source_scan_cache.snapshot(path) is one
    assert source_scan_cache.snapshot(path) is one
    assert calls[0][path.resolve()] == 12
    now[0] += 1
    assert source_scan_cache.snapshot(path) is one
    assert calls[0][path.resolve()] == 13
    for _ in range(3):
        assert source_scan_cache.snapshot(path) is one
        assert source_scan_cache.source(path) is expected[0]
    assert calls[0][path.resolve()] == 13
    assert calls[1] == calls[2] == {text: 1}
    assert calls[3] == {"walk": 1, "compile": 1}


def test_same_size_restored_mtime_and_inode_replacement(tmp_path: Path) -> None:
    path = tmp_path / "rewrite.py"
    path.write_text("before = 1\n", encoding="utf-8")
    old_stat = path.stat()
    old = source_scan_cache.snapshot(path)
    old_artifacts = source_scan_cache.for_source(old.source())
    old_tree, old_tokens = old_artifacts.tree(), old_artifacts.tokens()
    path.write_text("after_ = 2\n", encoding="utf-8")
    os.utime(path, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    changed = source_scan_cache.snapshot(path)
    assert path.stat().st_size == old_stat.st_size
    assert path.stat().st_mtime_ns == old_stat.st_mtime_ns
    assert changed.source() == "after_ = 2\n"
    assert changed.content_hash != old.content_hash
    artifacts = source_scan_cache.for_source(changed.source())
    assert artifacts.tree() is not old_tree
    assert artifacts.tokens() is not old_tokens
    assert ast.dump(artifacts.tree()) != ast.dump(old_tree)
    assert artifacts.tokens() != old_tokens
    replacement = tmp_path / "replacement.py"
    replacement.write_text("third_ = 3\n", encoding="utf-8")
    os.utime(replacement, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    replacement.replace(path)
    assert path.stat().st_ino != old_stat.st_ino
    replaced = source_scan_cache.snapshot(path)
    assert replaced.content_hash != changed.content_hash
    assert replaced.source() == "third_ = 3\n"
    path.write_text(old.source(), encoding="utf-8")
    returned = source_scan_cache.snapshot(path)
    assert returned is old
    assert source_scan_cache.for_source(returned.source()) is old_artifacts


def test_generation_changed_during_read_fails_explicitly(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "racing.py"
    path.write_text("old = 1\n", encoding="utf-8")
    original_open = Path.open

    @contextmanager
    def changing_open(file: Path, *args: Any,
                      **kwargs: Any) -> Iterator[Any]:
        with original_open(file, *args, **kwargs) as handle:
            def changing_read() -> bytes:
                data = handle.read()
                with original_open(file, "wb") as writer:
                    writer.write(data + b"\n")
                return data

            yield types.SimpleNamespace(fileno=handle.fileno,
                                        read=changing_read)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", changing_open)
        assert isinstance(_failure(lambda: source_scan_cache.snapshot(path)),
                          RuntimeError)
    assert source_scan_cache.source(path) == "old = 1\n\n\n\n"
