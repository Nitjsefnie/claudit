import lzma
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
