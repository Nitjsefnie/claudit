"""The process-pool pipeline (issue #309): knob parsing, equality with
the in-process pipeline over the mini mirror, and per-file persist
failure isolation there."""

from test_ingest import (  # pylint: disable=unused-import
    _fresh_db_fixture,
    _mini_r2_env_fixture,
)
from test_ingest import _FLAKY_KEY, _scalar, _snapshot

from backend import db, ingest


def test_parse_process_count_defaults_and_clamps(monkeypatch):
    monkeypatch.delenv("INGEST_PARSE_PROCESSES", raising=False)
    assert ingest.parse_process_count() >= 1
    monkeypatch.setenv("INGEST_PARSE_PROCESSES", "0")
    assert ingest.parse_process_count() == 1
    monkeypatch.setenv("INGEST_PARSE_PROCESSES", "3")
    assert ingest.parse_process_count() == 3
    monkeypatch.setenv("INGEST_PARSE_PROCESSES", "not-a-number")
    assert ingest.parse_process_count() >= 1


def test_process_pool_ingest_matches_sequential_exactly(
        fresh_db, mini_r2_env, monkeypatch):
    """The process-pool path must land exactly what the in-process path
    lands: parse runs in forked children and persist in pool threads, but
    the per-file transaction, failure isolation, and derived state are the
    same — one run per path over the identical mirror, snapshots compared.
    """
    monkeypatch.setenv("INGEST_WORKERS", "1")
    monkeypatch.setenv("INGEST_PARSE_PROCESSES", "1")
    monkeypatch.setenv("INGEST_PERSIST_THREADS", "1")
    ingest.run_ingest("test-seq")
    sequential = _snapshot()

    # Wipe and re-ingest the identical mirror through the process pool.
    with db.viz_conn() as c:
        c.execute("DELETE FROM files")
        c.execute("DELETE FROM projects")
        c.commit()

    monkeypatch.setenv("INGEST_PARSE_PROCESSES", "2")
    monkeypatch.setenv("INGEST_PERSIST_THREADS", "2")
    summary = ingest.run_ingest("test-proc")
    parallel = _snapshot()

    assert parallel[0] == sequential[0], "files differ"
    assert parallel[1] == sequential[1], "records differ"
    assert parallel[2] == sequential[2], "tool_uses differ"
    assert parallel[3] == sequential[3], "projects differ"
    assert len(sequential[1]) > 0, "fixture produced no records — vacuous test"
    assert summary["error"] is None
    assert summary["failed"] == 0


def test_process_pool_persists_survive_a_per_file_failure(
        fresh_db, mini_r2_env, monkeypatch):
    """One file's persist failure must not take the run (or the pool) down:
    the failed file is booked per-object and every other file still lands.
    """
    monkeypatch.setenv("INGEST_WORKERS", "1")
    monkeypatch.setenv("INGEST_PARSE_PROCESSES", "2")
    monkeypatch.setenv("INGEST_PERSIST_THREADS", "2")
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
