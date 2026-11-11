from __future__ import annotations

import bisect
import errno
import hashlib
import lzma
import multiprocessing
import os
import shutil
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import FrameType

import boto3
import pytest
from boto3.session import Session
from botocore.paginate import Paginator
from botocore.session import get_session

from backend import blob_cache, r2, r2_list, timing


class _ListingS3:
    """R2's ListObjectsV2: UTF-8 key order, ContinuationToken pages,
    StartAfter exclusive unless it extends the preceding key (returned too).
    Unmodelled parameters raise."""

    names = ("zulu", "claude", "alpha")

    def __init__(self, keys: dict[str, list[str]], page: int) -> None:
        self.keys = {b: sorted(set(ks), key=str.encode) for b, ks in keys.items()}
        self.page = page
        self.lock = threading.Lock()
        self.caller = threading.current_thread()
        self.calls: list[tuple] = []
        self.tokens: dict[str, tuple] = {}
        self.failure: tuple | None = None
        self.fired = False
        self.rendezvous = 0
        self.barrier: threading.Barrier | None = None
        session = get_session()
        self.config = session.get_paginator_model("s3").get_paginator("ListObjectsV2")
        self.model = session.get_service_model("s3").operation_model("ListObjectsV2")

    def get_paginator(self, operation: str) -> Paginator:
        assert operation == "list_objects_v2", operation
        return Paginator(self.list_objects_v2, self.config, self.model)

    def list_objects_v2(self, *, Bucket: str, Prefix: str = "", StartAfter: str | None = None,
                        ContinuationToken: str | None = None, MaxKeys: int = 1000) -> dict:
        keys = self.keys[Bucket]
        with self.lock:
            self.calls.append((threading.current_thread(), Bucket, Prefix, StartAfter,
                               ContinuationToken))
            if ContinuationToken is not None:
                start, origin, number = self.tokens[ContinuationToken]
            else:
                start, origin, number = self._first(keys, StartAfter), StartAfter, 0
            barrier = self.barrier if number == 0 and self.rendezvous < 3 else None
            self.rendezvous += barrier is not None
        if barrier is not None:
            barrier.wait()
        if self.failure == (Bucket, origin, number):
            self.fired = True
            raise RuntimeError("listing failed")
        window = [i for i in range(start, len(keys)) if keys[i].startswith(Prefix)]
        shown = window[:min(MaxKeys, self.page)]
        page = {"IsTruncated": len(window) > len(shown), "KeyCount": len(shown)}
        if shown:
            page["Contents"] = [{"Key": keys[i], "ETag": f'"{_etag(keys[i])}"',
                                 "Size": len(keys[i].encode()), "LastModified": _STAMP}
                                for i in shown]
        if page["IsTruncated"]:
            with self.lock:
                token = f"token-{len(self.tokens)}"
                self.tokens[token] = (shown[-1] + 1, origin, number + 1)
            page["NextContinuationToken"] = token
        return page

    @staticmethod
    def _first(keys: list[str], start_after: str | None) -> int:
        if start_after is None:
            return 0
        start = bisect.bisect_right([k.encode() for k in keys], start_after.encode())
        if start and keys[start - 1] != start_after and start_after.startswith(keys[start - 1]):
            start -= 1
        return start

    def plain_walk(self) -> tuple[list[r2.R2Object], int]:
        objects, pages = [], 0
        for bucket in self.names:
            for page in self.get_paginator("list_objects_v2").paginate(Bucket=bucket):
                pages += 1
                objects += [r2.R2Object(f"{bucket}/{o['Key']}", o["ETag"].strip('"'),
                                        o["Size"], o["LastModified"])
                            for o in page.get("Contents", [])]
        assert [o.key for o in objects] == [
            f"{b}/{k}" for b in self.names for k in self.keys[b]]
        self.calls.clear()
        return objects, pages


_STAMP = datetime(2026, 1, 2, tzinfo=timezone.utc)
_BUCKET_KEYS = {
    "zulu": ["A/s1.jsonl", "B/s2.jsonl", "_x/s3.jsonl", "a/s4.jsonl", "a/s4.meta.json",
             "a/s5.jsonl", "b/s6.jsonl", "b/s7.jsonl", "c/Z.jsonl", "c/z.jsonl", "c/é.jsonl",
             "c/ž.jsonl", "d/s8.jsonl", "d/s9.jsonl"],
    "claude": [f"p/{n}.jsonl" for n in range(1, 10)] + ["q/1.jsonl", "q/2.jsonl"],
    "alpha": [],
}
_SPLITS = {
    "cold start": [],
    "existing": ["zulu/d/s8.jsonl", "zulu/a/s4.jsonl", "zulu/a/s4.jsonl", "zulu/A/s1.jsonl",
                 "zulu/c/z.jsonl", "claude/p/7.jsonl", "claude/p/2.jsonl"],
    "deleted": ["zulu/0/gone.jsonl", "zulu/a/s4.5.jsonl", "zulu/zz/gone.jsonl",
                "claude/p/0.jsonl", "alpha/x/gone.jsonl", "alpha/y/gone.jsonl"],
    "extends a key": ["zulu/b/s6.jsonl0", "zulu/b/s6.jsonl.bak", "claude/p/3.jsonl0"],
    "collation order": ["zulu/a/s5.jsonl", "zulu/B/s2.jsonl", "zulu/c/é.jsonl",
                        "zulu/c/Z.jsonl", "claude/q/1.jsonl", "claude/P/1.jsonl"],
}


def _etag(key: str) -> str:
    return hashlib.sha1(key.encode()).hexdigest()


def _ranges(splits: list[str]) -> int:
    return len(_ListingS3.names) + len(set(splits))


@pytest.fixture(name="listing_s3")
def _listing_s3_fixture(monkeypatch) -> _ListingS3:
    client = _ListingS3(_BUCKET_KEYS, page=3)
    monkeypatch.setenv("CLAUDIT_R2_ENDPOINT", "https://r2.invalid/s3")
    monkeypatch.setenv("CLAUDIT_R2_BUCKET", "+".join(client.names))
    monkeypatch.setattr(r2, "_boto_client", lambda: client)
    return client


@pytest.mark.parametrize("splits", _SPLITS.values(), ids=_SPLITS.keys())
def test_s3_listing_equals_the_plain_walk(listing_s3: _ListingS3, splits: list[str]) -> None:
    expected, pages = listing_s3.plain_walk()
    assert list(r2.list_keys(split_points=splits)) == expected
    starts = [call[1:4:2] for call in listing_s3.calls if call[4] is None]
    assert len(starts) == _ranges(splits) and set(starts) == {
        (b, None) for b in listing_s3.names} | {r2.split_key(s) for s in splits}
    assert len(listing_s3.calls) <= pages + _ranges(splits), (len(listing_s3.calls), pages)
    if not splits:
        assert len(listing_s3.calls) == pages


def test_s3_listing_stops_at_the_first_page_past_its_range() -> None:
    client = _ListingS3({"one": [f"k{n}" for n in range(1, 7)]}, page=3)
    rows = r2_list.list_keys(["one"], lambda: client, {"one": ["k2"]})
    assert [row[0] for row in rows] == [f"one/k{n}" for n in range(1, 7)]
    assert len(client.calls) == 3, client.calls


def test_s3_listing_runs_its_ranges_on_the_pool(listing_s3: _ListingS3) -> None:
    expected, _ = listing_s3.plain_walk()
    listing_s3.barrier = threading.Barrier(3, timeout=10)
    assert list(r2.list_keys(split_points=_SPLITS["existing"])) == expected
    assert listing_s3.caller not in {call[0] for call in listing_s3.calls}


@pytest.mark.parametrize("failure", [
    ("zulu", None, 0),
    ("zulu", "a/s4.jsonl", 0),
    ("claude", "p/2.jsonl", 1),
])
def test_s3_listing_failure_yields_nothing(listing_s3: _ListingS3, failure: tuple) -> None:
    listing_s3.failure = failure
    listing = r2.list_keys(split_points=_SPLITS["existing"])
    with pytest.raises(RuntimeError, match="listing failed"):
        next(listing)
    assert listing_s3.fired


def test_s3_listing_prefix_stays_serial(listing_s3: _ListingS3) -> None:
    expected, _ = listing_s3.plain_walk()
    assert list(r2.list_keys(prefix="claude/p/", split_points=_SPLITS["existing"])) == [
        o for o in expected if o.key.startswith("claude/p/")]
    assert [call[1:4] for call in listing_s3.calls] == [("claude", "p/", None)] * 3
    assert [call[4] is None for call in listing_s3.calls] == [True, False, False]
    assert {call[0] for call in listing_s3.calls} == {listing_s3.caller}


@pytest.fixture(name="mini_r2")
def _mini_r2_fixture(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="sv-test-r2-")
    root = Path(tmp) / "claude"
    (root / "proj-a" / "sess-1").mkdir(parents=True)
    (root / "proj-a" / "sess-1" / "sess-1.jsonl").write_text("hello\n", newline="\n")
    (root / "proj-b" / "sess-2").mkdir(parents=True)
    (root / "proj-b" / "sess-2" / "sess-2.jsonl").write_text("world\n", newline="\n")
    (root / "proj-b" / "sess-2" / "data" / "tool-results").mkdir(
        parents=True
    )
    (root / "proj-b" / "sess-2" / "data" / "tool-results" / "x.txt"
     ).write_text("payload", newline="\n")
    # as_uri, not an f"file://{path}" spelling: on Windows the f-string
    # form reads the drive letter as the URL host and loses it from the
    # path, pointing the mirror at a relative directory.
    monkeypatch.setenv("CLAUDIT_R2_ENDPOINT", f"{Path(tmp).as_uri()}/")
    yield root
    shutil.rmtree(tmp)


def test_list_keys_walks_recursively(mini_r2):
    keys = sorted(o.key for o in r2.list_keys(split_points=["claude/proj-b"]))
    assert keys == [
        "claude/proj-a/sess-1/sess-1.jsonl",
        "claude/proj-b/sess-2/data/tool-results/x.txt",
        "claude/proj-b/sess-2/sess-2.jsonl",
    ]


def test_list_keys_with_prefix(mini_r2):
    keys = [o.key for o in r2.list_keys(prefix="claude/proj-a")]
    assert keys == ["claude/proj-a/sess-1/sess-1.jsonl"]


def _can_stage_chmod_denial() -> bool:
    """Staging a chmod-000 denial needs POSIX permission bits and a
    reader they bind: root reads through them, and Windows grants the
    walk regardless of the mode bits. The stat-monkeypatch test below
    covers the same walk path where this one cannot run."""
    return hasattr(os, "geteuid") and os.geteuid() != 0


@pytest.mark.skipif(
    not _can_stage_chmod_denial(),
    reason="chmod 000 denies nothing to root and binds nothing on "
           "Windows, so the unreadable-subtree scenario cannot be "
           "staged here; the stat-monkeypatch test below covers the "
           "same path",
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
    try:
        with pytest.raises(OSError):
            list(r2.list_keys())
    finally:
        # Undo before the fixture's rmtree teardown: POSIX rmtree walks
        # with directory-descriptor scanning and never calls os.walk,
        # but Windows rmtree IS os.walk-based, so the patched walk would
        # blow up the cleanup instead of the listing.
        monkeypatch.undo()


def test_get_object_inflates_xz(mini_r2, monkeypatch):
    # xz inflates transparently; the inflate wall stamps the per-thread
    # accumulator when the timing flag is on, read-and-reset.
    plain = b'{"type":"user"}\n{"type":"assistant"}\n'
    key = "claude/proj-a/sess-1/sess-1.jsonl.xz"
    (mini_r2 / "proj-a" / "sess-1" / "sess-1.jsonl.xz").write_bytes(
        lzma.compress(plain)
    )
    assert r2.get_object(key) == plain

    monkeypatch.setattr(timing, "TIMING_ON", True)
    bulk = b'{"type":"user"}\n' * 2000
    timed_key = "claude/proj-a/sess-1/timed.jsonl.xz"
    (mini_r2 / "proj-a" / "sess-1" / "timed.jsonl.xz").write_bytes(
        lzma.compress(bulk)
    )
    r2.get_object(timed_key)
    assert r2.pop_decompress_seconds() > 0.0
    assert r2.pop_decompress_seconds() == 0.0  # read-and-reset
    r2.get_object("claude/proj-a/sess-1/sess-1.jsonl")
    assert r2.pop_decompress_seconds() == 0.0
    monkeypatch.setattr(timing, "TIMING_ON", False)
    r2.get_object(timed_key)
    assert r2.pop_decompress_seconds() == 0.0


def test_get_stream_inflates_xz(mini_r2):
    plain = b"alpha\nbeta\ngamma\n"
    (mini_r2 / "proj-a" / "sess-1" / "s.jsonl.xz").write_bytes(
        lzma.compress(plain)
    )
    with r2.get_stream("claude/proj-a/sess-1/s.jsonl.xz") as fh:
        assert fh.read() == plain


def test_get_object_cache_miss_falls_through_to_the_object(
        mini_r2, monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDIT_R2_BLOB_CACHE", str(tmp_path / "cache"))
    key = "claude/proj-a/sess-1/sess-1.jsonl"
    # No entry yet: the miss reads the object and stores it for the next
    # run, and a different etag is a different entry.
    assert r2.get_object(key, "etag-one", 6) == b"hello\n"
    (mini_r2 / "proj-a" / "sess-1" / "sess-1.jsonl").write_bytes(b"CHANGED")
    assert r2.get_object(key, "etag-two", 7) == b"CHANGED"
    assert r2.get_object(key, "etag-two", 7) == b"CHANGED"
    assert r2.get_object(key, "etag-one", 6) == b"hello\n"


def test_get_object_cache_stores_raw_xz_bytes(
        mini_r2, monkeypatch, tmp_path):
    """The entry holds the compressed bytes; a hit inflates like the GET
    would, so TIMING's decompress stage stays booked either way."""
    monkeypatch.setenv("CLAUDIT_R2_BLOB_CACHE", str(tmp_path / "cache"))
    plain = b'{"type":"user"}\n{"type":"assistant"}\n'
    key = "claude/proj-a/sess-1/sess-1.jsonl.xz"
    path = mini_r2 / "proj-a" / "sess-1" / "sess-1.jsonl.xz"
    path.write_bytes(lzma.compress(plain))
    compressed = path.read_bytes()
    assert r2.get_object(key, "etag-xz", len(compressed)) == plain
    cached = blob_cache.lookup(key, "etag-xz", None)
    assert cached == compressed
    path.unlink()
    assert r2.get_object(key, "etag-xz", len(compressed)) == plain


def test_get_object_without_etag_never_touches_the_cache(
        mini_r2, monkeypatch, tmp_path):
    """Serving and the bench speak the plain (key) shape: even with the
    cache enabled, no entry is written or read."""
    monkeypatch.setenv("CLAUDIT_R2_BLOB_CACHE", str(tmp_path / "cache"))
    assert r2.get_object("claude/proj-a/sess-1/sess-1.jsonl") == b"hello\n"
    assert not (tmp_path / "cache").exists()


def test_path_traversal_blocked(mini_r2):
    # A foreign first segment is refused as an unconfigured bucket...
    with pytest.raises(ValueError):
        r2.get_object("../etc/passwd")
    with pytest.raises(ValueError):
        r2.get_object("proj-a/../../../etc/passwd")
    # ..while traversal inside the object-key part still hits
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
    # ..and the listing path hits the same join: '..' raises there too.
    with pytest.raises(PermissionError):
        list(r2.list_keys())


def test_scan_root_refuses_a_bucket_not_in_r2_bucket(monkeypatch, tmp_path):
    """CodeQL py/path-injection fix: _scan_root resolves its bucket
    against buckets() and builds the path from the CONFIGURED list
    element, not from the caller's string (which reaches it as the first
    segment of a stored file key via get_object/get_stream). A bucket
    outside CLAUDIT_R2_BUCKET raises _configured's ValueError before any path is
    built, and for a configured bucket the returned root is built from
    the configured name."""
    monkeypatch.setenv("CLAUDIT_R2_BUCKET", "claude")
    # Refusal: not in the list, no path is built at all (multi=True is
    # the None-returning case, multi=False the root fallback — both must
    # refuse before either answer is reachable).
    with pytest.raises(ValueError, match="is not configured in CLAUDIT_R2_BUCKET"):
        r2._scan_root(str(tmp_path), "codex", multi=True)  # pylint: disable=protected-access
    with pytest.raises(ValueError, match="is not configured in CLAUDIT_R2_BUCKET"):
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


@pytest.mark.parametrize("root_kind", ["missing", "file"],
                         ids=["missing-endpoint-root", "plain-file-root"])
def test_list_keys_refuses_an_invalid_endpoint_root(monkeypatch, tmp_path, root_kind):
    root = tmp_path / "mirror"
    if root_kind == "file":
        root.write_text("not a mirror", newline="\n")
    monkeypatch.setenv("CLAUDIT_R2_ENDPOINT", root.as_uri())
    monkeypatch.delenv("CLAUDIT_R2_BUCKET", raising=False)
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
        # realpath on both sides: the walk root is realpath-resolved by
        # _safe_join, and on macOS the temp dir sits behind the /var ->
        # /private/var symlink, so the raw spellings never compare
        # equal there and the denial would never fire.
        if os.path.realpath(str(path)) == os.path.realpath(str(victim)):
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


# ---------------------------------------------------------------------------
# The cached S3 client must not cross a fork: a thread that
# built its client and then forks hands the child the forking thread's
# thread-local — the same client object, and with it the same open TLS
# sockets. Two processes reading one SSL stream corrupt each other's
# records (ssl.SSLError: record layer failure), so every fork-started
# child must build its own client. The ingest parse pool no longer forks
# from the ingest thread (a preloaded forkserver); the guard
# still protects any fork-started child. No network: the probe compares
# object identity and the creator stamp inside a fork-context child, over
# a fixture mirror env.


_FORK_REF: dict = {}


def _fork_client_probe(conn) -> None:
    """Run in a forked child: report what `_boto_client()` returns.

    `_FORK_REF` is a module global, so the fork — not pickling — hands
    the child the parent's client reference, and the identity comparison
    stays in-process where `is` means the same object.
    """
    client = r2._boto_client()  # pylint: disable=protected-access
    conn.send({
        "same_object": client is _FORK_REF.get("client"),
        # The creator stamp lives in the thread-local beside the client.
        "creator_pid": getattr(r2._tls, "pid", None),  # pylint: disable=protected-access
        "child_pid": os.getpid(),
    })
    conn.close()


class TestBotoClientFork:
    @pytest.fixture(name="tls_state")
    def _tls_state_fixture(self):
        """Snapshot/restore this thread's cached-client state so the
        test's client never leaks into another test, and point
        CLAUDIT_R2_ENDPOINT at a well-formed, never-dialed URL: `_boto_client`
        refuses the suite's `file://` endpoint at construction, and the
        endpoint is only ever dialed by an actual GET, which no test
        here performs."""
        keys = ("client", "pid")
        saved = {k: getattr(r2._tls, k, None) for k in keys}  # pylint: disable=protected-access
        had = {k: hasattr(r2._tls, k) for k in keys}  # pylint: disable=protected-access
        saved_endpoint = os.environ["CLAUDIT_R2_ENDPOINT"]
        os.environ["CLAUDIT_R2_ENDPOINT"] = "https://r2.invalid/s3"
        yield
        os.environ["CLAUDIT_R2_ENDPOINT"] = saved_endpoint
        for k in keys:
            if had[k]:
                setattr(r2._tls, k, saved[k])  # pylint: disable=protected-access
            else:
                try:
                    delattr(r2._tls, k)  # pylint: disable=protected-access
                except AttributeError:
                    pass

    @pytest.mark.usefixtures("tls_state")
    def test_threads_build_clients_with_private_sessions(self, monkeypatch):
        monkeypatch.setenv("CLAUDIT_R2_ACCESS_KEY_ID", f"test-{os.getpid()}")
        monkeypatch.setenv("CLAUDIT_R2_SECRET_ACCESS_KEY", f"test-{os.getpid()}")
        factory = getattr(r2, "_boto_client")
        factory()
        barrier = threading.Barrier(3, timeout=10)

        def build() -> Session:
            sessions: list[Session] = []

            def observe(frame: FrameType, event: str, _arg: object) -> None:
                if event == "call" and frame.f_code is Session.client.__code__:
                    sessions.append(frame.f_locals["self"])

            previous = sys.getprofile()
            sys.setprofile(observe)
            try:
                barrier.wait()
                client = factory()
                try:
                    assert factory() is client
                finally:
                    client.close()
            finally:
                sys.setprofile(previous)
            assert len(sessions) == 1
            return sessions[0]

        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(build) for _ in range(3)]
            sessions = [future.result() for future in futures]
        assert len({id(session) for session in sessions}) == 3
        assert all(session is not boto3.DEFAULT_SESSION for session in sessions)

    @pytest.mark.skipif(
        sys.platform != "linux",
        reason="the guard is pinned on Linux; macOS has "
               "os.fork but forking this multithreaded suite process "
               "segfaults inside botocore client construction in the "
               "child (observed exitcode -11 on the CI runners), so the "
               "pin runs where the hazard is real; Windows skips for "
               "want of os.fork",
    )
    @pytest.mark.usefixtures("tls_state")
    def test_forked_child_builds_its_own_client(self):
        # The shape the guard defends: this thread builds a client, then
        # forks a child from it (the parse pool no longer does).
        parent_client = r2._boto_client()  # pylint: disable=protected-access
        _FORK_REF["client"] = parent_client
        ctx = multiprocessing.get_context("fork")
        recv, send = ctx.Pipe(duplex=False)
        child = ctx.Process(target=_fork_client_probe, args=(send,))
        try:
            child.start()
            child.join(timeout=60)
            if not recv.poll():
                pytest.fail(
                    f"probe child sent nothing (exitcode={child.exitcode})")
            report = recv.recv()
        finally:
            # A timed-out child must not outlive the test: kill and reap
            # it here, where the join's timeout already proved the wait
            # bounded, so the teardown itself cannot hang.
            if child.is_alive():
                child.kill()
                child.join(timeout=10)
            _FORK_REF.clear()
            send.close()
            recv.close()
        assert child.exitcode == 0, child.exitcode
        assert report["child_pid"] != os.getpid()
        assert report["same_object"] is False, report
        assert report["creator_pid"] == report["child_pid"], report
        # The parent's cache keeps its own client: the keep-alive pool
        # survives for the listing and any parent-side fetch.
        assert r2._boto_client() is parent_client  # pylint: disable=protected-access

    @pytest.mark.usefixtures("tls_state")
    def test_cached_client_carries_its_creator_pid(self):
        """The stamp is what lets a forked child tell an inherited client
        from its own; assert it in-process so the fork test's identity
        check has a visible, non-network second witness."""
        r2._boto_client()  # pylint: disable=protected-access
        assert getattr(r2._tls, "pid", None) == os.getpid()  # pylint: disable=protected-access
