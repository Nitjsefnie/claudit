"""The rollback guard on the reparse decision (issue #118).

A binary OLDER than the one that stored a file's rows must not rewrite
them: _persist DELETEs and re-INSERTs each file's records with its own
column list, so an older binary's ingest silently NULLs every column it
does not know. The reparse decision therefore never queues a file whose
stored parser_version is newer than the binary's own PARSER_VERSION —
not even when the object's etag changed: silent data erasure is worse
than a stale file, and the file is re-parsed normally once a binary at
or above the stored version runs again.

The tests live beside test_ingest (not in it) for the same reason as
test_ingest_lock: test_ingest.py stays under pylint's line cap. The
fixtures they share come from test_ingest.
"""
import logging

# The fixtures register on import; pylint only sees names nobody calls.
from test_ingest import (  # pylint: disable=unused-import
    _fresh_db_fixture, _mini_r2_env_fixture, _snapshot,
)

from backend import constants, db, ingest


def test_newer_stored_parser_version_is_never_reparsed(
        fresh_db, mini_r2_env, caplog):
    """A file stored by a NEWER parser_version keeps its file row and its
    records verbatim across a rollback — even when the object's etag
    changed, because the older binary's rewrite would NULL the columns
    the old binary does not know. A sentinel planted in records.provider
    — the exact column issue #118 demonstrated being wiped — must
    survive the run. The skip is surfaced in the run summary and logged
    at warning level."""
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        c.execute(
            "UPDATE files SET parser_version = %s WHERE session_id = 'sess-A'",
            (str(int(constants.PARSER_VERSION) + 1),),
        )
        c.execute(
            "UPDATE records SET provider = 'sentinel' "
            "WHERE file_key LIKE '%sess-A.jsonl'")
        planted = c.execute(
            "SELECT COUNT(*) FROM records "
            "WHERE file_key LIKE '%sess-A.jsonl' "
            "AND provider = 'sentinel'").fetchone()
        assert planted is not None and planted[0] > 0, (
            "the fixture must produce records for sess-A, or the "
            "sentinel proves nothing")
        c.commit()
    before = _snapshot()

    # An etag change on the guarded file must not defeat the guard.
    target = mini_r2_env / "projA" / "sess-A" / "sess-A.jsonl"
    target.write_text(target.read_text() + "\n")

    with caplog.at_level(logging.WARNING, logger="claudit.ingest"):
        result = ingest.run_ingest(trigger="manual")

    assert result["newer"] == 1
    assert result["reparsed"] == 0
    assert result["inserted"] == 0
    assert result["deleted"] == 0
    assert result["error"] is None
    assert _snapshot() == before, (
        "the guarded file row and its records must stay verbatim — "
        "etag and parser_version included")
    with db.viz_conn() as c:
        stored = c.execute(
            "SELECT newer FROM ingest_runs WHERE id = %s",
            (result["id"],)).fetchone()
    assert stored == (1,), (
        "the run row must carry the newer skip count — /health reads it "
        "from there (issue #161)")
    with db.viz_conn() as c:
        kept = c.execute(
            "SELECT DISTINCT provider FROM records "
            "WHERE file_key LIKE '%sess-A.jsonl'").fetchall()
    assert kept == [("sentinel",)], (
        "the newer binary's column values must survive the rollback run")
    warned = [r for r in caplog.records
              if r.levelno == logging.WARNING
              and "parser_version is newer" in r.getMessage()]
    assert warned, "the skip must be logged at warning level"


def test_older_stored_parser_version_still_reparses(
        fresh_db, mini_r2_env, monkeypatch):
    """The guard only blocks NEWER stored versions: a file stored by an
    older parser_version still reparses when the binary moves forward —
    the existing etag/parser_version contract, unchanged."""
    ingest.run_ingest(trigger="manual")
    monkeypatch.setattr(
        constants, "PARSER_VERSION",
        str(int(constants.PARSER_VERSION) + 1))
    result = ingest.run_ingest(trigger="manual")
    assert result["reparsed"] == 5
    assert result["newer"] == 0
    with db.viz_conn() as c:
        stored = c.execute(
            "SELECT newer FROM ingest_runs WHERE id = %s",
            (result["id"],)).fetchone()
    assert stored == (0,), (
        "a run that skipped nothing must store 0, not NULL — NULL marks "
        "rows written before the column existed")


def test_unparsable_stored_parser_version_still_reparses(
        fresh_db, mini_r2_env):
    """A stored value that does not parse as an int cannot be shown
    newer, so the ordinary reparse decision applies."""
    ingest.run_ingest(trigger="manual")
    with db.viz_conn() as c:
        c.execute("UPDATE files SET parser_version = 'not-a-number'")
        c.commit()
    result = ingest.run_ingest(trigger="manual")
    assert result["reparsed"] == 5
    assert result["newer"] == 0
    with db.viz_conn() as c:
        stored = {r[0] for r in c.execute(
            "SELECT DISTINCT parser_version FROM files").fetchall()}
    assert stored == {constants.PARSER_VERSION}
