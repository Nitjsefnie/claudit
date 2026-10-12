"""The process-pool pipeline (issue #309): knob parsing, equality with
the in-process pipeline over the mini mirror, and per-file persist
failure isolation there."""
from __future__ import annotations

import logging
import lzma
import re
import shutil
from functools import partial

from test_ingest import (  # pylint: disable=unused-import
    _fresh_db_fixture,
    _mini_r2_env_fixture,
)
from test_ingest import _FLAKY_KEY, _scalar, _snapshot

from backend import db, ingest, pricing, pricing_load
from backend.ingest_fetch import VanishedObject
from backend import timing as _timing

from tests import mini_mirror
from tests import ingest_process_helpers as child_units

#: How many transcripts the committed mirror holds, read from its
#: tree (issue #503 grew it with the lane layout).
_MIRROR = mini_mirror.counts()["transcripts"]


def test_parse_process_count_defaults_and_clamps(monkeypatch):
    monkeypatch.delenv("CLAUDIT_INGEST_PARSE_PROCESSES", raising=False)
    assert ingest.parse_process_count() >= 1
    monkeypatch.setenv("CLAUDIT_INGEST_PARSE_PROCESSES", "0")
    assert ingest.parse_process_count() == 1
    monkeypatch.setenv("CLAUDIT_INGEST_PARSE_PROCESSES", "3")
    assert ingest.parse_process_count() == 3
    monkeypatch.setenv("CLAUDIT_INGEST_PARSE_PROCESSES", "not-a-number")
    assert ingest.parse_process_count() >= 1


def test_process_pool_ingest_matches_sequential_exactly(
        fresh_db, mini_r2_env, monkeypatch, tmp_path):
    """The process-pool path must land exactly what the in-process path
    lands: parse runs in forkserver children and persist in pool threads, but
    the per-file transaction, failure isolation, and derived state are the
    same — one run per path over the identical mirror, snapshots compared.
    """
    monkeypatch.setenv("CLAUDIT_INGEST_WORKERS", "1")
    monkeypatch.setenv("CLAUDIT_INGEST_PARSE_PROCESSES", "1")
    monkeypatch.setenv("CLAUDIT_INGEST_PERSIST_THREADS", "1")
    ingest.run_ingest("test-seq")
    sequential = _snapshot()

    # Wipe and re-ingest the identical mirror through the process pool.
    with db.viz_conn() as c:
        c.execute("DELETE FROM files")
        c.execute("DELETE FROM projects")
        c.commit()

    monkeypatch.setenv("CLAUDIT_INGEST_PARSE_PROCESSES", "2")
    monkeypatch.setenv("CLAUDIT_INGEST_PERSIST_THREADS", "2")
    cache_dir = tmp_path / "cache"
    monkeypatch.setenv("CLAUDIT_R2_BLOB_CACHE", str(cache_dir))
    # Share runtime-bound fixtures too (the suite bench's bounded rates),
    # rather than letting a fresh child reload different pricing data.
    tables = {name: getattr(pricing, name)
              for name in pricing_load.RateTables.__annotations__}
    monkeypatch.setattr(ingest, "_fetch_and_parse", partial(
        child_units.priced_parse, tables=tables))
    summary = ingest.run_ingest("test-proc")
    parallel = _snapshot()

    assert parallel[0] == sequential[0], "files differ"
    assert parallel[1] == sequential[1], "records differ"
    assert parallel[2] == sequential[2], "tool_uses differ"
    assert parallel[3] == sequential[3], "projects differ"
    assert len(sequential[1]) > 0, "fixture produced no records — vacuous test"
    assert summary["error"] is None
    assert summary["failed"] == 0
    assert any(p.is_file() for p in cache_dir.rglob("*")), (
        "process fetches did not populate the cache")


def test_process_pool_persists_survive_a_per_file_failure(
        fresh_db, mini_r2_env, monkeypatch):
    """One file's persist failure must not take the run (or the pool) down:
    the failed file is booked per-object and every other file still lands.
    """
    monkeypatch.setenv("CLAUDIT_INGEST_WORKERS", "1")
    monkeypatch.setenv("CLAUDIT_INGEST_PARSE_PROCESSES", "2")
    monkeypatch.setenv("CLAUDIT_INGEST_PERSIST_THREADS", "2")
    real = ingest._persist  # pylint: disable=protected-access

    def flaky(obj, proj, parsed, parser_version):
        if obj.key != _FLAKY_KEY:
            raise RuntimeError("persist boom")
        return real(obj, proj, parsed, parser_version)

    monkeypatch.setattr(ingest, "_persist", flaky)
    summary = ingest.run_ingest("test-proc-fail")

    assert summary["failed"] >= 1
    assert "persist boom" not in (summary["error"] or "")
    with db.viz_conn() as c:
        kept = c.execute(
            "SELECT file_key FROM files WHERE file_key = %s", (_FLAKY_KEY,)
        ).fetchall()
        total = _scalar(c, "SELECT count(*) FROM files")
    assert kept, "the survivor file did not land"
    assert total == 1, "files beyond the survivor landed despite the failure"


def test_process_pool_books_a_parse_failure_per_file(
        fresh_db, mini_r2_env, monkeypatch):
    """A fetch failure in a forkserver child books its own file only; the run
    answers without a fatal error and every other file still lands.
    """
    monkeypatch.setenv("CLAUDIT_INGEST_WORKERS", "1")
    monkeypatch.setenv("CLAUDIT_INGEST_PARSE_PROCESSES", "2")
    monkeypatch.setattr(ingest, "_fetch_and_parse", partial(
        child_units.fetch_failure, failed_key=_FLAKY_KEY, error=OSError))
    summary = ingest.run_ingest("test-proc-parse-fail")

    assert summary["failed"] == 1
    assert summary["error"] is not None
    with db.viz_conn() as c:
        landed = _scalar(c, "SELECT count(*) FROM files")
    assert landed == _MIRROR - 1, "the flaky file's peers did not land"


def test_process_pool_treats_a_vanished_object_as_not_a_failure(
        fresh_db, mini_r2_env, monkeypatch):
    """A key that disappears between list and fetch is discarded from the
    orphan sweep's seen set, not booked as a failure.
    """
    monkeypatch.setenv("CLAUDIT_INGEST_WORKERS", "1")
    monkeypatch.setenv("CLAUDIT_INGEST_PARSE_PROCESSES", "2")
    monkeypatch.setattr(ingest, "_fetch_and_parse", partial(
        child_units.fetch_failure, failed_key=_FLAKY_KEY, error=VanishedObject))
    summary = ingest.run_ingest("test-proc-vanished")

    assert summary["error"] is None
    assert summary["failed"] == 0
    assert summary["vanished"] == 1
    with db.viz_conn() as c:
        doomed = _scalar(
            c,
            "SELECT count(*) FROM files WHERE file_key = %s", (_FLAKY_KEY,))
    assert doomed == 0, "the vanished key survived as a files row"


def test_process_pool_a_broken_fetch_is_fatal(
        fresh_db, mini_r2_env, monkeypatch):
    """A non-transient fetch failure means the fetch path itself is
    broken: the real wrapper raises FatalFetchError, it escapes the
    pool's per-item booking, and the run closes fatal with NOTHING
    booked as a per-object failure.
    """
    monkeypatch.setenv("CLAUDIT_INGEST_WORKERS", "1")
    monkeypatch.setenv("CLAUDIT_INGEST_PARSE_PROCESSES", "2")

    monkeypatch.setattr(ingest, "_fetch_and_parse", child_units.fatal_fetch)
    summary = ingest.run_ingest("test-proc-fatal")

    assert summary["aborted"] is False
    assert summary["failed"] == 0, (
        "the escape regressed into per-file booking")
    assert summary["error"] is not None


def test_process_pool_rerun_reparses_and_counts(
        fresh_db, mini_r2_env, monkeypatch):
    """A second pool run over an unchanged mirror reparses nothing; a
    PARSER_VERSION bump reparses every file through the pool path.
    """
    monkeypatch.setenv("CLAUDIT_INGEST_WORKERS", "1")
    monkeypatch.setenv("CLAUDIT_INGEST_PARSE_PROCESSES", "2")
    monkeypatch.setenv("CLAUDIT_INGEST_PERSIST_THREADS", "2")
    first = ingest.run_ingest("test-proc-1")
    assert first["reparsed"] == 0
    second = ingest.run_ingest("test-proc-2")
    assert second["reparsed"] == 0
    with db.viz_conn() as c:
        c.execute("UPDATE files SET parser_version = '0'")
        c.commit()
    third = ingest.run_ingest("test-proc-3")
    assert third["reparsed"] == _MIRROR


def test_process_pool_timing_line_marks_disjoint_phases(
        fresh_db, mini_r2_env, monkeypatch, caplog):
    """With CLAUDIT_TIMING on, the pool path's fetch_parse and persist
    marks stay disjoint and never exceed the run total.
    """
    monkeypatch.setenv("CLAUDIT_INGEST_WORKERS", "1")
    monkeypatch.setenv("CLAUDIT_INGEST_PARSE_PROCESSES", "2")
    monkeypatch.setenv("CLAUDIT_INGEST_PERSIST_THREADS", "2")
    monkeypatch.setenv("CLAUDIT_TIMING", "1")
    monkeypatch.setattr(_timing, "TIMING_ON", True)
    with caplog.at_level(logging.INFO, logger="claudit.ingest"):
        summary = ingest.run_ingest("test-proc-timing")
    assert summary["error"] is None
    line = next(r.getMessage() for r in caplog.records
                if r.name == "claudit.ingest"
                and r.getMessage().startswith("TIMING ingest "))

    def _ms(field):
        m = re.search(rf"\b{field}=(\d+)ms", line)
        assert m is not None, line
        return int(m.group(1))

    total, parse_ms, persist_ms = (
        _ms("total"), _ms("fetch_parse"),
        _ms("persist"))
    assert parse_ms >= 0 and persist_ms >= 0
    assert parse_ms + persist_ms <= total + 100, line
    assert "parse_processes=2" in line
    assert "persist_threads=2" in line

    # Issue #662: the wall's parts, summed across the pool.
    for part in ("child_fetch", "child_decompress", "child_parse",
                 "child_sidecar", "persist_work", "wait_parse",
                 "wait_persist"):
        assert _ms(part) >= 0, line
    assert _ms("child_parse") > 0, line
    assert _ms("wait_parse") > 0, line
    assert _ms("wait_persist") > 0, line
    assert _ms("persist_work") > 0, line


def test_pool_timing_parts_are_disjoint_slices(
        fresh_db, mini_r2_env, monkeypatch, caplog):
    """Issue #680: the pool TIMING parts are disjoint slices.

    child_fetch is the GET alone — the .xz inflate the GET ran in-call is
    booked once, as child_decompress, not again inside child_fetch — and
    wait_persist books only blocking inside the fetch_parse wall: a
    single-chunk run has no in-wall drain, so its wait_persist is zero
    and the post-loop drain is persist-phase time. The mirror is pruned
    to one project (todo ≤ chunk, so one chunk) and the seeded .xz gets
    a sleep-padded decompressor so the inflate wall dwarfs the GET it
    hides in, deterministically.
    """
    monkeypatch.setenv("CLAUDIT_INGEST_WORKERS", "1")
    monkeypatch.setenv("CLAUDIT_INGEST_PARSE_PROCESSES", "2")
    monkeypatch.setenv("CLAUDIT_INGEST_PERSIST_THREADS", "2")
    monkeypatch.setenv("CLAUDIT_TIMING", "1")
    monkeypatch.setattr(_timing, "TIMING_ON", True)
    # The run's own mirror copy (the committed fixture stays plain,
    # SV-FIXTURE-SIZE): one project, and the .xz twin of its transcript.
    for child in mini_r2_env.iterdir():
        if child.name != "projA":
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    raw = (mini_r2_env / "projA" / "sess-A" / "sess-A.jsonl").read_bytes()
    payload = raw * (1024 * 1024 // len(raw))
    (mini_r2_env / "projA" / "sess-A" / "sess-A.jsonl.xz").write_bytes(
        lzma.compress(payload))
    # The pickled parse unit pads the child's decompressor with a sleep:
    # no CPU spent, and child_decompress books a controlled ~50ms.
    monkeypatch.setattr(ingest, "_fetch_and_parse", child_units.padded_inflate)
    with caplog.at_level(logging.INFO, logger="claudit.ingest"):
        summary = ingest.run_ingest("test-proc-parts")
    assert summary["error"] is None
    line = next(r.getMessage() for r in caplog.records
                if r.name == "claudit.ingest"
                and r.getMessage().startswith("TIMING ingest "))

    def _ms(field):
        m = re.search(rf"\b{field}=(\d+)ms", line)
        assert m is not None, line
        return int(m.group(1))

    # The inflate is real booked work…
    assert _ms("child_decompress") > 0, line
    # …booked once: the GET span it was popped out of carries it no more.
    assert _ms("child_fetch") < _ms("child_decompress"), line
    # One chunk, so no in-wall drain: the post-loop drain is persist
    # time, not wait_persist.
    assert _ms("wait_persist") == 0, line


def test_persist_thread_count_defaults_and_clamps(monkeypatch):
    monkeypatch.delenv("CLAUDIT_INGEST_PERSIST_THREADS", raising=False)
    assert ingest.persist_thread_count() == 4
    monkeypatch.setenv("CLAUDIT_INGEST_PERSIST_THREADS", "")
    assert ingest.persist_thread_count() == 4
    monkeypatch.setenv("CLAUDIT_INGEST_PERSIST_THREADS", "junk")
    assert ingest.persist_thread_count() == 4
    monkeypatch.setenv("CLAUDIT_INGEST_PERSIST_THREADS", "0")
    assert ingest.persist_thread_count() == 1
    monkeypatch.setenv("CLAUDIT_INGEST_PERSIST_THREADS", "3")
    assert ingest.persist_thread_count() == 3
