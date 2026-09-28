"""Differential tests for incremental rebuilds around timezone transitions."""
from __future__ import annotations

from contextlib import contextmanager

import pytest

from test_ingest import (  # pylint: disable=unused-import
    _fresh_db_fixture as ingest_fresh_db_fixture,
)
from test_ingest_incremental import _derived_snapshot
from test_ingest_incremental_review import _rebuild_rollups, _seed_rollups
from backend import db, ingest_scope


_DST_CASES = (
    ("Asia/Kolkata", ("2026-01-01T13:15Z", "2026-01-01T12:45Z")),
    ("America/New_York", ("2026-11-01T05:15Z", "2026-11-01T06:15Z")),
    ("Europe/Prague", ("2026-10-25T00:15Z", "2026-10-25T01:15Z")),
    ("Australia/Lord_Howe", ("2026-04-04T14:45Z", "2026-04-04T15:15Z")),
    ("Australia/Lord_Howe", ("2026-10-03T15:45Z", "2026-10-03T16:15Z")),
    ("Pacific/Chatham", ("2026-04-04T13:15Z", "2026-04-04T14:15Z")),
)


def _use_timezone(monkeypatch: pytest.MonkeyPatch, zone: str) -> None:
    """Run each viz connection with one database session timezone."""
    original = db.viz_conn

    @contextmanager
    def zoned():
        with original() as conn:
            conn.execute("SELECT set_config('TimeZone', %s, true)", (zone,))
            yield conn

    monkeypatch.setattr(db, "viz_conn", zoned)


def _change_records_and_compare(times: tuple[str, str]) -> None:
    """Change two records and compare incremental tables to a full rebuild."""
    with db.viz_conn() as conn:
        for line_num, timestamp in enumerate(times, 1):
            conn.execute(
                "UPDATE records SET ts = %s, reply_latency_s = 5 "
                "WHERE file_key = 'a' AND line_num = %s",
                (timestamp, line_num),
            )
        conn.commit()
    _rebuild_rollups()

    scope = ingest_scope.Scope(False, "test", 2000)
    with db.viz_conn() as conn:
        ingest_scope.capture_and_add(scope, conn, {"a"})
        conn.execute(
            "UPDATE records SET reply_latency_s = 17, output_tokens = 19 "
            "WHERE file_key = 'a'"
        )
        conn.commit()
    _rebuild_rollups(scope)
    incremental = _derived_snapshot()

    _rebuild_rollups()
    assert incremental == _derived_snapshot(), scope.dirty_hours


@pytest.mark.parametrize("zone,times", _DST_CASES)
def test_dst_differential(fresh_db, monkeypatch, zone, times):
    """Incremental rollups equal full rebuilds on both sides of DST shifts."""
    _use_timezone(monkeypatch, zone)
    _seed_rollups()
    _change_records_and_compare(times)


def test_lord_howe_fallback_tool_rollup_differential(fresh_db, monkeypatch):
    """Overlapping Lord Howe ranges do not multiply tool rollup source rows."""
    _use_timezone(monkeypatch, "Australia/Lord_Howe")
    _seed_rollups()
    with db.viz_conn() as conn:
        conn.execute(
            "UPDATE tool_uses SET ts = '2026-04-04T14:45Z' "
            "WHERE file_key = 'a' AND line_num = 3"
        )
        conn.execute(
            "INSERT INTO tool_uses (file_key, line_num, idx, tool_name, "
            "tool_use_id, ts, model) VALUES "
            "('a', 5, 0, 'Agent', 'lord-howe-second-call', "
            "'2026-04-04T15:15Z', 'model')"
        )
        conn.commit()
    _rebuild_rollups()

    scope = ingest_scope.Scope(False, "test", 2000)
    with db.viz_conn() as conn:
        ingest_scope.capture_and_add(scope, conn, {"a"})
        conn.execute(
            "UPDATE tool_uses SET model = 'changed-model' "
            "WHERE file_key = 'a' AND line_num IN (3, 5)"
        )
        conn.commit()
    _rebuild_rollups(scope)
    incremental = _derived_snapshot()

    _rebuild_rollups()
    assert incremental == _derived_snapshot(), scope.dirty_hours
