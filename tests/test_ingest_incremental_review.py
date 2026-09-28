"""Regression tests for incremental rebuild review findings."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from test_ingest import (  # pylint: disable=unused-import
    _fresh_db_fixture as ingest_fresh_db_fixture,
    _mini_r2_env_fixture as ingest_mini_r2_env,
)
from test_ingest_incremental import _derived_snapshot, _timing_lines
from backend import db, ingest, ingest_scope, timing
from backend.ingest_rollup_latency import _overlapping_bucket_starts


def _rebuild_rollups(scope: ingest_scope.Scope | None = None) -> None:
    """Run each hourly and latency table through its public ingest seam."""
    for name in (
        "rebuild_rollup", "rebuild_tool_rollup", "rebuild_tool_error_rollup",
        "rebuild_dispatch_rollup", "rebuild_dispatch_brief_rollup",
        "rebuild_latency_rollup", "rebuild_ctx_cost_rollup",
        "rebuild_agent_rollup",
    ):
        getattr(ingest, name)(scope)


def _seed_rollups() -> None:
    """Seed two files whose canonical winner and tool use span projects."""
    with db.viz_conn() as conn:
        conn.execute(
            "INSERT INTO projects VALUES "
            "('p', 'p', now(), now()), ('q', 'q', now(), now())"
        )
        for file_key, project_id in (("a", "p"), ("z", "q")):
            conn.execute(
                "INSERT INTO files (file_key, project_id, session_id, is_main, "
                "r2_etag, r2_size_bytes, r2_last_modified, parsed_at, "
                "parser_version) VALUES (%s, %s, %s, TRUE, 'e', 1, now(), "
                "now(), 'v')",
                (file_key, project_id, file_key),
            )
        for file_key, ts in (
            ("a", "2026-01-01T13:15:00Z"),
            ("z", "2026-01-02T14:45:00Z"),
        ):
            conn.execute(
                "INSERT INTO records (file_key, line_num, uuid, ts, model, "
                "reply_latency_s) VALUES (%s, 1, 'shared', %s, 'model', 5), "
                "(%s, 2, NULL, %s, 'model', NULL)",
                (file_key, ts, file_key, ts),
            )
            conn.execute(
                "INSERT INTO tool_uses (file_key, line_num, idx, tool_name, "
                "tool_use_id, ts, model, is_error, error_kind, agent_type, "
                "dispatch_brief_ref, dispatch_prompt_chars) VALUES "
                "(%s, 3, 0, 'Agent', 'shared-call', %s, 'model', TRUE, "
                "'failed', 'worker', TRUE, 50), "
                "(%s, 4, 0, 'Read', NULL, NULL, 'model', FALSE, NULL, NULL, "
                "NULL, NULL)",
                (file_key, ts, file_key),
            )
        conn.commit()
    ingest.recompute_canonical()
    _rebuild_rollups()


def _apply_shape(scope: ingest_scope.Scope, shape: str) -> set[str]:
    dirty = {"0"} if shape == "new_winner" else {"a"}
    with db.viz_conn() as conn:
        ingest_scope.capture_and_add(scope, conn, dirty)
        if shape == "delete_winner":
            conn.execute("DELETE FROM files WHERE file_key = 'a'")
        elif shape == "new_winner":
            conn.execute(
                "INSERT INTO files (file_key, project_id, session_id, is_main, "
                "r2_etag, r2_size_bytes, r2_last_modified, parsed_at, "
                "parser_version) VALUES ('0', 'q', '0', TRUE, 'e', 1, now(), "
                "now(), 'v')"
            )
            conn.execute(
                "INSERT INTO records (file_key, line_num, uuid, ts, model, "
                "reply_latency_s) VALUES ('0', 1, 'shared', "
                "'2026-01-03T15:00Z', 'model', 9)"
            )
            conn.execute(
                "INSERT INTO tool_uses (file_key, line_num, idx, tool_name, "
                "tool_use_id, ts, model) VALUES ('0', 3, 0, 'Read', "
                "'shared-call', '2026-01-03T15:00Z', 'model')"
            )
        elif shape == "move_remove":
            conn.execute(
                "UPDATE files SET project_id = 'q', is_main = FALSE, "
                "agent_type = 'worker' WHERE file_key = 'a'"
            )
            conn.execute(
                "UPDATE records SET ts = '2026-01-04T16:00Z' "
                "WHERE file_key = 'a'"
            )
            conn.execute("DELETE FROM tool_uses WHERE file_key = 'a'")
        elif shape == "null_ts":
            conn.execute(
                "UPDATE records SET ts = NULL, reply_latency_s = NULL "
                "WHERE file_key = 'a'"
            )
        else:
            conn.execute("INSERT INTO suppressed_models VALUES ('suppressed')")
            conn.execute(
                "UPDATE records SET model = 'suppressed' WHERE file_key = 'a'"
            )
            conn.execute(
                "UPDATE tool_uses SET model = 'suppressed' WHERE file_key = 'a'"
            )
        conn.commit()
        ingest_scope.capture_and_add(scope, conn, dirty)
    return dirty


@pytest.mark.parametrize(
    "shape", ("delete_winner", "new_winner", "move_remove", "null_ts", "suppressed")
)
def test_differential_shapes(fresh_db, shape):
    """Incremental promotions and moved/deleted contributions equal full."""
    _seed_rollups()
    scope = ingest_scope.Scope(False, "test", 2000)
    _apply_shape(scope, shape)
    ingest.purge_suppressed(scope)
    ingest.recompute_canonical(scope)
    ingest.resolve_teammate_agent_types(scope)
    _rebuild_rollups(scope)
    incremental = _derived_snapshot()

    ingest.purge_suppressed()
    ingest.recompute_canonical()
    ingest.resolve_teammate_agent_types()
    _rebuild_rollups()
    assert incremental == _derived_snapshot()


def test_fractional_timezone_latency(fresh_db, monkeypatch):
    """A local dirty hour can overlap two UTC-aligned latency buckets."""
    original = db.viz_conn

    @contextmanager
    def zoned():
        with original() as conn:
            conn.execute("SET LOCAL TIME ZONE 'Asia/Kolkata'")
            yield conn

    monkeypatch.setattr(db, "viz_conn", zoned)
    _seed_rollups()
    scope = ingest_scope.Scope(False, "test", 2000)
    with db.viz_conn() as conn:
        ingest_scope.capture_and_add(scope, conn, {"a"})
        conn.execute(
            "UPDATE records SET reply_latency_s = 17 "
            "WHERE file_key = 'a' AND line_num = 1"
        )
        conn.commit()
    ingest.rebuild_latency_rollup(scope)
    incremental = _derived_snapshot()["latency_rollup"]
    ingest.rebuild_latency_rollup()
    full = _derived_snapshot()["latency_rollup"]
    assert incremental == full, (incremental, full, scope.dirty_hours)


def test_dirty_hour_bucket_endpoint_is_half_open():
    """A bucket at the expanded dirty interval's end is not touched."""
    hour = datetime(2026, 1, 1, 13, tzinfo=timezone.utc)
    assert _overlapping_bucket_starts(hour, 3600) == [
        hour, hour + timedelta(hours=1)]


@pytest.mark.parametrize("phase", ("_close_run", "warm_common"))
def test_finalization_error_leaves_incomplete_and_next_run_full(
        fresh_db, mini_r2_env, monkeypatch, caplog, phase):
    """Any finalization failure leaves a marker that forces full recovery."""
    monkeypatch.setattr(timing, "TIMING_ON", True)
    assert ingest.run_ingest(trigger="manual")["error"] is None

    def crash(*args, **kwargs):
        raise RuntimeError("review injected finalization error")

    with monkeypatch.context() as patch:
        patch.setattr(ingest, phase, crash)
        with pytest.raises(RuntimeError, match="review injected"):
            ingest.run_ingest(trigger="manual")
    with db.viz_conn() as conn:
        complete = conn.execute(
            "SELECT complete FROM ingest_derived_state WHERE singleton"
        ).fetchone()
    assert complete == (False,)

    caplog.clear()
    with caplog.at_level("INFO", logger="claudit.ingest"):
        recovered = ingest.run_ingest(trigger="manual")
    assert recovered["error"] is None
    assert "scope=full" in _timing_lines(caplog)[-1]


def test_incremental_alias_phase_keeps_zero_argument_seam(
        fresh_db, mini_r2_env, monkeypatch):
    """The alias phase resolves its patch seam through ingest globals."""
    assert ingest.run_ingest(trigger="manual")["error"] is None
    scope = ingest_scope.begin_scope()
    assert not scope.full
    calls = []
    monkeypatch.setattr(ingest, "rekey_folded_projects", lambda: calls.append(1) or 0)
    try:
        ingest._rebuild_derived_state()  # pylint: disable=protected-access
    finally:
        ingest_scope.finish_scope()
    assert calls == [1]


def test_late_full_promotion_does_not_complete_incremental_rollups(
        fresh_db, mini_r2_env, monkeypatch, caplog):
    """Promotion after the first rollup cannot claim a full rebuild."""
    monkeypatch.setattr(timing, "TIMING_ON", True)
    assert ingest.run_ingest(trigger="manual")["error"] is None
    path = mini_r2_env / "projA" / "sess-A" / "sess-A.jsonl"
    path.write_bytes(path.read_bytes() + b"\n")
    original = ingest.rebuild_rollup
    with db.viz_conn() as conn:
        previous_full_at = conn.execute(
            "SELECT last_full_at FROM ingest_derived_state WHERE singleton"
        ).fetchone()
    assert previous_full_at is not None

    def promote_after_rollups_start():
        rows = original()
        scope = ingest_scope.current_scope()
        assert scope is not None and not scope.full
        scope.promote_full("late test promotion")
        return rows

    monkeypatch.setattr(ingest, "rebuild_rollup", promote_after_rollups_start)
    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    assert result["reparsed"] == 1
    with db.viz_conn() as conn:
        state = conn.execute(
            "SELECT complete, last_full_at FROM ingest_derived_state "
            "WHERE singleton"
        ).fetchone()
    assert state is not None and state[0] is False
    assert state[1] == previous_full_at[0]

    monkeypatch.setattr(ingest, "rebuild_rollup", original)
    caplog.clear()
    with caplog.at_level("INFO", logger="claudit.ingest"):
        recovered = ingest.run_ingest(trigger="manual")
    assert recovered["error"] is None
    assert "scope=full" in _timing_lines(caplog)[-1]
