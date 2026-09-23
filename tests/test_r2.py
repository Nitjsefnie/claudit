import errno
import lzma
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from backend import r2


@pytest.fixture(name="mini_r2")
def _mini_r2_fixture(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="sv-test-r2-")
    root = Path(tmp) / "claude"
    (root / "proj-a" / "sess-1").mkdir(parents=True)
    (root / "proj-a" / "sess-1" / "sess-1.jsonl").write_text("hello\n")
    (root / "proj-b" / "sess-2").mkdir(parents=True)
    (root / "proj-b" / "sess-2" / "sess-2.jsonl").write_text("world\n")
    (root / "proj-b" / "sess-2" / "data" / "tool-results").mkdir(
        parents=True
    )
    (root / "proj-b" / "sess-2" / "data" / "tool-results" / "x.txt"
     ).write_text("payload")
    monkeypatch.setenv("R2_ENDPOINT", f"file://{tmp}/")
    yield root
    shutil.rmtree(tmp)


def test_list_keys_walks_recursively(mini_r2):
    keys = sorted(o.key for o in r2.list_keys())
    assert keys == [
        "claude/proj-a/sess-1/sess-1.jsonl",
        "claude/proj-b/sess-2/data/tool-results/x.txt",
        "claude/proj-b/sess-2/sess-2.jsonl",
    ]


def test_list_keys_with_prefix(mini_r2):
    keys = [o.key for o in r2.list_keys(prefix="claude/proj-a")]
    assert keys == ["claude/proj-a/sess-1/sess-1.jsonl"]


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="root reads through chmod 000, so the unreadable-subtree "
           "scenario cannot be staged as root; the walk-error test "
           "below covers the same path",
)
def test_list_keys_aborts_when_a_subtree_is_unreadable(mini_r2):
    """A chmod-000'd subtree must abort the listing, not be silently
    skipped: os.walk's default onerror swallows the failure, and a
    partial listing is what the ingest orphan sweep deletes against."""
    locked = mini_r2 / "proj-b"
    locked.chmod(0o000)
    try:
        with pytest.raises(OSError):
            list(r2.list_keys())
    finally:
        locked.chmod(0o755)


def test_list_keys_aborts_when_the_walk_errors(monkeypatch, mini_r2):
    """Drives os.walk's error path directly (the chmod variant skips as
    root, this suite's user): the listing must propagate the walk
    failure instead of yielding a partial walk."""
    err = OSError(13, "Permission denied")

    def _failing_walk(_top, onerror=None, **_kw):
        if onerror is not None:
            onerror(err)
        return iter(())  # not reached: onerror raises

    monkeypatch.setattr(r2.os, "walk", _failing_walk)
    with pytest.raises(OSError):
        list(r2.list_keys())


def test_get_object(mini_r2):
    assert r2.get_object("claude/proj-a/sess-1/sess-1.jsonl") == b"hello\n"


def test_get_object_inflates_xz(mini_r2):
    # An xz-compressed object inflates transparently to its plain bytes.
    plain = b'{"type":"user"}\n{"type":"assistant"}\n'
    key = "claude/proj-a/sess-1/sess-1.jsonl.xz"
    (mini_r2 / "proj-a" / "sess-1" / "sess-1.jsonl.xz").write_bytes(
        lzma.compress(plain)
    )
    assert r2.get_object(key) == plain


def test_get_stream_inflates_xz(mini_r2):
    # Streaming a `.xz` key yields the decompressed lines.
    plain = b"alpha\nbeta\ngamma\n"
    (mini_r2 / "proj-a" / "sess-1" / "s.jsonl.xz").write_bytes(
        lzma.compress(plain)
    )
    with r2.get_stream("claude/proj-a/sess-1/s.jsonl.xz") as fh:
        assert fh.read() == plain


def test_path_traversal_blocked(mini_r2):
    # A foreign first segment is refused as an unconfigured bucket...
    with pytest.raises(ValueError):
        r2.get_object("../etc/passwd")
    with pytest.raises(ValueError):
        r2.get_object("proj-a/../../../etc/passwd")
    # ...while traversal inside the object-key part still hits
    # _safe_join: the bucket passes, the path out of it does not.
    with pytest.raises(PermissionError):
        r2.get_object("claude/../../etc/passwd")
    with pytest.raises(PermissionError):
        r2.get_stream("claude/../../etc/passwd")


def test_a_bucket_segment_that_is_not_a_plain_name_cannot_escape(
        mini_r2, monkeypatch):
    """The configured-bucket check is list membership, not a path check,
    and buckets()' grammar is what normally keeps '..' out. Even if a
    segment that is not a plain name reached the read path anyway, the
    _scan_root join itself (now _safe_join, like every other key-derived
    path) refuses to escape the mirror root."""
    monkeypatch.setattr(r2, "buckets", lambda: ["claude", "..", "..."])
    # '..' climbs out of the root: refused by the join, not by a lookup.
    with pytest.raises(PermissionError):
        r2.get_object("../secret.jsonl")
    with pytest.raises(PermissionError):
        r2.get_stream("../secret.jsonl")
    # A non-plain name that stays UNDER the root ('...' — a '/'
    # -carrying one cannot even get this far: split_key splits it off)
    # merely has no mirror directory, so reading refuses without ever
    # touching anything outside the root.
    with pytest.raises(FileNotFoundError):
        r2.get_object(".../secret.jsonl")
    # ...and the listing path hits the same join: '..' raises there too.
    with pytest.raises(PermissionError):
        list(r2.list_keys())


def test_scan_root_refuses_a_bucket_not_in_r2_bucket(monkeypatch, tmp_path):
    """CodeQL py/path-injection fix: _scan_root resolves its bucket
    against buckets() and builds the path from the CONFIGURED list
    element, not from the caller's string (which reaches it as the first
    segment of a stored file key via get_object/get_stream). A bucket
    outside R2_BUCKET raises _configured's ValueError before any path is
    built, and for a configured bucket the returned root is built from
    the configured name."""
    monkeypatch.setenv("R2_BUCKET", "claude")
    # Refusal: not in the list, no path is built at all (multi=True is
    # the None-returning case, multi=False the root fallback — both must
    # refuse before either answer is reachable).
    with pytest.raises(ValueError, match="is not configured in R2_BUCKET"):
        r2._scan_root(str(tmp_path), "codex", multi=True)  # pylint: disable=protected-access
    with pytest.raises(ValueError, match="is not configured in R2_BUCKET"):
        r2._scan_root(str(tmp_path), "codex", multi=False)  # pylint: disable=protected-access
    # Configured name: `<root>/claude` when the mirror has that
    # directory, the endpoint root itself (fallback) when it does not.
    (tmp_path / "claude").mkdir()
    assert r2._scan_root(str(tmp_path), "claude", multi=False) == (  # pylint: disable=protected-access
        os.path.realpath(str(tmp_path / "claude"))
    )
    shutil.rmtree(tmp_path / "claude")
    assert r2._scan_root(str(tmp_path), "claude", multi=False) == (  # pylint: disable=protected-access
        os.path.realpath(str(tmp_path))
    )


# ---------------------------------------------------------------------------
# M1/M2: a listing the walk cannot prove complete must raise, never be
# silently partial — the ingest orphan sweep deletes every row for keys
# the listing did not show.
# ---------------------------------------------------------------------------


def test_list_keys_refuses_a_missing_endpoint_root(monkeypatch, tmp_path):
    """Single-bucket file mode with the endpoint root itself GONE (a
    typo'd path, an unmounted mountpoint): the listing must raise like
    the multi-bucket refusal does, never yield empty and let the orphan
    sweep delete the bucket's whole history."""
    monkeypatch.setenv("R2_ENDPOINT", f"file://{tmp_path}/absent/")
    monkeypatch.delenv("R2_BUCKET", raising=False)
    with pytest.raises(FileNotFoundError):
        list(r2.list_keys())


def test_list_keys_refuses_a_root_that_is_not_a_directory(
        monkeypatch, tmp_path):
    """Same scenario with the root present but a plain file."""
    notdir = tmp_path / "notadir"
    notdir.write_text("not a mirror")
    monkeypatch.setenv("R2_ENDPOINT", f"file://{notdir}")
    monkeypatch.delenv("R2_BUCKET", raising=False)
    with pytest.raises(FileNotFoundError):
        list(r2.list_keys())


def test_list_keys_raises_when_a_listed_file_cannot_be_stated(
        monkeypatch, mini_r2):
    """An os.stat failure other than ENOENT (a directory with r but no
    x, a symlink whose target denies) must abort the listing: silently
    dropping the file would let the orphan sweep delete the row of a
    file that is still there."""
    victim = mini_r2 / "proj-a" / "sess-1" / "sess-1.jsonl"
    real_stat = os.stat

    def denying_stat(path, *args, **kwargs):
        if str(path) == str(victim):
            raise PermissionError(errno.EACCES, "Permission denied")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(r2.os, "stat", denying_stat)
    with pytest.raises(OSError):
        list(r2.list_keys())


def test_list_keys_skips_a_file_that_vanished_mid_walk(mini_r2):
    """ENOENT stays the allowed case: a file that was listed and then
    deleted before its stat is legitimately gone, and the orphan sweep
    will see it gone too. The listing succeeds without it."""
    dangling = mini_r2 / "proj-a" / "sess-1" / "gone.jsonl"
    dangling.symlink_to(mini_r2 / "nowhere.jsonl")
    keys = [o.key for o in r2.list_keys()]
    assert "claude/proj-a/sess-1/gone.jsonl" not in keys
    assert "claude/proj-a/sess-1/sess-1.jsonl" in keys
