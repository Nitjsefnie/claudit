"""Per-object R2 fetch outcomes: transient drops, corrupt payloads, bugs,
and objects that vanish between the listing and their GET.

Split out of test_ingest.py, whose fixtures it borrows, once that module
crossed pylint's line budget.
"""
from __future__ import annotations

import lzma
import multiprocessing
import os
import signal
import types
import threading
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
# The fixtures register on import; pylint only sees names nobody calls.
from test_ingest import (  # pylint: disable=unused-import
    _fresh_db_fixture, _mini_r2_env_fixture, _scalar, _FLAKY_KEY,
)

from backend import (api, app as app_mod, blob_cache, constants, db, events,
                     ingest, ingest_fetch, ingest_runs, timing)
from tests import mini_mirror

#: How many transcripts the committed mirror holds, read from its tree
#: (issue #503 grew it with the lane layout). A count written down here is
#: a count every future fixture addition makes wrong.
_MIRROR = mini_mirror.counts()["transcripts"]

# failed_keys is the PUBLIC key form returned for authenticated triage.
_FLAKY_PUBLIC_KEY = _FLAKY_KEY.split("/", 1)[1]


# ------------------------------------------- per-object R2 failures (#2)

def _patch_fetch(monkeypatch, key, fail_times, exc=None):
    """Make r2.get_object fail for `key` on its first `fail_times` calls.

    `exc` is the exception to raise, defaulting to a plain OSError.
    Returns (call_counts, slept) — the per-key GET count (the pooled path
    calls this from several threads, hence the lock) and the backoff sleeps
    the retry asked for, which are swallowed so the suite does not pay them.
    """
    real_get = ingest.r2.get_object
    counts: Counter = Counter()
    slept: list[float] = []
    lock = threading.Lock()

    def flaky(k):
        with lock:
            counts[k] += 1
            n = counts[k]
        if k == key and n <= fail_times:
            raise exc or OSError(f"connection reset while fetching {k}")
        return real_get(k)

    monkeypatch.setattr(ingest.r2, "get_object", flaky)
    monkeypatch.setattr(ingest.time, "sleep", slept.append)
    return counts, slept


@pytest.mark.parametrize("workers", [1, 4])
def test_one_failed_object_does_not_abort_the_run(
    fresh_db, mini_r2_env, monkeypatch, workers
):
    """A single unfetchable object costs that object, not the ingest.

    Both the sequential and the pooled path are exercised: they collect
    results differently (a generator consumed in the persist loop vs
    as_completed), and each used to let the exception escape.
    """
    monkeypatch.setenv("INGEST_WORKERS", str(workers))
    counts, _ = _patch_fetch(monkeypatch, _FLAKY_KEY, fail_times=99)
    recorded = []
    event_names = []
    real_broadcast = events.broadcast_threadsafe

    def record_broadcast(event, data):
        event_names.append(event)
        recorded.append(dict(data))
        real_broadcast(event, data)

    monkeypatch.setattr(events, "broadcast_threadsafe", record_broadcast)

    result = ingest.run_ingest(trigger="manual")

    assert result["failed"] == 1
    assert result["inserted"] == _MIRROR - 1, (
        "every file but the flaky one must still persist")
    assert result["failed_keys"] == [_FLAKY_PUBLIC_KEY]
    assert result["error"] == "1 object failed after retries"
    event_index = event_names.index("ingest_done")
    assert "failed_keys" not in recorded[event_index]
    assert counts[_FLAKY_KEY] == ingest.FETCH_ATTEMPTS
    with db.viz_conn() as c:
        keys = [r[0] for r in c.execute(
            "SELECT file_key FROM files ORDER BY file_key"
        ).fetchall()]
    assert _FLAKY_KEY not in keys
    assert len(keys) == _MIRROR - 1


def test_per_object_failure_still_rebuilds_derived_state(
    fresh_db, mini_r2_env, monkeypatch
):
    """The regression that matters: derived state must not be left stale.

    recompute_canonical() and the rollups describe whatever `records` now
    holds. Gating them on a flawless run meant one dropped connection left
    `usage_rollup` / `tool_rollup` describing the PREVIOUS dataset and
    is_canonical un-recomputed until some later run happened to be clean.
    """
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        rollup_before = _scalar(c, "SELECT COUNT(*) FROM usage_rollup")
        dupes_before = _scalar(c, "SELECT COUNT(*) FROM records WHERE NOT is_canonical")
    assert rollup_before > 0 and dupes_before > 0, "fixture proves nothing"

    with db.viz_conn() as c:
        # Tool calls hang off the file whose fetch fails, so the reparse
        # never deletes them and tool_rollup has something to rebuild from.
        c.execute(
            "INSERT INTO tool_uses (file_key, line_num, idx, ts, tool_name, "
            "is_error) VALUES (%s, 9001, 0, now(), 'Read', false)",
            (_FLAKY_KEY,),
        )
        # Wreck every piece of derived state, then prove the run restores it.
        c.execute("TRUNCATE usage_rollup")
        c.execute("TRUNCATE tool_rollup")
        c.execute("UPDATE records SET is_canonical = TRUE")
        c.commit()

    # A forward bump forces the reparse; a rollback would now be skipped
    # by the guard (issue #118).
    monkeypatch.setattr(
        constants, "PARSER_VERSION", str(int(constants.PARSER_VERSION) + 1))
    _patch_fetch(monkeypatch, _FLAKY_KEY, fail_times=99)
    result = ingest.run_ingest(trigger="manual")
    assert result["failed"] == 1
    assert result["reparsed"] == _MIRROR - 1

    with db.viz_conn() as c:
        rollup_after = _scalar(c, "SELECT COUNT(*) FROM usage_rollup")
        dupes_after = _scalar(c, "SELECT COUNT(*) FROM records WHERE NOT is_canonical")
        tool_rollup_after = _scalar(c, "SELECT COUNT(*) FROM tool_rollup")
    assert rollup_after == rollup_before, "usage_rollup was not rebuilt"
    assert dupes_after == dupes_before, "is_canonical was not recomputed"
    assert tool_rollup_after > 0, "tool_rollup was not rebuilt"


def test_ctx_cost_rollup_totals_equal_the_live_aggregate(
    fresh_db, mini_r2_env
):
    """SV-ROLLUP: the pre-aggregate stands in for a live pass, so the
    per-bucket request counts and cost must match it exactly.

    The live expression is written out longhand rather than reusing the
    builder's, so a wrong bucket expression cannot agree with itself.
    """
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        live = c.execute(
            """
            SELECT CASE
                     WHEN fresh_tokens + cache_creation_tokens
                          + cache_read_tokens >= 1000000 THEN 1000000
                     WHEN fresh_tokens + cache_creation_tokens
                          + cache_read_tokens <= 0 THEN 0
                     ELSE ((fresh_tokens + cache_creation_tokens
                            + cache_read_tokens) / 50000) * 50000
                   END AS ctx_bucket,
                   COUNT(*), SUM(cost_usd)
              FROM records
             WHERE is_canonical AND ts IS NOT NULL
             GROUP BY 1 ORDER BY 1
            """
        ).fetchall()
        rolled = c.execute(
            "SELECT ctx_bucket, SUM(requests), SUM(cost_usd) "
            "FROM ctx_cost_rollup GROUP BY 1 ORDER BY 1"
        ).fetchall()
    assert live, "fixture produced no records - the test proves nothing"
    assert [(int(b), int(n), round(cost, 6)) for b, n, cost in rolled] == \
           [(int(b), int(n), round(cost, 6)) for b, n, cost in live]


def test_ctx_cost_rollup_counts_only_canonical_rows(fresh_db, mini_r2_env):
    """It reads is_canonical, so the mini mirror's cross-file duplicate
    must be counted once, not twice."""
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        dupes = _scalar(
            c, "SELECT COUNT(*) FROM records WHERE NOT is_canonical")
        rolled = _scalar(c, "SELECT SUM(requests) FROM ctx_cost_rollup")
        canonical = _scalar(
            c, "SELECT COUNT(*) FROM records "
               "WHERE is_canonical AND ts IS NOT NULL")
    assert dupes > 0, "fixture has no duplicates - the test proves nothing"
    assert rolled == canonical


def test_ctx_cost_rollup_is_rebuilt_after_state_reset(fresh_db, mini_r2_env):
    """Clearing derived state forces a full rebuild without reparsing."""
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        before = _scalar(c, "SELECT COUNT(*) FROM ctx_cost_rollup")
    assert before > 0, "fixture proves nothing"
    with db.viz_conn() as c:
        c.execute("TRUNCATE ctx_cost_rollup")
        c.execute("DELETE FROM ingest_derived_state")
        c.commit()
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        after = _scalar(c, "SELECT COUNT(*) FROM ctx_cost_rollup")
    assert after == before, "ctx_cost_rollup was not rebuilt"


def test_retry_recovers_then_gives_up(fresh_db, mini_r2_env, monkeypatch):
    """The retry policy's two arms over one flaky key: two failed GETs
    then a good one land the file with a clean run; an always-dead
    object is given up on after the third attempt, booked as the run's
    one failure."""
    counts, slept = _patch_fetch(monkeypatch, _FLAKY_KEY, fail_times=2)

    result = ingest.run_ingest(trigger="manual")

    assert result["failed"] == 0
    assert result["error"] is None
    assert result["inserted"] == _MIRROR
    assert counts[_FLAKY_KEY] == 3
    assert slept == [0.5, 1.0], "exponential backoff between attempts"
    with db.viz_conn() as c:
        n = _scalar(c, "SELECT COUNT(*) FROM files WHERE file_key = %s", (_FLAKY_KEY,))
    assert n == 1

    # Force the give-up arm's re-fetch: the first run stored the etag.
    with db.viz_conn() as c:
        c.execute("DELETE FROM files")
        c.execute("DELETE FROM projects")
        c.commit()

    counts, slept = _patch_fetch(monkeypatch, _FLAKY_KEY, fail_times=99)

    result = ingest.run_ingest(trigger="manual")

    assert counts[_FLAKY_KEY] == 3
    assert slept == [0.5, 1.0]
    assert result["failed"] == 1
    assert result["error"] == "1 object failed after retries"


_CORRUPT_XZ_KEY = "claude/projC/sess-E/sess-E.jsonl.xz"


def test_a_corrupt_xz_object_is_one_failure_not_a_dead_run(
    fresh_db, mini_r2_env, monkeypatch
):
    """Real invalid bytes under a `.xz` key, not a monkeypatched raise.

    r2.get_object inflates `.xz` transparently, so lzma raises from inside
    the fetch — and lzma.LZMAError is not an OSError. Every production
    object is `.xz`, so classifying it as "not transient, therefore a bug"
    would abort the entire run over one truncated upload: this issue's
    original failure mode, for 100% of the bucket.
    """
    corrupt = mini_r2_env / "projC" / "sess-E" / "sess-E.jsonl.xz"
    corrupt.parent.mkdir(parents=True)
    corrupt.write_bytes(b"\xfd7zXZ\x00 this is not a valid xz stream \x00\x01")
    with pytest.raises(lzma.LZMAError):
        lzma.decompress(corrupt.read_bytes())   # the fixture must really be corrupt

    counts, slept = _patch_fetch(monkeypatch, _CORRUPT_XZ_KEY, fail_times=0)

    result = ingest.run_ingest(trigger="manual")

    assert result["r2_listed"] == _MIRROR + 1  # the corrupt object it adds
    assert result["failed"] == 1
    assert result["error"] == "1 object failed after retries"
    assert result["inserted"] == _MIRROR, "the intact objects are still persisted"
    assert counts[_CORRUPT_XZ_KEY] == 1, "a corrupt object must not be re-fetched"
    assert not slept, "and must not sleep between attempts it does not make"
    with db.viz_conn() as c:
        rollup = _scalar(c, "SELECT COUNT(*) FROM usage_rollup")
        stored = _scalar(c, "SELECT COUNT(*) FROM files WHERE file_key = %s", (_CORRUPT_XZ_KEY,))
    assert rollup > 0, "derived state must still be rebuilt"
    assert stored == 0


def test_a_programming_error_in_the_fetch_is_not_retried(
    fresh_db, mini_r2_env, monkeypatch
):
    """A bug is not a transient, and must not be dressed up as one.

    Retrying a TypeError sleeps 1.5s per object and books it as a
    per-object failure — at 9,213 objects that is a silent multi-hour
    "partial run" instead of a server-log traceback. The stored fatal
    text stays static; the logged traceback retains the TypeError detail.
    """
    counts, slept = _patch_fetch(
        monkeypatch, _FLAKY_KEY, fail_times=99,
        exc=TypeError(
            f"unexpected object: {_FLAKY_PUBLIC_KEY}/x.jsonl"),
    )

    result = ingest.run_ingest(trigger="manual")

    assert counts[_FLAKY_KEY] == 1, "a bug must not be retried"
    assert not slept, "and must not sleep"
    assert result["failed"] == 0, "it is not a per-object failure"
    assert result["error"].startswith("FatalFetchError:"), result["error"]
    project, session = _FLAKY_PUBLIC_KEY.split("/")[:2]
    assert project not in result["error"]
    assert session not in result["error"]
    a = FastAPI()
    a.include_router(api.router)
    a.get("/health")(app_mod.health)
    body = TestClient(a).get("/health").text
    assert project not in body
    assert session not in body
    assert result["error"] == (
        "FatalFetchError: details are in the server log"), \
        "the stored fatal text is static; detail lives in the server log"
    with db.viz_conn() as c:
        rollup = _scalar(c, "SELECT COUNT(*) FROM usage_rollup")
    assert rollup == 0, "a fatal run must not rebuild derived state"


def test_a_parse_failure_is_not_retried(fresh_db, mini_r2_env, monkeypatch):
    """Parsing is deterministic: re-fetching the same bytes buys nothing."""
    real_get = ingest.r2.get_object
    counts: Counter = Counter()
    lock = threading.Lock()

    def counting(k):
        with lock:
            counts[k] += 1
        return real_get(k)

    real_parse = ingest.parse.parse_file

    def boom(file_key, blob):
        if file_key == _FLAKY_KEY:
            raise ValueError("malformed line 3")
        return real_parse(file_key, blob)

    monkeypatch.setattr(ingest.r2, "get_object", counting)
    monkeypatch.setattr(ingest.parse, "parse_file", boom)
    monkeypatch.setattr(ingest.time, "sleep", lambda s: pytest.fail(
        "a parse failure must not sleep on a retry"
    ))

    result = ingest.run_ingest(trigger="manual")

    assert counts[_FLAKY_KEY] == 1, "the GET must not be repeated"
    assert result["failed"] == 1
    assert result["inserted"] == _MIRROR - 1
    assert "ValueError" not in (result["error"] or "")


def test_failure_summary_counts_every_failure():
    """A count-only error includes failures beyond the former key limit."""
    failed = [(f"p/s{i}/s{i}.jsonl", "OSError: boom") for i in range(9)]
    summary_builder = getattr(ingest_runs, "failure_summary", None)
    assert callable(summary_builder), "failure_summary belongs in ingest_runs"
    summary = summary_builder(failed)
    assert summary is not None
    assert summary == "9 objects failed after retries"
    assert summary_builder([]) is None


# ------------------------------------------- objects that vanish mid-run

def _no_such_key(key):
    """The ClientError boto3 raises when a listed object is gone by GET."""
    return ingest.ClientError(
        {"Error": {"Code": "NoSuchKey",
                   "Message": "The specified key does not exist."}},
        "GetObject",
    )


@pytest.mark.parametrize("exc_factory", [
    _no_such_key,
    lambda key: FileNotFoundError(2, "No such file or directory", key),
], ids=["s3-NoSuchKey", "file-mode-ENOENT"])
def test_an_object_deleted_between_list_and_fetch_is_not_a_failure(
    fresh_db, mini_r2_env, monkeypatch, exc_factory
):
    """The archiver moves objects between buckets while a run is in flight.

    A key that was listed at the start and is gone by the time its GET
    happens is not a transient drop (retrying cannot bring it back) and
    not a failure of this run (the next listing will not show it): it is
    an orphan that appeared early. It must not sleep through the backoff,
    must not land in ingest_runs.error, and any row the previous run left
    for it must be swept with the other orphans rather than kept for an
    hour as a stale file.
    """
    # A previous run stored the key so the vanish leaves a stale row.
    ingest.run_ingest(trigger="manual")
    # A changed parser_version makes the stored files stale; derived from
    # the committed constant so the bump target can never collide with it
    # (issue #198).
    monkeypatch.setattr(
        constants, "PARSER_VERSION",
        str(int(constants.PARSER_VERSION) + 1))
    counts, slept = _patch_fetch(
        monkeypatch, _FLAKY_KEY, fail_times=99, exc=exc_factory(_FLAKY_KEY)
    )

    result = ingest.run_ingest(trigger="manual")

    assert counts[_FLAKY_KEY] == 1, "a vanished object must not be re-fetched"
    assert not slept, "and must not sleep between attempts it does not make"
    assert result["failed"] == 0, "it is not a per-object failure"
    assert result["error"] is None, result["error"]
    assert result["vanished"] == 1
    assert result["reparsed"] == _MIRROR - 1, "every other file is still reparsed"
    assert result["deleted"] == 1, "its stale row is swept with the orphans"
    with db.viz_conn() as c:
        stored = _scalar(c, "SELECT COUNT(*) FROM files WHERE file_key = %s", (_FLAKY_KEY,))
        rollup = _scalar(c, "SELECT COUNT(*) FROM usage_rollup")
    assert stored == 0
    assert rollup > 0, "derived state must still be rebuilt"


# ------------------------------------------ parse-worker signal hygiene (#373)

def _noop_parse(key, _sidecar_key=None, _etag=None, _size=None):
    """A parse unit with no side effects, for pipeline plumbing tests."""
    return {}


def _worker_report():
    """Run inside the forked worker: its handlers and parent pid."""
    return (signal.getsignal(signal.SIGTERM),
            signal.getsignal(signal.SIGINT),
            os.getppid())


def _fork_pool(**kwargs):
    """A one-worker fork pool with the production initializer wired."""
    return ProcessPoolExecutor(
        max_workers=1,
        mp_context=multiprocessing.get_context("fork"),
        initializer=ingest_fetch.parse_worker_init,
        **kwargs)


@pytest.mark.skipif(not hasattr(os, "fork"),
                    reason="the parse pool is fork-based")
def test_parse_worker_init_and_its_sigterm_defense():
    """The #373 defense: a forked worker inherits uvicorn's flag-only
    SIGTERM handler, so the initializer must restore SIG_DFL — and a
    cgroup stop must then kill the worker outright."""
    with _fork_pool(initargs=(os.getpid(),)) as pool:
        handler, int_handler, ppid = pool.submit(
            _worker_report).result(timeout=60)
        assert handler is signal.SIG_DFL
        assert int_handler is signal.SIG_DFL
        assert ppid == os.getpid()
        worker_pid = pool.submit(os.getpid).result(timeout=60)
        os.kill(worker_pid, signal.SIGTERM)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                os.kill(worker_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            pytest.fail("a parse worker survived SIGTERM")


@pytest.mark.skipif(not hasattr(os, "fork"),
                    reason="the parse pool is fork-based")
def test_parse_worker_exits_when_the_parent_is_already_gone():
    """PDEATHSIG is armed after fork, so a parent dying in that window
    would leave an orphan; the initializer's ppid check closes the race by
    exiting any worker whose parent is not the one that forked it."""
    with _fork_pool(initargs=(-1,)) as pool:
        with pytest.raises(BrokenProcessPool):
            pool.submit(int).result(timeout=60)


@pytest.mark.skipif(not hasattr(os, "fork"),
                    reason="the parse pool is fork-based")
def test_abort_cancels_the_queued_persist_half(fresh_db, mini_r2_env,
                                               monkeypatch):
    """An abort must drop the QUEUED work, not only stop taking new chunks:
    a pipeline_pool abort fires at a chunk top, when the PREVIOUS chunk's
    persists are still queued. The queued persists here park on an event
    that the test sets only after pipeline_pool has returned — so draining
    instead of cancelling would deadlock the abort unwind instead of
    letting it finish, which is the failure this pin reports. The persist
    in flight must complete (the oracle is live); the queued ones must
    never run."""
    entered_first = threading.Event()
    release_first = threading.Event()
    release_queued = threading.Event()
    persisted: list[str] = []

    def persist_call(obj, _proj, _parsed, _version, _current):
        if obj.key.endswith("first-0.jsonl"):
            entered_first.set()
            release_first.wait(timeout=60)
            persisted.append(obj.key)
        else:
            # A queued persist that ran instead of being cancelled parks
            # here, so the unwind cannot finish while it waits.
            release_queued.wait(timeout=60)
            persisted.append(obj.key)

    checks = iter([False, True])  # first chunk top passes, second aborts

    def check_shutdown():
        if next(checks):
            raise ingest_fetch.IngestAborted("shutdown requested")

    monkeypatch.setattr(ingest_fetch, "parse_process_count", lambda: 1)
    monkeypatch.setattr(ingest_fetch, "persist_thread_count", lambda: 1)
    todo = [(types.SimpleNamespace(key=f"chunk/first-{i}.jsonl",
                                   sidecar_key=None, etag="e", size=1), None,
             None)
            for i in range(5)]  # chunk is processes*4 = 4: 4 + 1 items
    box: dict = {}

    def run():
        try:
            box["r"] = ingest_fetch.pipeline_pool(
                todo, "v", [], None, None, {}, set(), set(),
                _noop_parse, persist_call, check_shutdown)
        except BaseException as exc:  # noqa: BLE001
            box["exc"] = exc

    worker = threading.Thread(target=run)
    worker.start()
    assert entered_first.wait(timeout=30), "the first persist never entered"
    # The canceller reaches the queue within moments of the abort (the
    # chunk-top raise precedes the in-flight persist's release), while the
    # in-flight persist parks the only pool worker, so the queue is frozen
    # until this release: waiting here is what makes the cancellation
    # deterministic instead of a dequeue race.
    worker.join(timeout=5)
    assert worker.is_alive(), (
        "the unwind finished without the in-flight persist (it must wait "
        "for it)")
    release_first.set()
    worker.join(timeout=60)
    if worker.is_alive():
        release_queued.set()
        worker.join(timeout=60)
        pytest.fail("the abort waited on the queued persists "
                    "(the cancel_futures shutdowns are missing)")
    release_queued.set()  # anything not cancelled would now run and record
    assert isinstance(box.get("exc"), ingest_fetch.IngestAborted), box
    assert persisted == ["chunk/first-0.jsonl"], (
        f"queued persists survived the abort: {persisted}")


# --------------------------------- fetch_parse stage accounting (#662)

def test_parse_wire_books_child_stages(monkeypatch):
    """The #662 booking and channel: fetch_and_parse writes a file's
    child work into an accumulator (inert without one), parse_wire
    plants it for the timed child and returns (parsed, stages), the
    sidecar fetch booking beside fetch/parse. Fakes throughout — the
    real fork pool is the pool TIMING-line test's."""
    monkeypatch.setattr(timing, "TIMING_ON", True)

    def fetch(key):
        return b"payload"

    def parse_file(key, data):
        return {"agent_type_in_band": True, "data": data}

    stages: dict = {}
    parsed = ingest_fetch.fetch_and_parse("k", None, fetch, parse_file,
                                          stages=stages)
    assert parsed == {"agent_type_in_band": True, "data": b"payload"}
    assert set(stages) == {"child_fetch", "child_decompress", "child_parse"}
    assert stages["child_fetch"] > 0.0  # pylint: disable=invalid-sequence-index
    assert stages["child_parse"] > 0.0  # pylint: disable=invalid-sequence-index
    bare_call = ingest_fetch.fetch_and_parse("k", None, fetch, parse_file)
    assert bare_call == parsed, "the bare call must behave exactly as before"

    item = (types.SimpleNamespace(key="claude/p/s/k.jsonl",
                                  sidecar_key=None, etag="e1", size=8),
            None, None)

    def parse_call(key, sidecar_key, etag=None, size=None):
        return ingest_fetch.fetch_and_parse(key, sidecar_key, fetch,
                                            parse_file)

    result = ingest_fetch.parse_wire(item, parse_call, True)
    assert isinstance(result, tuple)
    wired_parsed, wired_stages = result
    assert wired_parsed == parsed
    assert set(wired_stages) == set(stages)

    def sidecar_call(key, sidecar_key, etag=None, size=None):
        return ingest_fetch.fetch_and_parse(
            key, sidecar_key, lambda k: b"main-bytes",
            lambda k, d: {"agent_type_in_band": False, "data": d})

    item = (types.SimpleNamespace(key="claude/p/s/k.jsonl",
                                  sidecar_key="sc", etag="e1", size=8),
            None, None)
    result = ingest_fetch.parse_wire(item, sidecar_call, True)
    assert isinstance(result, tuple)
    sc_parsed, sc_stages = result
    assert sc_parsed["data"] == b"main-bytes"  # pylint: disable=invalid-sequence-index
    assert sc_stages["child_sidecar"] > 0.0  # pylint: disable=invalid-sequence-index
    assert set(sc_stages) == {"child_fetch", "child_decompress",
                              "child_parse", "child_sidecar"}
    bare = ingest_fetch.parse_wire(item, sidecar_call, False)
    assert isinstance(bare, dict)


# --------------------------------------- the disk blob cache (#684)

def test_fetch_and_parse_forwards_the_listing_identity_only_when_cached(
        monkeypatch, tmp_path):
    """The etag/size pair travels to the fetch callable only when the
    deploy enabled the blob cache: with it off (the suite's default), the
    original fetch(key) shape is spoken and a patched single-arg fetch
    callable keeps working."""
    calls: list[tuple] = []

    def fetch(key, *rest):
        calls.append((key, rest))
        return b"payload"

    def parse_file(key, data):
        return {"agent_type_in_band": True, "data": data}

    ingest_fetch.fetch_and_parse("k", None, fetch, parse_file,
                                 etag="e1", size=8)
    assert calls == [("k", ())]

    monkeypatch.setenv("R2_BLOB_CACHE", str(tmp_path / "cache"))
    ingest_fetch.fetch_and_parse("k", None, fetch, parse_file,
                                 etag="e1", size=8)
    assert calls[-1] == ("k", ("e1", 8))


def test_fetch_and_parse_answers_a_cache_hit_without_a_fetch(
        monkeypatch, tmp_path):
    """The money path: a cached (key, etag) parses identically with the
    object's file gone from the mirror — the GET never happened."""
    mirror = tmp_path / "claude" / "p" / "s"
    mirror.mkdir(parents=True)
    body = b"x\n"
    (mirror / "w.jsonl").write_bytes(body)
    monkeypatch.setenv("R2_ENDPOINT", f"{(tmp_path).as_uri()}/")
    monkeypatch.setenv("R2_BUCKET", "claude")
    monkeypatch.setenv("R2_BLOB_CACHE", str(tmp_path / "cache"))
    key = "claude/p/s/w.jsonl"
    # The real fetch callable, no spy: the consult sits below it, inside
    # r2.get_object, so "no second fetch" is proven by the second call
    # succeeding with the object's file GONE — only a cache hit can.
    def parse_file(key, data):
        return {"agent_type_in_band": True, "data": data}

    first = ingest_fetch.fetch_and_parse(
        key, None, ingest_fetch.fetch_with_retry, parse_file,
        etag="e1", size=len(body))
    (mirror / "w.jsonl").unlink()
    second = ingest_fetch.fetch_and_parse(
        key, None, ingest_fetch.fetch_with_retry, parse_file,
        etag="e1", size=len(body))
    assert second == first


def test_parse_wire_passes_the_listing_identity():
    """The pool child's unit carries the item's etag/size to the parse
    seam, whatever the seam does with them."""
    recorded: list[tuple] = []

    def parse_call(key, sidecar_key, etag=None, size=None):
        recorded.append((key, sidecar_key, etag, size))
        return {}

    item = (types.SimpleNamespace(key="claude/p/s/k.jsonl",
                                  sidecar_key=None, etag="e9", size=7),
            None, None)
    ingest_fetch.parse_wire(item, parse_call, False)
    assert recorded == [("claude/p/s/k.jsonl", None, "e9", 7)]


def test_ingest_run_populates_and_prunes_the_blob_cache(
        fresh_db, mini_r2_env, monkeypatch, tmp_path):
    """The run wiring: fetches store entries and the run ends with a
    prune, so the cache on a live meter stays under its cap without a
    separate sweeper."""
    cache_dir = tmp_path / "cache"
    monkeypatch.setenv("R2_BLOB_CACHE", str(cache_dir))
    pruned: list[int] = []
    monkeypatch.setattr(blob_cache, "prune",
                        lambda *a, **k: pruned.append(1) or 0)
    result = ingest.run_ingest(trigger="manual")
    assert result["failed"] == 0
    assert pruned == [1]
    assert any(cache_dir.rglob("*")), "the run's fetches stored nothing"
