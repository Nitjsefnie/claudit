"""Per-object R2 fetch outcomes: transient drops, corrupt payloads, bugs,
and objects that vanish between the listing and their GET.

Split out of test_ingest.py, whose fixtures it borrows, once that module
crossed pylint's line budget.
"""
import lzma
import threading
from collections import Counter

import pytest
# The fixtures register on import; pylint only sees names nobody calls.
from test_ingest import (  # pylint: disable=unused-import
    _fresh_db_fixture, _mini_r2_env_fixture, _scalar, _FLAKY_KEY,
)

from backend import constants, db, ingest

# failure_summary serves the PUBLIC key form (the error column feeds the
# public /health), so assertions on it strip the configured bucket.
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

    result = ingest.run_ingest(trigger="manual")

    assert result["failed"] == 1
    assert result["inserted"] == 4, "the other four files must still persist"
    assert result["error"] == (
        f"1 object failed after retries: {_FLAKY_PUBLIC_KEY}")
    assert counts[_FLAKY_KEY] == ingest.FETCH_ATTEMPTS
    with db.viz_conn() as c:
        keys = [r[0] for r in c.execute(
            "SELECT file_key FROM files ORDER BY file_key"
        ).fetchall()]
    assert _FLAKY_KEY not in keys
    assert len(keys) == 4


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
    assert result["reparsed"] == 4

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


def test_ctx_cost_rollup_is_rebuilt_after_every_run(fresh_db, mini_r2_env):
    """Derived state: wrecking it and re-running must restore it, the
    same guarantee usage_rollup and tool_rollup carry."""
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        before = _scalar(c, "SELECT COUNT(*) FROM ctx_cost_rollup")
    assert before > 0, "fixture proves nothing"
    with db.viz_conn() as c:
        c.execute("TRUNCATE ctx_cost_rollup")
        c.commit()
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        after = _scalar(c, "SELECT COUNT(*) FROM ctx_cost_rollup")
    assert after == before, "ctx_cost_rollup was not rebuilt"


def test_a_transient_fetch_failure_is_retried_and_recovers(
    fresh_db, mini_r2_env, monkeypatch
):
    """Two failed GETs then a good one: the file lands, the run is clean."""
    counts, slept = _patch_fetch(monkeypatch, _FLAKY_KEY, fail_times=2)

    result = ingest.run_ingest(trigger="manual")

    assert result["failed"] == 0
    assert result["error"] is None
    assert result["inserted"] == 5
    assert counts[_FLAKY_KEY] == 3
    assert slept == [0.5, 1.0], "exponential backoff between attempts"
    with db.viz_conn() as c:
        n = _scalar(c, "SELECT COUNT(*) FROM files WHERE file_key = %s", (_FLAKY_KEY,))
    assert n == 1


def test_fetch_gives_up_after_three_attempts(
    fresh_db, mini_r2_env, monkeypatch
):
    """The retry is bounded — it must not spin on a genuinely dead object."""
    counts, slept = _patch_fetch(monkeypatch, _FLAKY_KEY, fail_times=99)

    result = ingest.run_ingest(trigger="manual")

    assert counts[_FLAKY_KEY] == 3
    assert slept == [0.5, 1.0]
    assert result["failed"] == 1
    assert "connection reset" not in (result["error"] or ""), \
        "the summary names keys, not stack noise"
    assert _FLAKY_PUBLIC_KEY in result["error"]


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

    assert result["r2_listed"] == 6
    assert result["failed"] == 1
    assert result["error"] == (
        f"1 object failed after retries: "
        f"{_CORRUPT_XZ_KEY.split('/', 1)[1]}"
    )
    assert result["inserted"] == 5, "the intact objects are still persisted"
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
    "partial run" instead of one loud traceback.
    """
    counts, slept = _patch_fetch(
        monkeypatch, _FLAKY_KEY, fail_times=99,
        exc=TypeError("get_object() takes 1 positional argument but 2 were given"),
    )

    result = ingest.run_ingest(trigger="manual")

    assert counts[_FLAKY_KEY] == 1, "a bug must not be retried"
    assert not slept, "and must not sleep"
    assert result["failed"] == 0, "it is not a per-object failure"
    assert result["error"].startswith("FatalFetchError:"), result["error"]
    assert "TypeError" in result["error"], "the type must survive into the run"
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
    assert result["inserted"] == 4
    assert "ValueError" not in (result["error"] or "")


def test_failure_summary_truncates_a_long_key_list():
    """A 9,213-file run losing its connection must not write a novel into
    ingest_runs.error."""
    failed = [(f"p/s{i}/s{i}.jsonl", "OSError: boom") for i in range(9)]
    summary = ingest.failure_summary(failed)
    assert summary is not None
    assert summary.startswith("9 objects failed after retries: ")
    assert summary.endswith(", ... (+4 more)")
    assert summary.count(".jsonl") == ingest.FAILURE_KEYS_IN_SUMMARY
    assert ingest.failure_summary([]) is None


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
    assert result["reparsed"] == 4, "the other four files are still reparsed"
    assert result["deleted"] == 1, "its stale row is swept with the orphans"
    with db.viz_conn() as c:
        stored = _scalar(c, "SELECT COUNT(*) FROM files WHERE file_key = %s", (_FLAKY_KEY,))
        rollup = _scalar(c, "SELECT COUNT(*) FROM usage_rollup")
    assert stored == 0
    assert rollup > 0, "derived state must still be rebuilt"
