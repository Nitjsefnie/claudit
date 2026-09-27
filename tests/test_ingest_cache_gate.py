"""The response-cache invalidation / ingest_done broadcast gate of
run_ingest_locked (issue #256): an ingest whose only data change comes
from the derived-state phases — the reprice pass after a PRICING_VERSION
bump, or the suppression purge — must gate exactly like a run that
inserted, reparsed or deleted a file.

Split from test_ingest.py's planned additions: that module sits at its
committed size-baseline entry (957), so new tests live here and borrow
its fixtures, the way test_ingest_fetch.py already does.
"""
# The fixtures register on import; pylint only sees names nobody calls.
from test_ingest import (  # pylint: disable=unused-import
    _fresh_db_fixture, _mini_r2_env_fixture, _scalar,
)

from backend import cache, db, ingest


def _prime_and_spy(key, monkeypatch):
    """Prime one response-cache entry and record every ingest_done
    broadcast; return the recorded list."""
    cache.response_cache.put(key, {"v": "old"})
    broadcasts = []
    monkeypatch.setattr(ingest.events, "broadcast_threadsafe",
                        lambda *args, **kwargs: broadcasts.append(args))
    return broadcasts


def test_a_reprice_only_ingest_invalidates_and_broadcasts(
        fresh_db, mini_r2_env, monkeypatch):
    """Issue #256: an ingest whose only data change is the reprice pass
    must mark the response cache stale and broadcast ingest_done, exactly
    like a run that inserted, reparsed or deleted files — SV-ROLLUP: the
    dashboard must never serve the previous numbers after records
    changed."""
    assert ingest.run_ingest_locked("manual")["error"] is None
    with db.viz_conn() as c:
        c.execute("UPDATE records SET pricing_version = '0'")
        c.commit()
    broadcasts = _prime_and_spy("reprice-only-key", monkeypatch)

    summary = ingest.run_ingest_locked("manual")

    assert summary["error"] is None
    assert (summary["inserted"], summary["reparsed"],
            summary["deleted"]) == (0, 0, 0), (
        "the run must have changed records ONLY through the reprice pass")
    assert broadcasts, "a reprice-only run must broadcast ingest_done"
    assert cache.response_cache.get_entry("reprice-only-key") == (
        {"v": "old"}, True), "a reprice-only run must mark responses stale"


def test_a_purge_only_ingest_invalidates_and_broadcasts(
        fresh_db, mini_r2_env, monkeypatch):
    """Issue #256: an ingest whose only data change is the suppression
    purge must mark the response cache stale and broadcast ingest_done —
    the purge deletes records without inserting, reparsing or deleting a
    file, so the walk-level counts all stay zero."""
    assert ingest.run_ingest_locked("manual")["error"] is None
    with db.viz_conn() as c:
        c.execute(
            "INSERT INTO suppressed_models(pattern, note) "
            "VALUES ('claude-sonnet%', 'issue 256')")
        c.commit()
        assert _scalar(
            c, "SELECT COUNT(*) FROM records "
               "WHERE model ILIKE 'claude-sonnet%'") > 0, (
            "the mini mirror must carry records the pattern matches")
    broadcasts = _prime_and_spy("purge-only-key", monkeypatch)

    summary = ingest.run_ingest_locked("manual")

    assert summary["error"] is None
    assert summary["inserted"] == 0
    assert summary["deleted"] == 0, (
        "orphan deletion is separate from the purge's own deletion — the "
        "purge removing records is the whole point")
    assert broadcasts, "a purge-only run must broadcast ingest_done"
    assert cache.response_cache.get_entry("purge-only-key") == (
        {"v": "old"}, True), "a purge-only run must mark responses stale"


def test_a_no_op_ingest_stays_quiet(fresh_db, mini_r2_env, monkeypatch):
    """Issue #256's flip side: a run that changes nothing stays quiet.
    The rollup rebuilds rewrite their whole derived table every run, so
    their row counts must NOT gate the invalidation — only the four
    record-mutating phases do."""
    assert ingest.run_ingest_locked("manual")["error"] is None
    broadcasts = _prime_and_spy("no-op-key", monkeypatch)

    summary = ingest.run_ingest_locked("manual")

    assert summary["error"] is None
    assert (summary["inserted"], summary["reparsed"],
            summary["deleted"]) == (0, 0, 0)
    assert not broadcasts, "a no-op run must not broadcast ingest_done"
    assert cache.response_cache.get_entry("no-op-key") == (
        {"v": "old"}, False), "a no-op run must leave responses fresh"
